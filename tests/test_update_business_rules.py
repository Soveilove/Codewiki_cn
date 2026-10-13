# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Incremental update must build pages with the same business rules as init.

The update path never runs the init entry point that turns on business
analysis, so without explicit wiring the regenerated pages lose the topic
purpose, ownership and evidence discipline the rest of the wiki was built
with. These tests pin the wiring at both seams: the CLI update entry and the
backend's fresh runtime.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from codewiki.cli.adapters.doc_generator import CLIDocumentationGenerator
from codewiki.src.be import pydantic_ai_backend as backend_mod
from codewiki.src.config import Config


def _backend_config(tmp_path: Path) -> Config:
    return Config.from_cli(
        repo_path=str(tmp_path),
        output_dir=str(tmp_path / "docs"),
        llm_base_url="http://x",
        llm_api_key="k",
        main_model="m",
        cluster_model="m",
    )


# --------------------------------------------------------------------------- #
# Seam 1: the CLI update entry sets the flag before any leaf agent runs
# --------------------------------------------------------------------------- #


def test_update_entry_sets_business_flag_before_agents_run(tmp_path, monkeypatch):
    seen = {}

    class FakeUpdater:
        def __init__(self, config, backend, doc_generator, opts):
            seen["doc_generator"] = doc_generator

        async def run(self, prev_graph_path, new_graph, leaf_nodes, revision):
            # Leaf agents run inside updater.run: the flags must already be set.
            doc_generator = seen["doc_generator"]
            seen["at_run"] = (
                doc_generator.business_analysis,
                doc_generator.backend.business_analysis,
            )
            record = SimpleNamespace(
                outcome="no_change",
                summary=lambda: {"outcome": "no_change"},
                save=lambda working_dir: None,
            )
            return record

    monkeypatch.setattr("codewiki.src.be.updater.orchestrator.IncrementalUpdater", FakeUpdater)

    generator = object.__new__(CLIDocumentationGenerator)
    generator.progress_tracker = SimpleNamespace(
        start_stage=lambda *a, **k: None,
        update_stage=lambda *a, **k: None,
        complete_stage=lambda *a, **k: None,
    )
    generator.output_dir = tmp_path
    generator.repo_path = tmp_path
    generator.commit_id = "abc123"
    generator.verbose = False
    generator.job = SimpleNamespace(files_generated=[])
    generator.config = {}

    backend = SimpleNamespace(business_analysis=False)
    doc_generator = SimpleNamespace(
        backend=backend,
        business_analysis=False,
        create_documentation_metadata=lambda *a, **k: None,
        validate_generated_docs=lambda working_dir: [],
    )

    outcome = asyncio.run(
        CLIDocumentationGenerator._run_incremental_update(
            generator,
            backend_config=None,
            doc_generator=doc_generator,
            update_opts=None,
            prev_graph_path=None,
            components={},
            leaf_nodes=[],
        )
    )

    assert outcome == "no_change"
    assert seen["at_run"] == (True, True)


# --------------------------------------------------------------------------- #
# Seam 2: a fresh runtime inherits the backend flag (update path has no
# init-bound runtime)
# --------------------------------------------------------------------------- #


def _make_fake_runtime(captured):
    class FakeRuntime:
        def __init__(self, config, working_dir):
            self._business_analysis = False
            self.tree = {}

        @property
        def business_analysis(self):
            return self._business_analysis

        @business_analysis.setter
        def business_analysis(self, value):
            captured["runtime_business_analysis"] = value
            self._business_analysis = value

        def doc_path(self, name, path):
            return Path("/tmp") / f"{name}.md"

        def begin(self, path):
            pass

        def finish(self, path):
            pass

        def activate(self):
            class _Activate:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return False

            return _Activate()

        async def run_page_agent(self, agent, prompt, deps, expected_doc):
            captured["user_prompt"] = prompt
            captured["deps_business_analysis"] = deps.business_analysis
            return SimpleNamespace(output="ok", usage=None)

    return FakeRuntime


def _run_module_agent(tmp_path, monkeypatch, captured, backend_flag):
    monkeypatch.setattr(backend_mod, "create_fallback_models", lambda config: None)
    monkeypatch.setattr(backend_mod, "GenerationRuntime", _make_fake_runtime(captured))
    monkeypatch.setattr(
        backend_mod,
        "Agent",
        lambda *a, **k: SimpleNamespace(),
    )

    backend = backend_mod.PydanticAIBackend(_backend_config(tmp_path))
    backend.business_analysis = backend_flag
    asyncio.run(
        backend.run_module_agent(
            module_name="订单管理",
            components={},
            core_component_ids=[],
            module_path=["订单管理"],
            working_dir=str(tmp_path / "docs"),
        )
    )


def test_regenerated_page_gets_business_rules_when_flag_set(tmp_path, monkeypatch):
    captured = {}
    _run_module_agent(tmp_path, monkeypatch, captured, backend_flag=True)
    assert captured["runtime_business_analysis"] is True
    assert captured["deps_business_analysis"] is True
    assert "Document business knowledge, not a source-directory catalog" in captured["user_prompt"]


def test_page_without_business_flag_stays_old_style(tmp_path, monkeypatch):
    """Default behaviour is unchanged: nobody sets the flag, no business note."""
    captured = {}
    _run_module_agent(tmp_path, monkeypatch, captured, backend_flag=False)
    assert captured["runtime_business_analysis"] is False
    assert (
        "Document business knowledge, not a source-directory catalog" not in captured["user_prompt"]
    )


# --------------------------------------------------------------------------- #
# Seam 3: the update patch prompt and the init author prompt quote the same
# discipline constant — one source of truth, no second copy
# --------------------------------------------------------------------------- #


def test_update_prompt_and_init_prompt_share_the_discipline_constant():
    from codewiki.src.be.prompt_template import (
        OWNERSHIP_AND_EVIDENCE,
        format_user_prompt,
    )
    from codewiki.src.be.updater.prompts import format_update_system_prompt

    update_prompt = format_update_system_prompt("订单管理", "团队约定")
    assert OWNERSHIP_AND_EVIDENCE in update_prompt
    # update-specific clause: a patch must de-duplicate, never add duplication
    assert "reduce duplicate ownership, never add to it" in update_prompt
    # the pre-existing minimal-diff and evidence rules stay untouched
    assert "Prefer surgical edits" in update_prompt
    assert "Business accuracy outranks minimal diff" in update_prompt

    init_prompt = format_user_prompt(
        module_name="订单管理",
        core_component_ids=[],
        components={},
        module_tree={},
        business_analysis=True,
    )
    assert OWNERSHIP_AND_EVIDENCE in init_prompt


# --------------------------------------------------------------------------- #
# Seam 4: the fingerprint baseline only moves on a full rebuild
# --------------------------------------------------------------------------- #


def _run_update_with_fingerprints(tmp_path, monkeypatch, stored, prompts_text):
    """Drive _run_incremental_update and return the hash it wrote to metadata."""
    import json

    seen = {}

    class FakeUpdater:
        def __init__(self, config, backend, doc_generator, opts):
            pass

        async def run(self, prev_graph_path, new_graph, leaf_nodes, revision):
            return SimpleNamespace(
                outcome="no_change",
                summary=lambda: {"outcome": "no_change"},
                save=lambda working_dir: None,
            )

    monkeypatch.setattr("codewiki.src.be.updater.orchestrator.IncrementalUpdater", FakeUpdater)
    if prompts_text is not None:
        prompts = tmp_path / ".codewiki" / "prompts.md"
        prompts.parent.mkdir(parents=True, exist_ok=True)
        prompts.write_text(prompts_text, encoding="utf-8")
    metadata = {"generation_info": {}}
    if stored is not None:
        metadata["generation_info"]["prompt_asset_sha256"] = stored
    (tmp_path / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    generator = object.__new__(CLIDocumentationGenerator)
    generator.progress_tracker = SimpleNamespace(
        start_stage=lambda *a, **k: None,
        update_stage=lambda *a, **k: None,
        complete_stage=lambda *a, **k: None,
    )
    generator.output_dir = tmp_path
    generator.repo_path = tmp_path
    generator.commit_id = "abc123"
    generator.verbose = False
    generator.job = SimpleNamespace(files_generated=[])
    generator.config = {}

    def create_metadata(working_dir, components, num_leaf_nodes, prompt_asset_hash=None):
        seen["hash"] = prompt_asset_hash

    doc_generator = SimpleNamespace(
        backend=SimpleNamespace(business_analysis=False),
        business_analysis=False,
        create_documentation_metadata=create_metadata,
        validate_generated_docs=lambda working_dir: [],
    )
    asyncio.run(
        CLIDocumentationGenerator._run_incremental_update(
            generator, None, doc_generator, None, None, {}, []
        )
    )
    return seen.get("hash")


def test_update_keeps_stored_fingerprint_while_conventions_differ(tmp_path, monkeypatch):
    """The warning must persist until a full rebuild, so the baseline stays."""
    written = _run_update_with_fingerprints(
        tmp_path, monkeypatch, stored="stale", prompts_text="新约定"
    )
    assert written == "stale"


def test_update_records_current_fingerprint_without_a_baseline(tmp_path, monkeypatch):
    from codewiki.src.prompt_assets import prompt_asset_fingerprint

    written = _run_update_with_fingerprints(tmp_path, monkeypatch, stored=None, prompts_text="约定")
    assert written == prompt_asset_fingerprint(str(tmp_path))


def test_update_without_prompt_file_records_none(tmp_path, monkeypatch):
    written = _run_update_with_fingerprints(
        tmp_path, monkeypatch, stored="stale", prompts_text=None
    )
    # The file disappeared: keep warning until a full rebuild clears it.
    assert written == "stale"
