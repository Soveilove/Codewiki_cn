# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""MCP keeps IDE-driven generation compatible with the CLI's business wiki."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codewiki.mcp import server
from codewiki.mcp.session import SessionStore
from codewiki.mcp.tools.prompt_server import _resolve_prompt
from codewiki.src.be.doc_layout import resolve_output_dir
from codewiki.src.be.updater.graph_diff import diff_graphs
from codewiki.src.be.updater.graph_store import graph_file_path, load_graph, snapshot_old_graph
from codewiki.src.prompt_assets import FINGERPRINT_KEY, prompt_asset_fingerprint


def call(name, **arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return json.loads(result[0].text)


def content(result):
    return result.get("content") or Path(result["content_file"]).read_text(encoding="utf-8")


def start(repo, **arguments):
    result = call("analyze_repo", repo_path=str(repo), **arguments)
    assert "error" not in result, result
    session = server._store.get(result["session_id"])
    assert session is not None, result
    return result, session


def save_tree(session, tree=None):
    if tree is None:
        tree = {
            "订单处理": {"purpose": "处理订单", "components": session.leaf_nodes, "children": {}}
        }
    result = call("save_module_tree", session_id=session.session_id, module_tree=tree)
    assert "error" not in result, result
    return result


def write_pages(session, suffix="初次生成"):
    order = call("get_processing_order", session_id=session.session_id)
    entries = json.loads(Path(order["processing_order_file"]).read_text(encoding="utf-8"))
    for relative in [entry["doc_path"] for entry in entries] + ["overview.md"]:
        page = Path(session.output_dir) / relative
        text = f"# {page.stem}\n\n{suffix}\n"
        if page.exists():
            result = call(
                "edit_doc_file",
                session_id=session.session_id,
                filename=relative,
                command="str_replace",
                old_str=page.read_text(encoding="utf-8"),
                new_str=text,
            )
        else:
            result = call(
                "write_doc_file", session_id=session.session_id, filename=relative, content=text
            )
        assert "error" not in result, result


def complete(repo):
    result, session = start(repo)
    save_tree(session)
    write_pages(session)
    closed = call("close_session", session_id=session.session_id)
    assert closed["metadata_saved"], closed
    return Path(result["output_dir"])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    target = tmp_path / "repo"
    target.mkdir()
    (target / "orders.py").write_text(
        "class Orders:\n    def total(self):\n        return 1\n", encoding="utf-8"
    )
    (target / "isolated.py").write_text("def unrelated():\n    return 2\n", encoding="utf-8")
    prompts = target / ".codewiki" / "prompts.md"
    prompts.parent.mkdir()
    prompts.write_text(
        "---\nlanguage: zh\ndoc_type: 业务知识\n---\n\n金额标注币种。", encoding="utf-8"
    )
    monkeypatch.setattr(server, "_store", SessionStore())
    yield target
    for state in list(server._store._sessions.values()):
        if state.workspace:
            state.workspace.cleanup()


def test_analysis_needs_no_model_and_keeps_every_file(repo, monkeypatch):
    monkeypatch.setattr(
        server, "_load_config", lambda: pytest.fail("IDE mode must not load model credentials")
    )
    result, session = start(repo)
    files = {session.components[cid].relative_path for cid in session.leaf_nodes}
    assert files == {"orders.py", "isolated.py"}
    assert session.config.language == "Chinese (Simplified)"
    assert result["team_prompt"]["status"] == "loaded"
    assert "custom_instructions" in result["team_prompt"]["applied"]
    assert Path(result["files"]["planning_evidence"]).is_file()
    assert Path(result["files"]["generation_context"]).is_file()
    assert not (repo / ".codewiki" / "temp").exists()


def test_java_planning_evidence_keeps_type_users_and_nested_parent(repo):
    (repo / "Payload.java").write_text(
        "package sample;\nclass Payload {\n static class Detail { Long id; }\n}\n",
        encoding="utf-8",
    )
    (repo / "Worker.java").write_text(
        "package sample;\nimport java.util.*;\nclass Worker { void handle(Payload payload) {} }\n",
        encoding="utf-8",
    )
    result, session = start(repo)
    evidence = json.loads(Path(result["files"]["planning_evidence"]).read_text(encoding="utf-8"))
    payload = "Payload.java::Payload"
    assert "Worker.java::Worker" in evidence[payload]["type_used_by"]
    assert any(
        "handle(Payload payload)" in item["source_excerpt"]
        for item in evidence[payload]["usage_evidence"]
    )
    assert session.components["Payload.java::Detail"].class_name == "Payload"


@pytest.mark.parametrize(
    "kind", ["cluster", "system_leaf", "system_complex", "user", "overview_module", "overview_repo"]
)
def test_all_prompts_use_session_language_and_team_conventions(repo, kind):
    _, session = start(repo)
    save_tree(session)
    prompt = call(
        "get_prompt",
        session_id=session.session_id,
        prompt_type=kind,
        variables={"module_name": "订单处理", "language": "en"},
    )
    text = content(prompt)
    assert "Chinese (Simplified)" in text
    assert "金额标注币种" in text
    assert "not a source-directory catalog" in text
    assert "str_replace_editor" not in text
    assert "generate_sub_module_documentation" not in text
    if kind == "user":
        assert "return 1" in text and "return 2" in text
        assert session.source_reads
    assert Path(prompt["content_file"]).read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    "kind", ["cluster", "system_leaf", "system_complex", "user", "overview_module", "overview_repo"]
)
def test_standalone_prompt_api_remains_available(kind):
    text = _resolve_prompt(kind, {"language": "zh", "module_name": "订单处理"})
    assert "<OUTPUT_LANGUAGE>" in text
    assert "<MODULE_NAMING_LANGUAGE>" in text
    assert "not a source-directory catalog" in text


def test_partial_team_asset_diagnosis_and_scan_filters(repo):
    (repo / ".codewiki" / "prompts.md").write_text(
        "---\nlanguage: zh\ninclude_patterns: ['orders.py']\ntypo: true\n---\n约定",
        encoding="utf-8",
    )
    result, session = start(repo, include_patterns="isolated.py")
    assert result["team_prompt"]["status"] == "partial"
    assert result["team_prompt"]["ignored"] == ["typo"]
    assert {node.relative_path for node in session.components.values()} == {"orders.py"}


def test_large_prompt_uses_file_side_channel(repo):
    prompts = repo / ".codewiki" / "prompts.md"
    prompts.write_text("---\nlanguage: zh\n---\n" + "长期团队约定。" * 2500, encoding="utf-8")
    _, session = start(repo)
    prompt = call("get_prompt", session_id=session.session_id, prompt_type="system_leaf")
    assert prompt["content"] is None
    assert len(content(prompt)) > 12_000
    assert content(prompt).count("长期团队约定") == 2500


def test_output_discovery_preserves_old_docs_and_explicit_choices(repo):
    old = repo / "docs"
    old.mkdir()
    (old / "module_tree.json").write_text("{}", encoding="utf-8")
    assert resolve_output_dir(repo) == old
    assert resolve_output_dir(repo, repo / ".codewiki") == repo / ".codewiki"
    result, session = start(repo)
    assert result["output_dir"] == str(old)
    assert call("get_module_tree", repo_path=str(repo))["module_tree_path"] == str(
        old / "module_tree.json"
    )
    assert session.config.layout == "hierarchical"


def test_analysis_only_never_advances_cli_graph_or_metadata(repo):
    docs = complete(repo)
    graph = Path(graph_file_path(str(docs / "temp" / "dependency_graphs"), str(repo)))
    old_graph, old_meta = graph.read_bytes(), (docs / "metadata.json").read_bytes()
    (repo / "orders.py").write_text(
        "class Orders:\n    def total(self):\n        return 3\n", encoding="utf-8"
    )
    result, session = start(repo)
    assert not result["changes"]["no_changes"]
    assert "订单处理" in result["changes"]["affected_modules"]
    assert graph.read_bytes() == old_graph
    closed = call("close_session", session_id=session.session_id)
    assert not closed["metadata_saved"]
    assert graph.read_bytes() == old_graph
    assert (docs / "metadata.json").read_bytes() == old_meta
    previous = snapshot_old_graph(str(graph.parent), str(repo))
    _, next_session = start(repo)
    assert not diff_graphs(load_graph(previous), next_session.components).is_empty


def test_incomplete_close_is_retryable_and_preserves_initial_tree(repo):
    result, session = start(repo)
    saved = save_tree(session)
    assert Path(saved["tree_path"]).parent == Path(result["workspace_dir"])
    assert not (repo / ".codewiki" / "module_tree.json").exists()
    call(
        "write_doc_file",
        session_id=session.session_id,
        filename="订单处理.md",
        content="# 订单处理",
    )
    failed = call("close_session", session_id=session.session_id)
    assert failed["status"] == "incomplete"
    assert "overview.md" in failed["missing_pages"]
    assert server._store.get(session.session_id) is session
    assert not (repo / ".codewiki" / "metadata.json").exists()
    call("write_doc_file", session_id=session.session_id, filename="overview.md", content="# 总览")
    closed = call("close_session", session_id=session.session_id)
    assert closed["metadata_saved"]
    assert server._store.get(session.session_id) is None
    assert not Path(result["workspace_dir"]).exists()
    metadata = json.loads((repo / ".codewiki" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["generation_info"]["language"] == "Chinese (Simplified)"
    assert metadata["generation_info"][FINGERPRINT_KEY] == prompt_asset_fingerprint(str(repo))


def test_convention_only_change_requires_real_full_rewrite(repo):
    docs = complete(repo)
    old_metadata = (docs / "metadata.json").read_bytes()
    old_hash = json.loads(old_metadata)["generation_info"][FINGERPRINT_KEY]
    (docs / "prompts.md").write_text("---\nlanguage: ja\n---\n新的团队约定", encoding="utf-8")
    result, session = start(repo)
    assert not result["changes"]["no_changes"]
    assert result["changes"]["requires_full_regeneration"]
    assert session.config.language == "Chinese (Simplified)"
    call(
        "edit_doc_file",
        session_id=session.session_id,
        filename="overview.md",
        command="insert",
        new_str="局部更新",
    )
    failed = call("close_session", session_id=session.session_id)
    assert failed["status"] == "incomplete"
    assert (docs / "metadata.json").read_bytes() == old_metadata
    call("close_session", session_id=session.session_id, discard=True)
    _, full = start(repo, regenerate=True)
    assert full.config.language == "Japanese"
    write_pages(full, "全量重新生成")
    assert call("close_session", session_id=full.session_id)["metadata_saved"]
    new_info = json.loads((docs / "metadata.json").read_text(encoding="utf-8"))["generation_info"]
    assert new_info["language"] == "Japanese"
    assert new_info[FINGERPRINT_KEY] != old_hash


def test_prompt_baseline_is_the_applied_snapshot_not_close_time(repo):
    _, session = start(repo)
    applied = session.prompt_change.current
    save_tree(session)
    write_pages(session)
    (repo / ".codewiki" / "prompts.md").write_text("新约定", encoding="utf-8")
    closed = call("close_session", session_id=session.session_id)
    assert closed["prompt_changed_during_session"]
    info = json.loads((repo / ".codewiki" / "metadata.json").read_text(encoding="utf-8"))[
        "generation_info"
    ]
    assert info[FINGERPRINT_KEY] == applied
    result, _ = start(repo)
    assert result["changes"]["prompt_asset_change"]["status"] == "changed"


@pytest.mark.parametrize(
    "filename",
    [
        "prompts.md",
        "PROMPTS.md",
        "nested/prompts.md",
        "sessions/fake.md",
        "temp/fake.md",
        "../outside.md",
    ],
)
def test_writers_protect_team_and_working_assets(repo, filename):
    _, session = start(repo)
    before = (repo / ".codewiki" / "prompts.md").read_bytes()
    assert "error" in call(
        "write_doc_file", session_id=session.session_id, filename=filename, content="overwrite"
    )
    assert "error" in call(
        "edit_doc_file",
        session_id=session.session_id,
        filename=filename,
        command="insert",
        new_str="overwrite",
    )
    assert (repo / ".codewiki" / "prompts.md").read_bytes() == before


def test_tree_validation_and_primary_ownership(repo):
    _, session = start(repo)
    bad = call(
        "save_module_tree",
        session_id=session.session_id,
        module_tree={"订单处理": {"components": [session.leaf_nodes[0]]}},
    )
    assert "error" in bad and bad["leftover_component_ids"]
    invalid = call(
        "save_module_tree",
        session_id=session.session_id,
        module_tree={"../bad": {"components": session.leaf_nodes}},
    )
    assert "error" in invalid
    tree = {
        "订单处理": {"components": session.leaf_nodes},
        "共享使用": {"components": session.leaf_nodes},
    }
    save_tree(session, tree)
    assert session.module_tree["共享使用"]["components"] == []
    assert session.module_tree["共享使用"]["shared_components"] == session.leaf_nodes
    assert session.module_tree["订单处理"]["components_shared_with"]


def test_repeated_tree_save_keeps_pending_pages_and_retired_pages(repo):
    docs = complete(repo)
    old_tree = (docs / "first_module_tree.json").read_bytes()
    _, session = start(repo)
    tree = {"新的业务主题": {"components": session.leaf_nodes}}
    save_tree(session, tree)
    save_tree(session, tree)
    call(
        "edit_doc_file",
        session_id=session.session_id,
        filename="overview.md",
        command="insert",
        new_str="新的业务主题",
    )
    closed = call("close_session", session_id=session.session_id)
    assert closed["status"] == "incomplete"
    assert "新的业务主题.md" in closed["pending_pages"]
    assert "订单处理.md" in closed["retired_pages"]
    write_pages(session, "新主题源码说明")
    assert call("close_session", session_id=session.session_id)["status"] == "incomplete"
    old_page = (docs / "订单处理.md").read_bytes()
    assert (
        call(
            "edit_doc_file", session_id=session.session_id, filename="订单处理.md", command="delete"
        )["status"]
        == "deleted"
    )
    assert not (docs / "订单处理.md").exists()
    assert (
        call(
            "edit_doc_file", session_id=session.session_id, filename="订单处理.md", command="undo"
        )["status"]
        == "undone"
    )
    assert (docs / "订单处理.md").read_bytes() == old_page
    call("edit_doc_file", session_id=session.session_id, filename="订单处理.md", command="delete")
    (docs / "manual.md").write_text("人工维护文件", encoding="utf-8")
    assert "error" in call(
        "edit_doc_file", session_id=session.session_id, filename="manual.md", command="delete"
    )
    assert call("close_session", session_id=session.session_id)["metadata_saved"]
    assert (docs / "first_module_tree.json").read_bytes() == old_tree


def test_missing_legacy_language_stays_english_in_incremental_mode(repo):
    docs = complete(repo)
    metadata = json.loads((docs / "metadata.json").read_text(encoding="utf-8"))
    metadata["generation_info"].pop("language")
    (docs / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    _, session = start(repo)
    assert session.config.language is None


def test_invalid_utf8_asset_is_diagnosed_not_crashed(repo):
    prompts = repo / ".codewiki" / "prompts.md"
    prompts.write_bytes(b"\xff\xfe invalid")
    result, session = start(repo)
    assert result["team_prompt"]["status"] == "invalid"
    assert session.prompt_asset.instructions is None
    assert session.prompt_asset.sha256 == prompt_asset_fingerprint(str(repo))


def test_empty_tree_whole_repo_mode_is_supported(repo):
    _, session = start(repo)
    saved = save_tree(session, {})
    assert not saved["validation"]["leftover_component_ids"]
    order = call("get_processing_order", session_id=session.session_id)
    assert json.loads(Path(order["processing_order_file"]).read_text(encoding="utf-8")) == []
    prompt = call(
        "get_prompt",
        session_id=session.session_id,
        prompt_type="user",
        variables={"module_path": []},
    )
    assert "overview.md" in content(prompt)
    write_pages(session)
    assert call("close_session", session_id=session.session_id)["metadata_saved"]


def test_undo_does_not_count_as_completed_update(repo):
    docs = complete(repo)
    before = (docs / "metadata.json").read_bytes()
    (repo / "orders.py").write_text(
        "class Orders:\n    def total(self):\n        return 9\n", encoding="utf-8"
    )
    _, session = start(repo)
    call(
        "edit_doc_file",
        session_id=session.session_id,
        filename="订单处理.md",
        command="insert",
        new_str="变更",
    )
    call("edit_doc_file", session_id=session.session_id, filename="订单处理.md", command="undo")
    assert call("close_session", session_id=session.session_id)["status"] == "incomplete"
    assert (docs / "metadata.json").read_bytes() == before


def test_stale_session_cannot_overwrite_newer_generation_baseline(repo):
    docs = complete(repo)
    _, session = start(repo, regenerate=True)
    write_pages(session, "当前会话")
    metadata = json.loads((docs / "metadata.json").read_text(encoding="utf-8"))
    metadata["generation_info"]["timestamp"] = "2099-01-01T00:00:00Z"
    (docs / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    closed = call("close_session", session_id=session.session_id)
    assert closed["status"] == "incomplete"
    assert "another workflow" in closed["reason"]


def test_git_commits_during_session_are_not_baselined_away(repo):
    import git

    local = git.Repo.init(repo)
    actor = git.Actor("Test", "test@example.invalid")
    local.index.add(["orders.py", "isolated.py"])
    initial = local.index.commit("initial", author=actor, committer=actor).hexsha
    _, session = start(repo)
    save_tree(session)
    write_pages(session)
    (repo / "orders.py").write_text(
        "class Orders:\n    def total(self):\n        return 8\n", encoding="utf-8"
    )
    local.index.add(["orders.py"])
    local.index.commit("mid-session", author=actor, committer=actor)
    assert call("close_session", session_id=session.session_id)["metadata_saved"]
    metadata = json.loads((repo / ".codewiki" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["generation_info"]["commit_id"] == initial
    result, _ = start(repo)
    assert "orders.py" in result["changes"]["changed_files"]


def test_baseline_write_failure_restores_graph_and_metadata(repo, monkeypatch):
    docs = complete(repo)
    graph = Path(graph_file_path(str(docs / "temp" / "dependency_graphs"), str(repo)))
    before_graph = graph.read_bytes()
    before_meta = (docs / "metadata.json").read_bytes()
    _, session = start(repo, regenerate=True)
    write_pages(session, "新版本")
    original = Path.write_bytes
    fired = False

    def fail_once(path, data):
        nonlocal fired
        if path == docs / "metadata.json" and not fired:
            fired = True
            raise OSError("disk write failed")
        return original(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_once)
    closed = call("close_session", session_id=session.session_id)
    assert "error" in closed
    assert graph.read_bytes() == before_graph
    assert (docs / "metadata.json").read_bytes() == before_meta
    assert server._store.get(session.session_id) is session
    assert call("close_session", session_id=session.session_id)["metadata_saved"]


def test_legacy_generation_applies_team_asset_and_runtime_settings(repo, monkeypatch):
    from codewiki.cli.models.config import Configuration

    config = Configuration(
        base_url="http://localhost:1/v1", main_model="fake", cluster_model="fake", concurrency=5
    )
    monkeypatch.setattr(
        server,
        "_load_config",
        lambda: SimpleNamespace(get_config=lambda: config, get_api_key=lambda: "test"),
    )
    seen = {}

    class Generator:
        def __init__(self, backend_config, **kwargs):
            seen["config"] = backend_config

        async def run(self, *, write_metadata=True):
            assert not write_metadata
            self.analysis_components = {}
            self.analysis_leaf_nodes = []
            docs = Path(seen["config"].docs_dir)
            docs.mkdir(exist_ok=True)
            (docs / "overview.md").write_text("# 总览", encoding="utf-8")

    monkeypatch.setattr("codewiki.src.be.documentation_generator.DocumentationGenerator", Generator)
    response = call("generate_docs", repo_path=str(repo), language="en")
    assert response["status"] == "success"
    assert response["team_prompt"]["status"] == "loaded"
    assert seen["config"].language == "Chinese (Simplified)"
    assert seen["config"].concurrency == 5
    assert "金额标注币种" in seen["config"].get_prompt_addition()


def test_legacy_generation_requires_confirmation_when_team_asset_changed(repo, monkeypatch):
    docs = complete(repo)
    (docs / "prompts.md").write_text("新约定", encoding="utf-8")
    from codewiki.cli.models.config import Configuration

    config = Configuration(
        base_url="http://localhost:1/v1", main_model="fake", cluster_model="fake"
    )
    monkeypatch.setattr(
        server,
        "_load_config",
        lambda: SimpleNamespace(get_config=lambda: config, get_api_key=lambda: "test"),
    )
    response = call("generate_docs", repo_path=str(repo))
    assert response["status"] == "needs_confirmation"


def test_cli_default_also_finds_old_docs(repo, monkeypatch):
    from click.testing import CliRunner
    from codewiki.cli.models.config import Configuration
    from codewiki.cli.models.job import DocumentationJob

    command = importlib.import_module("codewiki.cli.commands.generate")
    old = repo / "docs"
    old.mkdir()
    (old / "module_tree.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(repo)
    config = Configuration(
        base_url="http://localhost:1/v1", main_model="fake", cluster_model="fake"
    )
    manager = SimpleNamespace(
        load=lambda: True,
        is_configured=lambda: True,
        get_config=lambda: config,
        get_api_key=lambda: "test",
    )
    monkeypatch.setattr(command, "ConfigManager", lambda: manager)
    monkeypatch.setattr(command, "validate_repository", lambda p: (p, [("python", 2)]))
    monkeypatch.setattr(command, "is_git_repository", lambda p: False)
    seen = {}

    class Generator:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def generate(self):
            return DocumentationJob()

    monkeypatch.setattr(command, "CLIDocumentationGenerator", Generator)
    monkeypatch.setattr(command, "display_post_generation_instructions", lambda **kwargs: None)
    result = CliRunner().invoke(command.generate_command, input="y\n")
    assert result.exit_code == 0, result.output
    assert seen["output_dir"] == old


def test_mcp_cli_mcp_can_update_the_same_wiki(repo, monkeypatch):
    import git
    from codewiki.cli.adapters.doc_generator import CLIDocumentationGenerator
    from codewiki.src.be import documentation_generator as generation
    from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor
    from codewiki.src.be.backend import AgentReply
    from codewiki.src.be.doc_layout import module_doc_relpath
    from codewiki.src.prompt_assets import load_prompt_asset

    local = git.Repo.init(repo)
    actor = git.Actor("Test", "test@example.invalid")
    local.index.add(["orders.py", "isolated.py"])
    local.index.commit("initial", author=actor, committer=actor)
    docs = complete(repo)
    first_tree = (docs / "first_module_tree.json").read_bytes()
    (repo / "orders.py").write_text(
        "class Orders:\n    def total(self):\n        return 4\n", encoding="utf-8"
    )
    local.index.add(["orders.py"])
    commit = local.index.commit("cli update", author=actor, committer=actor).hexsha
    calls = []

    class Backend:
        last_usage = None

        def complete(self, prompt, **kwargs):
            return "<OVERVIEW># 总览\n\n更新后的总览</OVERVIEW>"

        async def run_module_agent(
            self, module_name, components, core_component_ids, module_path, working_dir
        ):
            path = Path(working_dir) / module_doc_relpath(module_path, "hierarchical")
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# {module_name}\n\n" + str(core_component_ids), encoding="utf-8")
            return json.loads((Path(working_dir) / "module_tree.json").read_text(encoding="utf-8"))

        async def run_update_agent(self, system_prompt, user_prompt, deps):
            calls.append(deps.current_module_name)
            assert "Chinese (Simplified)" in system_prompt
            assert "金额标注币种" in system_prompt
            assert deps.business_analysis
            verdicts = {}
            for path in deps.allowed_write_paths:
                page = Path(path)
                result = await str_replace_editor(
                    SimpleNamespace(deps=deps),
                    "docs",
                    "insert",
                    path=page.relative_to(docs).as_posix(),
                    insert_line=1,
                    new_str="CLI 已按源码变化更新。",
                )
                assert "not in this agent" not in result
                verdicts[page.name] = {"verdict": "patch", "reason": "source changed"}
            return AgentReply(text="```json\n" + json.dumps({"verdicts": verdicts}) + "\n```")

    monkeypatch.setattr(generation, "get_backend", lambda cfg: Backend())
    adapter = CLIDocumentationGenerator(
        repo,
        docs,
        {
            "base_url": "http://localhost:1/v1",
            "api_key": "test",
            "main_model": "fake",
            "cluster_model": "fake",
            "trace": False,
            "concurrency": 1,
            "agent_instructions": load_prompt_asset(str(repo)).instructions,
            "update": True,
            "update_options": {"tau_full": 2.0, "tau_tree": 2.0},
        },
        commit_id=commit,
    )
    adapter.generate()
    assert calls
    record = json.loads((docs / "update_record.json").read_text(encoding="utf-8"))
    assert record["outcome"] == "incremental"
    assert record["diff"]["counts"]["body"] > 0
    assert "CLI 已按源码变化更新" in (docs / "订单处理.md").read_text(encoding="utf-8")
    info = json.loads((docs / "metadata.json").read_text(encoding="utf-8"))["generation_info"]
    assert info["commit_id"] == commit
    assert info["language"] == "Chinese (Simplified)"
    (repo / "orders.py").write_text(
        "class Orders:\n    def total(self):\n        return 7\n", encoding="utf-8"
    )
    result, session = start(repo)
    assert "订单处理" in result["changes"]["affected_modules"]
    write_pages(session, "MCP 再次更新")
    assert call("close_session", session_id=session.session_id)["metadata_saved"]
    assert (docs / "first_module_tree.json").read_bytes() == first_tree
    saved_graph = load_graph(graph_file_path(str(docs / "temp" / "dependency_graphs"), str(repo)))
    assert diff_graphs(saved_graph, session.components).is_empty


def test_real_stdio_session_lifecycle(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    repo = tmp_path / "stdio-repo"
    repo.mkdir()
    (repo / "api.py").write_text("def run():\n    return True\n", encoding="utf-8")

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "codewiki.mcp.server"],
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 10

                async def invoke(name, arguments):
                    result = await session.call_tool(name, arguments)
                    assert not result.isError
                    payload = json.loads(result.content[0].text)
                    assert "error" not in payload, payload
                    return payload

                analysis = await invoke("analyze_repo", {"repo_path": str(repo), "language": "zh"})
                sid = analysis["session_id"]
                ids = json.loads(Path(analysis["files"]["leaf_nodes"]).read_text(encoding="utf-8"))
                await invoke(
                    "save_module_tree",
                    {"session_id": sid, "module_tree": {"运行": {"components": ids}}},
                )
                prompt = await invoke(
                    "get_prompt",
                    {
                        "session_id": sid,
                        "prompt_type": "system_leaf",
                        "variables": {"module_name": "运行"},
                    },
                )
                assert "Chinese (Simplified)" in content(prompt)
                await invoke("read_code_components", {"session_id": sid, "component_ids": ids})
                for filename in ("运行.md", "overview.md"):
                    await invoke(
                        "write_doc_file",
                        {"session_id": sid, "filename": filename, "content": "# 运行\n\n源码说明"},
                    )
                closed = await invoke("close_session", {"session_id": sid})
                assert closed["metadata_saved"]
                assert not Path(analysis["workspace_dir"]).exists()

    asyncio.run(run())
