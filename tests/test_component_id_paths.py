# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Component IDs keep their canonical graph keys across path separators."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from codewiki.src.be import cluster_modules as cluster
from codewiki.src.be.agent_tools import generate_sub_module_documentations as sub
from codewiki.src.be.agent_tools.read_code_components import read_code_components
from codewiki.src.be.dependency_analyzer.models.core import Node


def _node(component_id):
    path, _, symbol = component_id.partition("::")
    return Node(
        id=component_id,
        name=symbol,
        component_type="function",
        file_path=path,
        relative_path=path,
        source_code=f"export const {symbol} = () => 42;",
    )


@pytest.mark.parametrize(
    ("canonical", "requested"),
    [
        (r"src\api\common\index.ts::getChapterList", "src/api/common/index.ts::getChapterList"),
        ("src/api/common/index.ts::getChapterList", r"src\api\common\index.ts::getChapterList"),
        (r"src\a.ts::symbol\nested", r"src/a.ts::symbol\nested"),
    ],
)
def test_resolve_preserves_graph_keys_and_symbols(canonical, requested):
    components = {canonical: _node(canonical)}
    assert cluster.resolve_component_ids([requested, canonical], components) == [canonical]


@pytest.mark.parametrize("separator", [": ", ":: "])
def test_grouped_file_symbols_expand_to_exact_keys(separator, caplog):
    path = r"src\api\ecc\workbench\personalCenter.ts"
    ids = [f"{path}::{name}" for name in ("batchReadMessage", "getTodayUnread", "getUnreadCount")]
    components = {key: _node(key) for key in ids}
    requested = (
        path.replace("\\", "/") + separator + "batchReadMessage, getTodayUnread, getUnreadCount"
    )
    assert cluster.resolve_component_ids([requested], components, strict=True) == ids
    assert not caplog.records
    ctx = SimpleNamespace(deps=SimpleNamespace(components=components))
    result = asyncio.run(read_code_components(ctx, [requested]))
    assert all(components[key].source_code in result for key in ids)


def test_grouped_unknown_symbol_is_not_guessed():
    key = "src/a.ts::f"
    components = {key: _node(key)}
    with pytest.raises(ValueError, match="src/a.ts::missing"):
        cluster.resolve_component_ids(["src/a.ts: f, missing"], components, strict=True)


def test_invalid_delegation_retries_before_mutating_tree(tmp_path):
    from pydantic_ai import ModelRetry

    key = "src/a.ts::f"
    tree = {"父": {"components": [key], "children": {}}}
    deps = SimpleNamespace(current_module_name="父", components={key: _node(key)}, module_tree=tree)
    with pytest.raises(ModelRetry, match="Unknown or ambiguous"):
        asyncio.run(
            sub.generate_sub_module_documentation(
                SimpleNamespace(deps=deps),
                {"子": ["src/a.ts: f, missing"]},
            )
        )
    assert tree["父"]["children"] == {}
    assert not list(tmp_path.iterdir())


def test_unknown_case_sensitive_and_ambiguous_ids_are_rejected(caplog):
    windows = r"src\dir\a.ts::f"
    posix = "src/dir/a.ts::f"
    components = {windows: _node(windows), posix: _node(posix)}
    requested = [r"src\dir/a.ts::f", "src/dir/a.ts::F", "src/missing.ts::f", 123, windows]
    assert cluster.resolve_component_ids(requested, components) == [windows]
    assert sum("Skipping invalid leaf node" in r.message for r in caplog.records) == 4


def test_source_summary_and_partition_use_canonical_ids(caplog):
    ids = [r"src\api\common\index.ts::getChapterList", r"src\api\oa\index.ts::approvalDetail"]
    components = {key: _node(key) for key in ids}
    requested = [key.replace("\\", "/") for key in ids]
    summary, code = cluster.format_potential_core_components(requested, components)
    for key in ids:
        assert key in summary
        assert components[key].source_code in code
    batches = cluster.partition_leaf_nodes_by_structure(requested, components, lambda batch: True)
    assert batches == [ids]
    assert not any("Skipping invalid" in r.message for r in caplog.records)


def test_clustering_response_saves_canonical_ids():
    key = r"src\api\common\index.ts::getChapterList"
    components = {key: _node(key)}
    response = (
        "<GROUPED_COMPONENTS>"
        + repr({"api": {"components": [key.replace("\\", "/"), key]}})
        + "</GROUPED_COMPONENTS>"
    )
    with patch.object(cluster, "format_cluster_prompt", return_value="prompt"):
        tree = cluster._cluster_via_llm(
            [key], components, None, {}, None, "test", lambda _: response
        )
    assert tree["api"]["components"] == [key]


def test_clustering_prompt_normalizes_windows_paths_and_restores_graph_keys(caplog):
    import json

    keys = [r"src\api\new\a.ts::f", r"src\business\test\b.ts::g"]
    components = {key: _node(key) for key in keys}
    calls = []

    def complete(prompt):
        calls.append(prompt)
        listing = prompt.split("<POTENTIAL_CORE_COMPONENTS>")[1].split(
            "</POTENTIAL_CORE_COMPONENTS>"
        )[0]
        assert "\\" not in listing
        assert all(key.replace("\\", "/") in listing for key in keys)
        return json.dumps({"接口": {"components": [key.replace("\\", "/") for key in keys]}})

    tree = cluster._cluster_via_llm(keys, components, None, {}, None, "repo", complete)
    assert tree["接口"]["components"] == keys
    assert len(calls) == 1
    assert not caplog.records


def test_short_references_restore_ambiguous_windows_and_posix_graph_keys():
    import json

    keys = [r"src\a.ts::f", "src/a.ts::f"]
    components = {key: _node(key) for key in keys}
    response = json.dumps({"接口": {"components": ["C1", "C0"]}})
    tree = cluster._cluster_via_llm(keys, components, None, {}, None, "repo", lambda _: response)
    assert tree["接口"]["components"] == list(reversed(keys))


def test_clustering_prompt_does_not_collapse_ambiguous_normalized_ids():
    keys = [r"src\a.ts::f", "src/a.ts::f"]
    components = {key: _node(key) for key in keys}
    summary, _ = cluster.format_potential_core_components(keys, components, normalize_paths=True)
    assert all(key in summary for key in keys)
    requested = [line.strip() for line in summary.splitlines() if line.startswith("\t")]
    assert set(cluster.resolve_component_ids(requested, components, strict=True)) == set(keys)


def test_cluster_entry_accepts_posix_ids_in_windows_graph(caplog):
    key = r"src\api\common\index.ts::getChapterList"
    components = {key: _node(key)}
    config = SimpleNamespace(max_token_per_module=100000)
    result = cluster.cluster_modules(
        [key.replace("\\", "/")], components, config, current_module_name="api"
    )
    assert result == {}
    assert not any("Skipping invalid" in r.message for r in caplog.records)


def test_source_reader_uses_canonical_key_and_reports_unknown():
    key = r"src\api\common\index.ts::getChapterList"
    components = {key: _node(key)}
    ctx = SimpleNamespace(deps=SimpleNamespace(components=components))
    result = asyncio.run(read_code_components(ctx, [key.replace("\\", "/")]))
    assert f"# Component {key}:" in result
    assert components[key].source_code in result
    from pydantic_ai import ModelRetry

    with pytest.raises(ModelRetry, match="missing.ts::f"):
        asyncio.run(read_code_components(ctx, ["missing.ts::f"]))


def test_sub_agent_tree_and_prompt_receive_canonical_ids(tmp_path):
    key = r"src\api\common\index.ts::getChapterList"
    components = {key: _node(key)}
    tree = {"root": {"children": {}}}
    deps = SimpleNamespace(
        current_module_name="root",
        components=components,
        module_tree=tree,
        absolute_docs_path=str(tmp_path),
        path_to_current_module=["root"],
        current_depth=1,
        max_depth=3,
        custom_instructions="",
        config=SimpleNamespace(max_token_per_leaf_module=100000, agent_retries=1, request_limit=10),
    )
    ctx = SimpleNamespace(deps=deps)
    agent = SimpleNamespace(run=AsyncMock())
    with (
        patch.object(sub, "create_fallback_models", return_value=None),
        patch.object(
            sub,
            "plan_sub_module_specs",
            return_value=SimpleNamespace(name_map={"api_test": "api_test"}, skipped={}),
        ),
        patch.object(sub, "Agent", return_value=agent),
        patch.object(sub, "format_leaf_system_prompt", return_value="system"),
        patch.object(sub, "format_user_prompt", return_value="prompt") as prompt,
        patch.object(sub, "sub_module_report", return_value="done"),
    ):
        result = asyncio.run(
            sub.generate_sub_module_documentation(ctx, {"api_test": [key.replace("\\", "/")]})
        )
    assert result == "done"
    assert tree["root"]["children"]["api_test"]["components"] == [key]
    assert prompt.call_args.kwargs["core_component_ids"] == [key]
    agent.run.assert_awaited_once()
    assert deps.path_to_current_module == ["root"]
    assert deps.current_module_name == "root"


def test_source_reader_resolves_unique_symbol_without_guessing_path():
    key = r"src\business\renewal\RenewalService.java::RenewalService"
    ctx = SimpleNamespace(deps=SimpleNamespace(components={key: _node(key)}))
    for requested in ("RenewalService", "src/service/RenewalService.java::RenewalService"):
        result = asyncio.run(read_code_components(ctx, [requested]))
        assert f"# Component {key}:" in result


def test_source_reader_requires_disambiguation_and_lists_real_ids():
    keys = ["one/a.py::Service", "two/b.py::Service"]
    ctx = SimpleNamespace(deps=SimpleNamespace(components={key: _node(key) for key in keys}))
    from pydantic_ai import ModelRetry

    with pytest.raises(ModelRetry) as error:
        asyncio.run(read_code_components(ctx, ["Service"]))
    assert all(key in str(error.value) for key in keys)


def test_repository_directory_view_lists_unicode_paths_without_shell(tmp_path):
    from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor

    (tmp_path / "中文目录").mkdir()
    (tmp_path / "中文目录" / "Service.java").write_text("class Service {}", encoding="utf-8")
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            registry={}, absolute_docs_path=str(tmp_path), absolute_repo_path=str(tmp_path)
        )
    )
    result = asyncio.run(str_replace_editor(ctx, working_dir="repo", command="view", path="."))
    assert "Service.java" in result
    assert "中文目录" in result
