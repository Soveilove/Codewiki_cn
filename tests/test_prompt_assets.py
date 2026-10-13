# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Team prompt assets and the run parameters that live in user configuration."""

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from codewiki.cli.models.config import Configuration
from codewiki.src import prompt_assets
from codewiki.src.be.doc_layout import list_doc_files, organize_docs


def _write_prompts(repo, text):
    path = repo / ".codewiki" / "prompts.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_no_prompt_asset_returns_none(tmp_path):
    asset = prompt_assets.load_prompt_asset(str(tmp_path))
    assert asset.status == "absent"
    assert asset.instructions is None


def test_front_matter_and_body_become_instructions(tmp_path):
    _write_prompts(
        tmp_path,
        "---\nlanguage: zh\ndoc_type: 业务知识\nfocus_modules:\n"
        "  - src/billing\n---\n\n# 文档约定\n\n- 金额标注币种\n",
    )
    asset = prompt_assets.load_prompt_asset(str(tmp_path))
    assert asset.status == "loaded"
    loaded = asset.instructions
    assert loaded["language"] == "zh"
    assert loaded["doc_type"] == "业务知识"
    assert loaded["focus_modules"] == ["src/billing"]
    assert "金额标注币种" in loaded["custom_instructions"]


def test_repository_asset_replaces_weaker_prompt_sources(tmp_path):
    asset = {"language": "zh", "custom_instructions": "团队约定"}
    merged = prompt_assets.merge_prompt_asset(
        {"language": "en", "doc_type": "api", "custom_instructions": "旧的个人设置"}, asset
    )
    assert merged["language"] == "zh"
    assert merged["custom_instructions"] == "团队约定"
    # Fields the team file does not mention stay as configured.
    assert merged["doc_type"] == "api"


def test_file_without_front_matter_is_all_prompt(tmp_path):
    _write_prompts(tmp_path, "只有正文，没有 front matter")
    asset = prompt_assets.load_prompt_asset(str(tmp_path))
    assert asset.status == "loaded"
    assert asset.instructions == {"custom_instructions": "只有正文，没有 front matter"}


def test_unknown_front_matter_keys_are_ignored(tmp_path):
    _write_prompts(tmp_path, "---\nlanguage: zh\nsystem_prompt: 覆盖系统提示词\n---\n正文")
    asset = prompt_assets.load_prompt_asset(str(tmp_path))
    assert asset.instructions["language"] == "zh"
    assert "system_prompt" not in asset.instructions
    assert asset.ignored == ["system_prompt"]
    assert asset.status == "partial"


def test_broken_yaml_drops_structured_fields_but_keeps_body(tmp_path):
    _write_prompts(
        tmp_path,
        "---\nlanguage: zh\n  bad indent: [\n---\n\n# 文档约定\n\n- 先写场景\n",
    )
    asset = prompt_assets.load_prompt_asset(str(tmp_path))
    assert asset.status == "partial"
    assert asset.reason is not None and "第 3 行" in asset.reason
    # The body still applies; the structured fields do not.
    assert asset.instructions == {"custom_instructions": "# 文档约定\n\n- 先写场景"}


def test_broken_yaml_without_body_makes_the_file_unusable(tmp_path):
    _write_prompts(tmp_path, "---\nlanguage: zh\n  bad indent: [\n---\n\n")
    asset = prompt_assets.load_prompt_asset(str(tmp_path))
    assert asset.status == "invalid"
    assert asset.instructions is None
    assert "第 3 行" in asset.reason


def test_file_without_usable_settings_is_invalid(tmp_path):
    _write_prompts(tmp_path, "---\nlanguage: ''\n---\n\n   \n")
    asset = prompt_assets.load_prompt_asset(str(tmp_path))
    assert asset.status == "invalid"
    assert asset.instructions is None


def test_concurrency_and_trace_round_trip_through_configuration():
    config = Configuration(
        base_url="http://localhost:1/v1", main_model="m", cluster_model="m", concurrency=3
    )
    restored = Configuration.from_dict(json.loads(json.dumps(config.to_dict())))
    assert restored.concurrency == 3
    assert restored.trace is True
    off = Configuration.from_dict({**config.to_dict(), "trace": False})
    assert off.trace is False


def test_output_defaults_to_codewiki_without_overwriting_saved_choices():
    config = Configuration(base_url="", main_model="m", cluster_model="m")
    assert config.default_output == ".codewiki"
    assert config.to_dict()["default_output"] == ".codewiki"
    assert Configuration.from_dict({}).default_output == ".codewiki"
    assert Configuration.from_dict({"default_output": "custom"}).default_output == "custom"


def test_prompt_asset_is_not_a_generated_page_or_rewritten(tmp_path):
    text = "# Team conventions\n\n[Billing](billing.md)\n"
    prompts = _write_prompts(tmp_path, text)
    docs = prompts.parent
    (docs / "billing.md").write_text("# Billing\n", encoding="utf-8")
    assert list_doc_files(str(docs)) == {"billing": "billing.md"}
    organize_docs(str(docs), "hierarchical", {})
    assert prompts.read_text(encoding="utf-8") == text


def test_module_names_cannot_overwrite_prompt_asset():
    from codewiki.src.be.module_naming import dedupe_module_tree_names

    tree = dedupe_module_tree_names({"prompts": {"components": [], "children": {}}})
    assert "prompts" not in tree
    assert len(tree) == 1


def test_analyzer_skips_codewiki_even_without_gitignore(tmp_path):
    from codewiki.src.be.dependency_analyzer.analysis.repo_analyzer import RepoAnalyzer

    (tmp_path / "app.py").write_text("def process():\n    return 1\n", encoding="utf-8")
    prompts = _write_prompts(tmp_path, "Team instructions")
    (prompts.parent / "generated.py").write_text("def generated(): pass\n", encoding="utf-8")
    analysis = RepoAnalyzer(include_patterns=["*"], use_gitignore=False)
    children = analysis.analyze_repository_structure(str(tmp_path))["file_tree"]["children"]
    assert {child["name"] for child in children} == {"app.py"}


@pytest.mark.parametrize("output", [None, "docs", "custom"])
@pytest.mark.parametrize("update", [False, True])
def test_cli_output_default_and_override(tmp_path, monkeypatch, output, update):
    command = importlib.import_module("codewiki.cli.commands.generate")
    from codewiki.cli.models.job import DocumentationJob

    monkeypatch.chdir(tmp_path)
    _write_prompts(tmp_path, "Team conventions")
    expected = tmp_path / (output or ".codewiki")
    config = Configuration(base_url="http://localhost:1/v1", main_model="m", cluster_model="m")
    manager = SimpleNamespace(
        load=lambda: True,
        is_configured=lambda: True,
        get_config=lambda: config,
        get_api_key=lambda: "test",
    )
    captured = {}

    class Generator:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def generate(self):
            return DocumentationJob()

    def unexpected_confirmation(*args, **kwargs):
        pytest.fail("Team prompt asset must not trigger a documentation overwrite confirmation")

    monkeypatch.setattr(command, "ConfigManager", lambda: manager)
    monkeypatch.setattr(command, "validate_repository", lambda path: (path, [("Python", 1)]))
    monkeypatch.setattr(command, "is_git_repository", lambda path: False)
    monkeypatch.setattr(command, "get_git_commit_hash", lambda path: None)
    monkeypatch.setattr(command, "CLIDocumentationGenerator", Generator)
    monkeypatch.setattr(command, "display_post_generation_instructions", lambda **kwargs: None)
    monkeypatch.setattr(command.click, "confirm", unexpected_confirmation)
    args = ["--output", output] if output else []
    if update:
        expected.mkdir(exist_ok=True)
        (expected / "metadata.json").write_text(
            json.dumps({"generation_info": {"layout": "hierarchical", "language": "zh"}}),
            encoding="utf-8",
        )
        (expected / "overview.md").write_text("# Existing wiki\n", encoding="utf-8")
        monkeypatch.setattr(command, "_detect_changed_files", lambda *a, **kw: ["app.py"])
        args.append("--update")
    result = CliRunner().invoke(command.generate_command, args)
    assert result.exit_code == 0, result.output
    assert captured["output_dir"] == expected
    assert captured["config"]["update"] is update
    assert captured["config"]["agent_instructions"]["custom_instructions"] == "Team conventions"
    if update:
        from codewiki.src.language import normalize_language

        assert normalize_language(
            captured["config"]["agent_instructions"]["language"]
        ) == normalize_language("zh")


@pytest.mark.parametrize(
    "prompts_text,expected_marker",
    [
        ("---\nlanguage: zh\nexclude_patterns:\n  - tests\n---\n\n正文约定", "✓ 已加载团队提示词"),
        (
            "---\nlanguage: zh\nexclude_paterns:\n  - tests\n---\n\n正文约定",
            "无法识别的字段:exclude_paterns",
        ),
        ("---\nlanguage: zh\n  bad: [\n---\n\n", "未生效"),
    ],
)
def test_cli_reports_prompt_asset_status_without_verbose(
    tmp_path, monkeypatch, prompts_text, expected_marker
):
    """The three-state summary must show up in a normal (non-verbose) run."""
    command = importlib.import_module("codewiki.cli.commands.generate")
    from codewiki.cli.models.job import DocumentationJob

    monkeypatch.chdir(tmp_path)
    _write_prompts(tmp_path, prompts_text)
    config = Configuration(base_url="http://localhost:1/v1", main_model="m", cluster_model="m")
    manager = SimpleNamespace(
        load=lambda: True,
        is_configured=lambda: True,
        get_config=lambda: config,
        get_api_key=lambda: "test",
    )

    class Generator:
        def __init__(self, **kwargs):
            pass

        def generate(self):
            return DocumentationJob()

    monkeypatch.setattr(command, "ConfigManager", lambda: manager)
    monkeypatch.setattr(command, "validate_repository", lambda path: (path, [("Python", 1)]))
    monkeypatch.setattr(command, "is_git_repository", lambda path: False)
    monkeypatch.setattr(command, "get_git_commit_hash", lambda path: None)
    monkeypatch.setattr(command, "CLIDocumentationGenerator", Generator)
    monkeypatch.setattr(command, "display_post_generation_instructions", lambda **kwargs: None)
    result = CliRunner().invoke(command.generate_command, [])
    assert result.exit_code == 0, result.output
    assert expected_marker in result.output


@pytest.mark.parametrize("shared", [False, True])
def test_update_fallback_preserves_assets_and_previous_docs(tmp_path, shared):
    from codewiki.cli.adapters.doc_generator import CLIDocumentationGenerator, _generated_files

    prompts = _write_prompts(tmp_path, "Team conventions")
    docs = prompts.parent if shared else tmp_path / "custom"
    docs.mkdir(exist_ok=True)
    (prompts.parent / "sessions" / "active").mkdir(parents=True)
    session_file = prompts.parent / "sessions" / "active" / "summary.json"
    session_file.write_text("{}", encoding="utf-8")
    settings = prompts.parent / "team.json"
    settings.write_text('{"team": true}', encoding="utf-8")
    (docs / "billing").mkdir()
    (docs / "billing.md").write_text("# Billing", encoding="utf-8")
    (docs / "billing" / "order.md").write_text("# Order", encoding="utf-8")
    tree = {"billing": {"components": [], "children": {"order": {"children": {}}}}}
    (docs / "module_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    (docs / "metadata.json").write_text("{}", encoding="utf-8")
    (docs / "temp").mkdir()
    (docs / "temp" / "reference_index.json").write_text("{}", encoding="utf-8")
    assert "prompts.md" not in _generated_files(str(docs))

    generator = object.__new__(CLIDocumentationGenerator)
    generator.repo_path = tmp_path
    generator.output_dir = docs
    generator.commit_id = "1234567890"
    generator.verbose = False
    generator._move_docs_aside()

    backup = docs / ".prev-12345678" if shared else tmp_path / "custom.prev-12345678"
    assert generator._preserved_docs_dir == str(backup)
    assert (backup / "billing" / "order.md").read_text(encoding="utf-8") == "# Order"
    assert (backup / "metadata.json").exists()
    assert (backup / "temp" / "reference_index.json").exists()
    assert not (docs / "module_tree.json").exists()
    assert not (docs / "billing.md").exists()
    assert prompts.read_text(encoding="utf-8") == "Team conventions"
    assert session_file.exists()
    assert settings.read_text(encoding="utf-8") == '{"team": true}'
    assert docs.is_dir()


def test_mcp_analyze_defaults_to_target_repo_not_process_cwd(tmp_path, monkeypatch):
    from codewiki.mcp.session import SessionStore
    from codewiki.mcp.tools.analysis import handle_analyze_repo
    from codewiki.src.be import dependency_analyzer

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)
    captured = []

    class Builder:
        def __init__(self, config):
            captured.append(config)

        def build_dependency_graph(self):
            return {}, []

    monkeypatch.setattr(dependency_analyzer, "DependencyGraphBuilder", Builder)
    store = SessionStore()
    result = json.loads(handle_analyze_repo({"repo_path": str(repo)}, store))
    assert result["output_dir"] == str(repo / ".codewiki")
    assert captured[0].docs_dir == str(repo / ".codewiki")
    assert captured[0].dependency_graph_dir == str(
        repo / ".codewiki" / "sessions" / result["session_id"] / "temp" / "dependency_graphs"
    )
    explicit = repo / "custom"
    result = json.loads(
        handle_analyze_repo({"repo_path": str(repo), "output_dir": str(explicit)}, store)
    )
    assert result["output_dir"] == str(explicit)


def test_mcp_module_tree_default_and_override(tmp_path, monkeypatch):
    from codewiki.mcp.server import _legacy_get_module_tree

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)
    for name in (".codewiki", "custom"):
        docs = repo / name
        docs.mkdir()
        path = docs / "module_tree.json"
        path.write_text("{}", encoding="utf-8")
        arguments = {"repo_path": str(repo)}
        if name == "custom":
            arguments["output_dir"] = str(docs)
        response = asyncio.run(_legacy_get_module_tree(arguments))
        result = json.loads(response[0].text)
        assert result["module_tree_path"] == str(path)


def test_mcp_schemas_describe_repo_relative_defaults():
    from codewiki.mcp.server import _fine_grained_tools, _legacy_tools

    tools = {tool.name: tool for tool in [*_fine_grained_tools(), *_legacy_tools()]}
    for name in ("analyze_repo", "generate_docs", "get_module_tree"):
        output = tools[name].inputSchema["properties"]["output_dir"]
        assert "<repo>/.codewiki" in output["description"]
        assert "default" not in output


# --------------------------------------------------------------------------- #
# Convention change detection (fingerprint in metadata.json)
# --------------------------------------------------------------------------- #


def _write_metadata(output_dir, fingerprint):
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metadata.json").write_text(
        json.dumps({"generation_info": {"prompt_asset_sha256": fingerprint}}),
        encoding="utf-8",
    )


def test_detect_prompt_change_states(tmp_path):
    from codewiki.src.prompt_assets import detect_prompt_change, prompt_asset_fingerprint

    docs = tmp_path / ".codewiki"
    prompts = _write_prompts(tmp_path, "---\nlanguage: zh\n---\n\n约定甲")
    current = prompt_asset_fingerprint(str(tmp_path))

    # No baseline: docs from a build that predates fingerprinting.
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "metadata.json").write_text("{}", encoding="utf-8")
    change = detect_prompt_change(str(tmp_path), str(docs))
    assert change.status == "no_baseline"
    assert change.current == current

    # Unchanged: same bytes as the fingerprinted build.
    _write_metadata(docs, current)
    assert detect_prompt_change(str(tmp_path), str(docs)).status == "unchanged"

    # Changed: the file moved on.
    prompts.write_text("---\nlanguage: zh\n---\n\n约定乙", encoding="utf-8")
    change = detect_prompt_change(str(tmp_path), str(docs))
    assert change.status == "changed"
    assert change.stored == current
    assert change.current != current

    # Deleted counts as a change too.
    prompts.unlink()
    change = detect_prompt_change(str(tmp_path), str(docs))
    assert change.status == "changed"
    assert change.current is None


def test_metadata_records_the_prompt_fingerprint(tmp_path):
    from codewiki.src.be.documentation_generator import DocumentationGenerator
    from codewiki.src.prompt_assets import FINGERPRINT_KEY, prompt_asset_fingerprint

    _write_prompts(tmp_path, "团队约定")
    generator = object.__new__(DocumentationGenerator)
    generator.config = SimpleNamespace(
        repo_path=str(tmp_path), main_model="m", max_depth=2, layout="hierarchical"
    )
    generator.commit_id = None
    generator.create_documentation_metadata(str(tmp_path), {}, 0)
    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["generation_info"][FINGERPRINT_KEY] == prompt_asset_fingerprint(str(tmp_path))


@pytest.mark.parametrize("answer,expect_full", [("n\n", False), ("y\ny\n", True)])
def test_cli_asks_before_updating_with_changed_conventions(
    tmp_path, monkeypatch, answer, expect_full
):
    command = importlib.import_module("codewiki.cli.commands.generate")
    from codewiki.cli.models.job import DocumentationJob

    monkeypatch.chdir(tmp_path)
    _write_prompts(tmp_path, "新约定")
    docs = tmp_path / ".codewiki"
    _write_metadata(docs, "stale-fingerprint")
    (docs / "overview.md").write_text("# Existing wiki\n", encoding="utf-8")
    config = Configuration(base_url="http://localhost:1/v1", main_model="m", cluster_model="m")
    manager = SimpleNamespace(
        load=lambda: True,
        is_configured=lambda: True,
        get_config=lambda: config,
        get_api_key=lambda: "test",
    )
    captured = {}

    class Generator:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def generate(self):
            return DocumentationJob()

    monkeypatch.setattr(command, "ConfigManager", lambda: manager)
    monkeypatch.setattr(command, "validate_repository", lambda path: (path, [("Python", 1)]))
    monkeypatch.setattr(command, "is_git_repository", lambda path: False)
    monkeypatch.setattr(command, "get_git_commit_hash", lambda path: None)
    monkeypatch.setattr(command, "CLIDocumentationGenerator", Generator)
    monkeypatch.setattr(command, "display_post_generation_instructions", lambda **kwargs: None)
    monkeypatch.setattr(command, "_detect_changed_files", lambda *a, **kw: ["app.py"])
    result = CliRunner().invoke(command.generate_command, ["--update"], input=answer)
    assert result.exit_code == 0, result.output
    assert "团队提示词已变更" in result.output
    assert captured["config"]["update"] is (not expect_full)


def test_cli_stays_silent_when_conventions_unchanged(tmp_path, monkeypatch):
    command = importlib.import_module("codewiki.cli.commands.generate")
    from codewiki.cli.models.job import DocumentationJob
    from codewiki.src.prompt_assets import prompt_asset_fingerprint

    monkeypatch.chdir(tmp_path)
    _write_prompts(tmp_path, "约定")
    docs = tmp_path / ".codewiki"
    _write_metadata(docs, prompt_asset_fingerprint(str(tmp_path)))
    (docs / "overview.md").write_text("# Existing wiki\n", encoding="utf-8")
    config = Configuration(base_url="http://localhost:1/v1", main_model="m", cluster_model="m")
    manager = SimpleNamespace(
        load=lambda: True,
        is_configured=lambda: True,
        get_config=lambda: config,
        get_api_key=lambda: "test",
    )
    captured = {}

    class Generator:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def generate(self):
            return DocumentationJob()

    monkeypatch.setattr(command, "ConfigManager", lambda: manager)
    monkeypatch.setattr(command, "validate_repository", lambda path: (path, [("Python", 1)]))
    monkeypatch.setattr(command, "is_git_repository", lambda path: False)
    monkeypatch.setattr(command, "get_git_commit_hash", lambda path: None)
    monkeypatch.setattr(command, "CLIDocumentationGenerator", Generator)
    monkeypatch.setattr(command, "display_post_generation_instructions", lambda **kwargs: None)
    monkeypatch.setattr(command, "_detect_changed_files", lambda *a, **kw: ["app.py"])
    result = CliRunner().invoke(command.generate_command, ["--update"])
    assert result.exit_code == 0, result.output
    assert "团队提示词已变更" not in result.output
    assert captured["config"]["update"] is True


@pytest.fixture
def prompt_update_repo(tmp_path, monkeypatch):
    from git import Actor, Repo
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from codewiki.cli import config_manager
    from codewiki.src.be import pydantic_ai_backend

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    git_repo = Repo.init(repo)
    git_repo.index.add(["app.py"])
    actor = Actor("CodeWiki test", "test@example.invalid")
    commit = git_repo.index.commit("initial source", author=actor, committer=actor)
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.json"
    credentials_path = home / "credentials.json"
    config = Configuration(
        base_url="http://localhost:1/v1", main_model="m", cluster_model="m", trace=False
    )
    config_path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
    credentials_path.write_text(json.dumps({"api_key": "test"}), encoding="utf-8")
    monkeypatch.setenv("CODEWIKI_NO_KEYRING", "1")
    monkeypatch.setattr(config_manager, "CONFIG_FILE", config_path)
    monkeypatch.setattr(config_manager, "CREDENTIALS_FILE", credentials_path)
    monkeypatch.chdir(repo)

    def respond(messages, info):
        count = sum(isinstance(message, ModelResponse) for message in messages)
        if count == 0:
            return ModelResponse(
                parts=[ToolCallPart("read_code_components", {"component_ids": ["app.py::value"]})]
            )
        if count == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "str_replace_editor",
                        {
                            "command": "create",
                            "working_dir": "docs",
                            "path": "repo.md",
                            "file_text": "# 新约定文档\n\nvalue 返回 1。\n",
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    monkeypatch.setattr(
        pydantic_ai_backend, "create_fallback_models", lambda config: FunctionModel(respond)
    )
    monkeypatch.setattr(
        pydantic_ai_backend,
        "call_llm",
        lambda *args, **kwargs: "<OVERVIEW># 新约定文档\n\nvalue 返回 1。</OVERVIEW>",
    )
    return repo, commit.hexsha


@pytest.mark.parametrize("update", [True, False])
def test_full_regeneration_rewrites_cached_pages_and_preserves_team_assets(
    prompt_update_repo, update
):
    from codewiki.cli.commands.generate import generate_command
    from codewiki.src.prompt_assets import prompt_asset_fingerprint

    repo, commit_id = prompt_update_repo
    prompts_text = "---\nlanguage: zh\n---\n\n新约定"
    prompts = _write_prompts(repo, prompts_text)
    docs = prompts.parent
    (docs / "metadata.json").write_text(
        json.dumps(
            {
                "generation_info": {
                    "commit_id": commit_id,
                    "language": "zh",
                    "prompt_asset_sha256": "stale",
                }
            }
        ),
        encoding="utf-8",
    )
    for name in ("first_module_tree.json", "module_tree.json"):
        (docs / name).write_text("{}", encoding="utf-8")
    (docs / "overview.md").write_text("# 旧约定文档\n", encoding="utf-8")
    session = docs / "sessions" / "active" / "summary.json"
    session.parent.mkdir(parents=True)
    session.write_text("{}", encoding="utf-8")

    args = ["--no-artifacts"]
    if update:
        args.append("--update")
    result = CliRunner().invoke(generate_command, args, input="y\ny\n")

    assert result.exit_code == 0, result.output
    assert (docs / "overview.md").read_text(encoding="utf-8") == "# 新约定文档\n\nvalue 返回 1。\n"
    backup = docs / f".prev-{commit_id[:8]}"
    assert (backup / "overview.md").read_text(encoding="utf-8") == "# 旧约定文档\n"
    assert prompts.read_text(encoding="utf-8") == prompts_text
    assert session.read_text(encoding="utf-8") == "{}"
    metadata = json.loads((docs / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["generation_info"]["prompt_asset_sha256"] == prompt_asset_fingerprint(str(repo))
    if update:
        record = json.loads((docs / "update_record.json").read_text(encoding="utf-8"))
        assert record["prompt_asset_change"] == {
            "status": "changed",
            "stored": "stale",
            "current": prompt_asset_fingerprint(str(repo)),
            "action": "full",
            "execution": "full",
        }
        assert metadata["last_update"]["previous_docs"] == str(backup)


def test_detect_prompt_change_distinguishes_missing_and_null_baselines(tmp_path):
    docs = tmp_path / ".codewiki"
    _write_metadata(docs, None)
    absent = prompt_assets.detect_prompt_change(str(tmp_path), str(docs))
    assert absent.status == "unchanged"
    assert absent.stored is None and absent.current is None

    _write_prompts(tmp_path, "新增团队约定")
    added = prompt_assets.detect_prompt_change(str(tmp_path), str(docs))
    assert added.status == "changed"
    assert added.stored is None and added.current is not None


@pytest.mark.parametrize("with_prompts", [True, False])
def test_update_without_code_changes_records_first_baseline(prompt_update_repo, with_prompts):
    from codewiki.cli.commands.generate import generate_command
    from codewiki.src.prompt_assets import prompt_asset_fingerprint

    repo, commit_id = prompt_update_repo
    docs = repo / ".codewiki"
    docs.mkdir()
    if with_prompts:
        _write_prompts(repo, "团队约定")
    metadata = {
        "generation_info": {"commit_id": commit_id, "language": "zh"},
        "update_history": [{"outcome": "incremental"}],
        "statistics": {"total_components": 1},
    }
    (docs / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    page = docs / "overview.md"
    page.write_text("# 既有文档\n", encoding="utf-8")
    before = page.read_bytes()

    result = CliRunner().invoke(generate_command, ["--update"])

    assert result.exit_code == 0, result.output
    assert "团队提示词已变更" not in result.output
    written = json.loads((docs / "metadata.json").read_text(encoding="utf-8"))
    assert written["generation_info"]["prompt_asset_sha256"] == prompt_asset_fingerprint(str(repo))
    assert written["statistics"] == metadata["statistics"]
    assert written["generation_info"]["commit_id"] == commit_id
    assert page.read_bytes() == before


def test_metadata_can_preserve_a_null_prompt_baseline(tmp_path):
    from codewiki.src.be.documentation_generator import DocumentationGenerator
    from codewiki.src.config import Config

    _write_prompts(tmp_path, "新增约定")
    config = Config.from_cli(
        repo_path=str(tmp_path),
        output_dir=str(tmp_path),
        llm_base_url="http://localhost:1/v1",
        llm_api_key="test",
        main_model="m",
        cluster_model="m",
    )
    generator = DocumentationGenerator(config, backend=SimpleNamespace())
    generator.create_documentation_metadata(str(tmp_path), {}, 0, prompt_asset_hash=None)
    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["generation_info"]["prompt_asset_sha256"] is None


@pytest.mark.parametrize("stored", ["stale", None])
def test_legacy_update_keeps_prompt_baseline_until_full_rebuild(prompt_update_repo, stored):
    from codewiki.cli.commands.generate import generate_command

    repo, commit_id = prompt_update_repo
    docs = _write_prompts(repo, "新约定").parent
    history = [{"outcome": "incremental"}]
    metadata = {
        "generation_info": {
            "commit_id": commit_id,
            "language": "zh",
            "prompt_asset_sha256": stored,
        },
        "update_history": history,
    }
    (docs / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    for name in ("first_module_tree.json", "module_tree.json"):
        (docs / name).write_text("{}", encoding="utf-8")
    (docs / "overview.md").write_text("# 既有文档\n", encoding="utf-8")
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    from git import Actor, Repo

    git_repo = Repo(repo)
    git_repo.index.add(["app.py"])
    actor = Actor("CodeWiki test", "test@example.invalid")
    git_repo.index.commit("change source", author=actor, committer=actor)

    result = CliRunner().invoke(
        generate_command, ["--update", "--update-rung", "0", "--no-artifacts"], input="n\n"
    )

    assert result.exit_code == 0, result.output
    written = json.loads((docs / "metadata.json").read_text(encoding="utf-8"))
    assert written["generation_info"]["prompt_asset_sha256"] == stored
    record = json.loads((docs / "update_record.json").read_text(encoding="utf-8"))
    assert record["prompt_asset_change"]["stored"] == stored
    assert record["prompt_asset_change"]["action"] == "declined"
    assert record["prompt_asset_change"]["execution"] == "incremental"
    assert written["update_history"][:-1] == history
    repeated = CliRunner().invoke(generate_command, ["--update"], input="n\n")
    assert repeated.exit_code == 0, repeated.output
    assert "团队提示词已变更" in repeated.output


@pytest.mark.parametrize(
    "stored,action", [("stale", "declined"), (None, "declined"), ("missing", "ok")]
)
def test_no_change_update_records_prompt_detection_and_choice(prompt_update_repo, stored, action):
    from codewiki.cli.commands.generate import generate_command
    from codewiki.src.prompt_assets import prompt_asset_fingerprint

    repo, commit_id = prompt_update_repo
    docs = _write_prompts(repo, "新约定").parent
    info = {"commit_id": commit_id, "language": "zh"}
    if stored != "missing":
        info["prompt_asset_sha256"] = stored
    history = [{"outcome": "incremental"}]
    (docs / "metadata.json").write_text(
        json.dumps(
            {
                "generation_info": info,
                "update_history": history,
            }
        ),
        encoding="utf-8",
    )
    page = docs / "overview.md"
    page.write_text("# 既有文档\n", encoding="utf-8")
    before = page.read_bytes()

    result = CliRunner().invoke(generate_command, ["--update"], input="n\n")

    assert result.exit_code == 0, result.output
    record = json.loads((docs / "update_record.json").read_text(encoding="utf-8"))
    assert record["outcome"] == "no_change"
    assert record["prompt_asset_change"] == {
        "status": "no_baseline" if stored == "missing" else "changed",
        "stored": None if stored == "missing" else stored,
        "current": prompt_asset_fingerprint(str(repo)),
        "action": action,
        "execution": "no_change",
    }
    metadata = json.loads((docs / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["last_update"]["prompt_asset_change"] == record["prompt_asset_change"]
    assert metadata["update_history"][:-1] == history
    assert page.read_bytes() == before


@pytest.mark.parametrize("with_graph", [True, False])
def test_component_update_and_fallback_record_prompt_choice(prompt_update_repo, with_graph):
    from git import Actor, Repo

    from codewiki.cli.commands.generate import generate_command
    from codewiki.src.be.dependency_analyzer import DependencyGraphBuilder
    from codewiki.src.config import Config

    repo, commit_id = prompt_update_repo
    docs = _write_prompts(repo, "新约定").parent
    (docs / "metadata.json").write_text(
        json.dumps(
            {
                "generation_info": {
                    "commit_id": commit_id,
                    "language": "zh",
                    "prompt_asset_sha256": None,
                }
            }
        ),
        encoding="utf-8",
    )
    for name in ("first_module_tree.json", "module_tree.json"):
        (docs / name).write_text("{}", encoding="utf-8")
    (docs / "overview.md").write_text("# 旧约定文档\n", encoding="utf-8")
    if with_graph:
        config = Config.from_cli(
            repo_path=str(repo),
            output_dir=str(docs),
            llm_base_url="http://localhost:1/v1",
            llm_api_key="test",
            main_model="m",
            cluster_model="m",
            artifacts_enabled=False,
        )
        DependencyGraphBuilder(config).build_dependency_graph()
    with (repo / "app.py").open("a", encoding="utf-8") as handle:
        handle.write("\n# New comment outside the component\n")
    git_repo = Repo(repo)
    git_repo.index.add(["app.py"])
    actor = Actor("CodeWiki test", "test@example.invalid")
    git_repo.index.commit("source comment", author=actor, committer=actor)

    result = CliRunner().invoke(generate_command, ["--update", "--no-artifacts"], input="n\n")

    assert result.exit_code == 0, result.output
    record = json.loads((docs / "update_record.json").read_text(encoding="utf-8"))
    expected = "no_change" if with_graph else "full"
    assert record["prompt_asset_change"]["execution"] == expected
    assert record["prompt_asset_change"]["stored"] is None
    assert record["prompt_asset_change"]["action"] == "declined"
    metadata = json.loads((docs / "metadata.json").read_text(encoding="utf-8"))
    if with_graph:
        assert metadata["generation_info"]["prompt_asset_sha256"] is None
    else:
        backup = docs / f".prev-{git_repo.head.commit.hexsha[:8]}"
        before_rebuild = json.loads((backup / "update_record.json").read_text(encoding="utf-8"))
        assert before_rebuild["prompt_asset_change"]["action"] == "declined"
        assert before_rebuild["prompt_asset_change"]["execution"] == "detector_failure"


def test_generate_with_unchanged_conventions_preserves_resume_pages(prompt_update_repo):
    from codewiki.cli.commands.generate import generate_command
    from codewiki.src.prompt_assets import prompt_asset_fingerprint

    repo, commit_id = prompt_update_repo
    docs = _write_prompts(repo, "团队约定").parent
    (docs / "metadata.json").write_text(
        json.dumps(
            {
                "generation_info": {
                    "commit_id": commit_id,
                    "prompt_asset_sha256": prompt_asset_fingerprint(str(repo)),
                }
            }
        ),
        encoding="utf-8",
    )
    for name in ("first_module_tree.json", "module_tree.json"):
        (docs / name).write_text("{}", encoding="utf-8")
    page = docs / "overview.md"
    page.write_text("# 已完成页面\n", encoding="utf-8")
    before = page.read_bytes()

    result = CliRunner().invoke(generate_command, ["--no-artifacts"], input="y\n")

    assert result.exit_code == 0, result.output
    assert page.read_bytes() == before
    assert not list(docs.glob(".prev-*"))


@pytest.mark.parametrize("text", ["{broken", "[]"])
def test_prompt_baseline_migration_does_not_overwrite_invalid_metadata(tmp_path, text):
    from codewiki.cli.commands.generate import check_prompt_asset_change

    docs = _write_prompts(tmp_path, "团队约定").parent
    metadata = docs / "metadata.json"
    metadata.write_text(text, encoding="utf-8")

    result = check_prompt_asset_change(tmp_path, docs)

    assert result["status"] == "no_baseline"
    assert result["action"] == "ok"
    assert metadata.read_text(encoding="utf-8") == text


def test_no_change_record_keeps_the_actual_comparison_commit(prompt_update_repo):
    from git import Actor, Repo

    from codewiki.cli.commands.generate import generate_command

    repo, old_commit = prompt_update_repo
    docs = repo / ".codewiki"
    docs.mkdir()
    (docs / "metadata.json").write_text(
        json.dumps(
            {
                "generation_info": {
                    "commit_id": old_commit,
                    "prompt_asset_sha256": None,
                }
            }
        ),
        encoding="utf-8",
    )
    git_repo = Repo(repo)
    actor = Actor("CodeWiki test", "test@example.invalid")
    new_commit = git_repo.index.commit("no source change", author=actor, committer=actor).hexsha

    result = CliRunner().invoke(generate_command, ["--update"])

    assert result.exit_code == 0, result.output
    record = json.loads((docs / "update_record.json").read_text(encoding="utf-8"))
    assert record["revision"]["old_commit"] == old_commit
    assert record["revision"]["new_commit"] == new_commit
