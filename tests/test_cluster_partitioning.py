# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Tests for structure-based clustering batches and None-robust LLM handling.

Covers partition_leaf_nodes_by_structure (directory splitting, chunking of
unsplittable groups, coalescing, determinism), the batched cluster_modules
flow (flat merge, name-collision union, per-batch fallback), and the guards
that turn an empty/None LLM response into a graceful fallback instead of a
TypeError (issue seen on the electron run: output truncated at max_tokens,
provider returned content: null).
"""

from __future__ import annotations

from types import SimpleNamespace

from codewiki.src.be.cluster_modules import (
    _batch_fallback_name,
    _cluster_batch_fits,
    _merge_module_trees,
    _parse_super_group_response,
    cluster_modules,
    partition_leaf_nodes_by_structure,
    super_group_modules,
)
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.llm_services import _extract_content
from codewiki.src.config import Config


def _make_node(component_id: str, source_code: str = "code") -> Node:
    rel_path, _, name = component_id.partition("::")
    return Node(
        id=component_id,
        name=name or component_id,
        component_type="class",
        file_path=rel_path,
        relative_path=rel_path,
        source_code=source_code,
    )


def _components(component_ids: list[str]) -> dict[str, Node]:
    return {cid: _make_node(cid) for cid in component_ids}


def _config(**overrides) -> Config:
    kwargs = dict(
        repo_path="/tmp/repo",
        output_dir="/tmp/out",
        llm_base_url="http://localhost",
        llm_api_key="key",
        main_model="m",
        cluster_model="m",
    )
    return Config.from_cli(**kwargs, **overrides)


def _max_count_fits(limit: int):
    """A fits() predicate on node count only — keeps tests tokenizer-free."""
    return lambda ids: len(ids) <= limit


# ---------------------------------------------------------------------------
# partition_leaf_nodes_by_structure
# ---------------------------------------------------------------------------

def test_partial_clustering_keeps_every_input_component():
    from codewiki.src.be.cluster_modules import collect_module_tree_component_ids

    ids = ["api/a.py::A", "api/b.py::B", "store/c.py::C"]
    tree = cluster_modules(
        ids, _components(ids), _config(max_token_per_module=1),
        completer=lambda _: '<GROUPED_COMPONENTS>{"API":{"components":["api/a.py::A"]}}</GROUPED_COMPONENTS>',
    )
    assert collect_module_tree_component_ids(tree) == set(ids)


def test_cached_coverage_repair_preserves_names_and_is_idempotent():
    from copy import deepcopy
    from codewiki.src.be.cluster_modules import ensure_component_coverage, collect_module_tree_component_ids

    ids = ["api/a.py::A", "api/b.py::B", "store/c.py::C"]
    original = {"中文旧模块": {"components": [ids[0]], "children": {}}}
    tree = deepcopy(original)
    cfg = _config(max_leaf_nodes_per_cluster=1)
    ensure_component_coverage(tree, ids, _components(ids), cfg)
    assert tree["中文旧模块"] == original["中文旧模块"]
    assert collect_module_tree_component_ids(tree) == set(ids)
    repaired = deepcopy(tree)
    ensure_component_coverage(tree, ids, _components(ids), cfg)
    assert tree == repaired
    assert all(len(info["components"]) <= 1 for info in tree.values())


def test_windows_partition_preserves_directory_boundaries():
    ids = [r"alpha\a.py::A", r"alpha\b.py::B", r"beta\a.py::C", r"beta\b.py::D"]
    batches = partition_leaf_nodes_by_structure(ids, _components(ids), _max_count_fits(3))
    assert batches == [ids[:2], ids[2:]]


def test_partition_single_batch_when_everything_fits():
    ids = [f"lib/a.py::C{i}" for i in range(5)]
    components = _components(ids)
    batches = partition_leaf_nodes_by_structure(ids, components, _max_count_fits(10))
    assert batches == [sorted(ids)]


def test_partition_drops_unknown_ids():
    ids = ["lib/a.py::A", "lib/b.py::B"]
    components = _components(ids)
    batches = partition_leaf_nodes_by_structure(
        ids + ["gone/x.py::X"], components, _max_count_fits(10)
    )
    assert batches == [ids]


def test_partition_splits_by_top_level_directory():
    ids = [f"alpha/f{i}.py::A{i}" for i in range(4)] + [
        f"beta/f{i}.py::B{i}" for i in range(4)
    ]
    components = _components(ids)
    batches = partition_leaf_nodes_by_structure(ids, components, _max_count_fits(4))
    assert len(batches) == 2
    assert all(len(batch) == 4 for batch in batches)
    # Path-sorted: alpha before beta.
    assert all(cid.startswith("alpha/") for cid in batches[0])
    assert all(cid.startswith("beta/") for cid in batches[1])


def test_partition_recurses_into_oversized_directory():
    ids = [f"pkg/sub{i // 3}/f{i}.py::C{i}" for i in range(9)]
    components = _components(ids)
    batches = partition_leaf_nodes_by_structure(ids, components, _max_count_fits(3))
    assert all(len(batch) <= 3 for batch in batches)
    union = sorted(cid for batch in batches for cid in batch)
    assert union == sorted(ids)


def test_partition_chunks_unsplittable_single_file():
    ids = [f"one/file.py::C{i:02d}" for i in range(10)]
    components = _components(ids)
    batches = partition_leaf_nodes_by_structure(ids, components, _max_count_fits(4))
    assert all(len(batch) <= 4 for batch in batches)
    union = [cid for batch in batches for cid in batch]
    assert sorted(union) == sorted(ids)


def test_partition_coalesces_root_files_and_is_deterministic():
    ids = (
        ["setup.py::Setup", "conftest.py::Conf"]
        + [f"src/f{i}.py::S{i}" for i in range(3)]
    )
    components = _components(ids)
    first = partition_leaf_nodes_by_structure(ids, components, _max_count_fits(4))
    second = partition_leaf_nodes_by_structure(
        list(reversed(ids)), components, _max_count_fits(4)
    )
    assert first == second
    assert all(len(batch) <= 4 for batch in first)
    # Root-level files must not sit alone in single-node batches.
    assert all(len(batch) > 1 for batch in first)
    union = sorted(cid for batch in first for cid in batch)
    assert union == sorted(ids)


def test_cluster_batch_fits_respects_node_count_and_token_budget():
    config = _config(max_leaf_nodes_per_cluster=2, max_tokens=64000)
    assert _cluster_batch_fits(["a/b.py::X"], config)
    assert not _cluster_batch_fits(["a/b.py::X", "a/c.py::Y", "a/d.py::Z"], config)
    # Tiny max_tokens shrinks the output budget below even a few IDs.
    tight = _config(max_leaf_nodes_per_cluster=600, max_tokens=1)
    long_ids = ["dir/" + "verylongsegment" * 400 + f".py::C{i}" for i in range(3)]
    assert not _cluster_batch_fits(long_ids, tight)


# ---------------------------------------------------------------------------
# None-robustness
# ---------------------------------------------------------------------------

def test_cluster_modules_none_response_returns_empty_tree():
    ids = [f"lib/f{i}.py::C{i}" for i in range(3)]
    components = _components(ids)
    config = _config(max_token_per_module=1)
    result = cluster_modules(
        ids, components, config, {}, None, [], completer=lambda prompt: None
    )
    assert result == {}


def test_parse_super_group_response_handles_none_and_garbage():
    assert _parse_super_group_response(None) is None
    assert _parse_super_group_response("") is None
    assert _parse_super_group_response("no tags here") is None


def test_super_group_modules_none_response_keeps_tree():
    tree = {
        f"Module{i}": {"path": f"m{i}", "components": [f"m{i}/f.py::C"]}
        for i in range(5)
    }
    config = _config()
    assert super_group_modules(tree, config, completer=lambda prompt: None) == tree


def test_extract_content_returns_none_on_truncated_empty_response():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None), finish_reason="length"
            )
        ]
    )
    assert _extract_content(response, "test-model") is None
    ok = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="hi"), finish_reason="stop")
        ]
    )
    assert _extract_content(ok, "test-model") == "hi"


# ---------------------------------------------------------------------------
# Batched clustering end-to-end (scripted completer)
# ---------------------------------------------------------------------------

def _grouped(tree: dict) -> str | None:
    return f"<GROUPED_COMPONENTS>{tree!r}</GROUPED_COMPONENTS>"


def test_batched_clustering_merges_batches_and_collisions():
    alpha = [f"alpha/f{i}.py::A{i}" for i in range(3)]
    beta = [f"beta/f{i}.py::B{i}" for i in range(3)]
    components = _components(alpha + beta)
    config = _config(max_token_per_module=1, max_leaf_nodes_per_cluster=3)

    def completer(prompt: str) -> str | None:
        if "<MODULE_TREE>" in prompt:
            return None  # recursive module-level calls: decline so recursion stops
        in_alpha = alpha[0] in prompt
        in_beta = beta[0] in prompt
        if in_alpha and not in_beta:
            return _grouped({
                "Alpha": {"path": "alpha", "components": alpha[:2]},
                "Shared": {"path": "alpha", "components": [alpha[2]]},
            })
        if in_beta and not in_alpha:
            return _grouped({
                "Beta": {"path": "beta", "components": beta[:2]},
                "Shared": {"path": "beta", "components": [beta[2]]},
            })
        return None

    tree = cluster_modules(
        alpha + beta, components, config, {}, None, [], completer=completer
    )
    assert set(tree) == {"Alpha", "Shared", "Beta"}
    # Name collision across batches merges components and generalizes the path.
    assert tree["Shared"]["components"] == [alpha[2], beta[2]]
    assert tree["Shared"]["path"] == ""
    assert tree["Alpha"]["components"] == alpha[:2]


def test_batched_clustering_failed_batch_becomes_fallback_module():
    alpha = [f"alpha/f{i}.py::A{i}" for i in range(3)]
    beta = [f"beta/f{i}.py::B{i}" for i in range(3)]
    components = _components(alpha + beta)
    config = _config(max_token_per_module=1, max_leaf_nodes_per_cluster=3)

    def completer(prompt: str) -> str | None:
        if "<MODULE_TREE>" in prompt:
            return None  # recursive module-level calls: decline so recursion stops
        if beta[0] in prompt and alpha[0] not in prompt:
            return None  # this batch's call "truncates"
        if alpha[0] in prompt and beta[0] not in prompt:
            return _grouped({
                "Alpha": {"path": "alpha", "components": alpha[:2]},
                "AlphaExtra": {"path": "alpha", "components": [alpha[2]]},
            })
        return None

    tree = cluster_modules(
        alpha + beta, components, config, {}, None, [], completer=completer
    )
    assert "Alpha" in tree and "AlphaExtra" in tree
    fallback = tree["beta"]
    assert fallback["components"] == beta
    assert fallback["path"] == "beta"


def test_merge_module_trees_and_fallback_name_helpers():
    target = {"M": {"path": "a/b", "components": ["a/b/x.py::X"]}}
    _merge_module_trees(
        target, {"M": {"path": "a/c", "components": ["a/c/y.py::Y", "a/b/x.py::X"]}}
    )
    assert target["M"]["components"] == ["a/b/x.py::X", "a/c/y.py::Y"]
    assert target["M"]["path"] == "a"

    ids = ["pkg/mod/f.py::A", "pkg/mod/g.py::B"]
    components = _components(ids)
    assert _batch_fallback_name(ids, components, {}) == "pkg_mod"
    assert _batch_fallback_name(ids, components, {"pkg_mod": {}}) == "pkg_mod_2"


def test_malformed_clustering_regenerates_once_without_losing_batch(caplog):
    from codewiki.src.be.cluster_modules import _cluster_via_llm

    ids = ["src/a.py::A", "src/b.py::B"]
    calls = []
    responses = iter([
        '<GROUPED_COMPONENTS>{"构建": {"path": ".", "components": []}]</GROUPED_COMPONENTS>',
        _grouped({"构建": {"path": "src", "components": ids}}),
    ])

    def complete(prompt):
        calls.append(prompt)
        return next(responses)

    tree = _cluster_via_llm(ids, _components(ids), _config(), {}, None, "batch 1/2", complete)
    assert tree["构建"]["components"] == ids
    assert len(calls) == 2
    assert "Regenerate the grouping" in calls[1]
    assert all(cid in calls[1] for cid in ids)
    assert "regenerating once" in caplog.text
    assert "Traceback" not in caplog.text


def test_invalid_clustering_shape_falls_back_after_bounded_retry():
    from codewiki.src.be.cluster_modules import _cluster_via_llm

    ids = ["src/a.py::A"]
    calls = []

    def complete(prompt):
        calls.append(prompt)
        return '<GROUPED_COMPONENTS>{"api": {"components": "not a list"}}</GROUPED_COMPONENTS>'

    assert _cluster_via_llm(ids, _components(ids), _config(), {}, None, "repo", complete) == {}
    assert len(calls) == 2


def test_cluster_parser_accepts_fenced_json_and_legacy_literals():
    import json
    from codewiki.src.be.cluster_modules import _parse_cluster_response

    tree = {"构建": {"path": ".", "components": ["config.json::config.json"], "extra": None}}
    response = '<GROUPED_COMPONENTS>\n```json\n' + json.dumps(tree) + '\n```\n</GROUPED_COMPONENTS>'
    assert _parse_cluster_response(response) == tree
    assert _parse_cluster_response(_grouped(tree)) == tree


def test_cluster_parser_never_executes_response(tmp_path):
    import pytest
    from codewiki.src.be.cluster_modules import _parse_cluster_response

    target = tmp_path / "must-not-exist"
    response = f'<GROUPED_COMPONENTS>__import__("pathlib").Path({str(target)!r}).touch()</GROUPED_COMPONENTS>'
    with pytest.raises(ValueError):
        _parse_cluster_response(response)
    assert not target.exists()


def test_cluster_parser_accepts_complete_result_without_envelope():
    import json
    from codewiki.src.be.cluster_modules import _parse_cluster_response

    tree = {"接口": {"path": "src", "components": ["src/a.py::A"]}}
    body = json.dumps(tree, ensure_ascii=False)
    responses = [body, "```json\n" + body + "\n```", "分组结果：\n" + body + "\n分组完成。",
                 "分组结果：\n```json\n" + body + "\n```\n完成。",
                 "<GROUPED_COMPONENTS>\n" + body, body + "\n</GROUPED_COMPONENTS>", repr(tree)]
    for response in responses:
        assert _parse_cluster_response(response) == tree


def test_cluster_parser_rejects_truncation_multiple_results_and_wrong_shape():
    import pytest
    from codewiki.src.be.cluster_modules import _parse_cluster_response

    for response in ["没有结果", '{"api": {"components": ["a.py::A"]}',
                     '{"api": {"components": "a.py::A"}}',
                     '结果：{"api": {"components": []}}\n{"other": {"components": []}}',
                     '```json\n{}\n```\n```json\n{}\n```']:
        with pytest.raises((ValueError, SyntaxError)):
            _parse_cluster_response(response)


def test_clustering_without_tags_does_not_regenerate(caplog):
    import json
    from codewiki.src.be.cluster_modules import _cluster_via_llm

    ids = ["src/a.py::A"]
    calls = []
    def complete(prompt):
        calls.append(prompt)
        return json.dumps({"接口": {"components": ids}})
    tree = _cluster_via_llm(ids, _components(ids), _config(), {}, None, "batch 2/3", complete)
    assert tree["接口"]["components"] == ids
    assert len(calls) == 1
    assert "regenerating" not in caplog.text


def test_cluster_correction_request_failure_falls_back():
    from codewiki.src.be.cluster_modules import _cluster_via_llm

    ids = ["src/a.py::A"]
    calls = []

    def complete(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return "invalid"
        raise RuntimeError("provider unavailable")

    assert _cluster_via_llm(ids, _components(ids), _config(), {}, None, "repo", complete) == {}
    assert len(calls) == 2


def test_incomplete_clustering_regenerates_with_missing_ids(caplog):
    from codewiki.src.be.cluster_modules import _cluster_via_llm

    ids = ["src/a.py::A", "src/b.py::B", "src/c.py::C"]
    responses = iter([
        _grouped({"接口": {"components": ids[:1]}}),
        _grouped({"接口": {"components": ids}}),
    ])
    calls = []

    def complete(prompt):
        calls.append(prompt)
        return next(responses)

    tree = _cluster_via_llm(ids, _components(ids), _config(), {}, None, "repo", complete)
    assert tree == {"接口": {"components": ids}}
    assert len(calls) == 2
    missing = calls[1].split("<UNASSIGNED_COMPONENT_IDS>")[1]
    assert ids[0] not in missing
    assert all(cid in missing for cid in ids[1:])
    assert "COMPLETE grouping" in calls[1]
    assert "regenerating once" in caplog.text
    assert "聚类覆盖不足" not in caplog.text


def test_incomplete_retry_retains_best_grouping_and_repairs_coverage(caplog):
    from codewiki.src.be.cluster_modules import _cluster_via_llm, collect_module_tree_component_ids

    ids = ["src/a.py::A", "src/b.py::B", "src/c.py::C"]
    first = {"原分组": {"components": ids[:2]}}
    retries = [
        _grouped({"更差分组": {"components": ids[:1]}}),
        '<GROUPED_COMPONENTS>{"api": {"components": []}}}</GROUPED_COMPONENTS>',
        None,
        RuntimeError("provider unavailable"),
    ]
    for retry in retries:
        calls = []

        def complete(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return _grouped(first)
            if isinstance(retry, Exception):
                raise retry
            return retry

        tree = _cluster_via_llm(ids, _components(ids), _config(), {}, None, "repo", complete)
        assert tree["原分组"]["components"] == ids[:2]
        assert "更差分组" not in tree
        assert collect_module_tree_component_ids(tree) == set(ids)
        assert len(calls) == 2
    assert "聚类覆盖不足" in caplog.text


def test_incomplete_retry_uses_better_grouping_without_third_request():
    from codewiki.src.be.cluster_modules import _cluster_via_llm, collect_module_tree_component_ids

    ids = ["src/a.py::A", "src/b.py::B", "src/c.py::C"]
    responses = iter([
        _grouped({"旧分组": {"components": ids[:1]}}),
        _grouped({"新分组": {"components": ids[:2]}}),
    ])
    tree = _cluster_via_llm(
        ids, _components(ids), _config(), {}, None, "repo", lambda _: next(responses)
    )
    assert "旧分组" not in tree
    assert tree["新分组"]["components"] == ids[:2]
    assert collect_module_tree_component_ids(tree) == set(ids)


def test_clustering_rejects_ids_from_other_batch_and_retries():
    from codewiki.src.be.cluster_modules import _cluster_via_llm

    ids = ["src/a.py::A", "src/b.py::B"]
    responses = iter([
        _grouped({"错误批次": {"components": ids[1:]}}),
        _grouped({"当前批次": {"components": ids[:1]}}),
    ])
    tree = _cluster_via_llm(
        ids[:1], _components(ids), _config(), {}, None, "repo", lambda _: next(responses)
    )
    assert tree == {"当前批次": {"components": ids[:1]}}


def test_short_reference_clustering_covers_369_components_in_one_request(caplog):
    import json
    from codewiki.src.be.cluster_modules import _cluster_via_llm, collect_module_tree_component_ids

    ids = [f"src/business/customer/handlers/f{i}.py::Handler{i}" for i in range(369)]
    calls = []

    def complete(prompt):
        calls.append(prompt)
        assert all(f"C{i} = {cid}" in prompt for i, cid in enumerate(ids))
        return json.dumps({
            "客户业务": {"path": "src/business/customer", "components": [f"C{i}" for i in range(368, -1, -1)]},
        }, ensure_ascii=False)

    tree = _cluster_via_llm(ids, _components(ids), _config(), {}, None, "repo", complete)
    assert tree["客户业务"]["components"] == list(reversed(ids))
    assert collect_module_tree_component_ids(tree) == set(ids)
    assert len(calls) == 1
    assert "regenerating" not in caplog.text
    assert "聚类覆盖不足" not in caplog.text


def test_short_reference_retry_keeps_original_numbering_for_missing_components():
    from codewiki.src.be.cluster_modules import _cluster_via_llm

    ids = ["src/a.py::A", "src/b.py::B", "src/c.py::C"]
    calls = []

    def complete(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return _grouped({"接口": {"components": ["C0", "C2"]}})
        missing = prompt.split("<UNASSIGNED_COMPONENT_IDS>")[1]
        assert f"C1 = {ids[1]}" in missing
        assert "C0 =" not in missing
        assert "C2 =" not in missing
        return _grouped({"接口": {"components": ["C2", "C1", "C0"]}})

    tree = _cluster_via_llm(ids, _components(ids), _config(), {}, None, "repo", complete)
    assert tree["接口"]["components"] == list(reversed(ids))
    assert len(calls) == 2


def test_unknown_short_reference_regenerates_without_guessing(caplog):
    from codewiki.src.be.cluster_modules import _cluster_via_llm

    ids = ["src/a.py::A", "src/b.py::B"]
    responses = iter([
        _grouped({"接口": {"components": ["C0", "C99"]}}),
        _grouped({"接口": {"components": ["C0", "C1"]}}),
    ])
    tree = _cluster_via_llm(ids, _components(ids), _config(), {}, None, "repo", lambda _: next(responses))
    assert tree["接口"]["components"] == ids
    assert "Unknown component references: C99" in caplog.text
    assert "Skipping invalid leaf node" not in caplog.text


def test_short_reference_duplicate_module_assignment_is_rejected(caplog):
    from codewiki.src.be.cluster_modules import _cluster_via_llm

    ids = ["src/a.py::A", "src/b.py::B"]
    responses = iter([
        _grouped({"接口": {"components": ["C0", "C1"]}, "重复": {"components": ["C0"]}}),
        _grouped({"接口": {"components": ["C0"]}, "业务": {"components": ["C1"]}}),
    ])
    tree = _cluster_via_llm(ids, _components(ids), _config(), {}, None, "repo", lambda _: next(responses))
    assert tree == {"接口": {"components": ids[:1]}, "业务": {"components": ids[1:]}}
    assert "assigned to multiple modules" in caplog.text


def test_short_reference_partial_response_repairs_with_real_ids_only():
    from codewiki.src.be.cluster_modules import _cluster_via_llm, collect_module_tree_component_ids

    ids = ["src/a.py::A", "src/b.py::B", "src/c.py::C"]
    responses = iter([
        _grouped({"接口": {"components": ["C0", "C2"]}}),
        _grouped({"错误": {"components": ["C999"]}}),
    ])
    tree = _cluster_via_llm(ids, _components(ids), _config(), {}, None, "repo", lambda _: next(responses))
    assert tree["接口"]["components"] == [ids[0], ids[2]]
    assert collect_module_tree_component_ids(tree) == set(ids)
    assert all(cid in ids for info in tree.values() for cid in info["components"])


def test_short_references_are_local_to_each_batch_and_recursive_request():
    alpha = [f"alpha/f{i}.py::A{i}" for i in range(3)]
    beta = [f"beta/f{i}.py::B{i}" for i in range(3)]
    config = _config(max_token_per_module=1, max_leaf_nodes_per_cluster=3)

    def complete(prompt):
        if "<MODULE_TREE>" in prompt:
            return None
        assert (f"C0 = {alpha[0]}" in prompt) != (f"C0 = {beta[0]}" in prompt)
        return _grouped({
            "共享入口": {"components": ["C0", "C2"]},
            "共享业务": {"components": ["C1"]},
        })

    tree = cluster_modules(alpha + beta, _components(alpha + beta), config, completer=complete)
    assert tree["共享入口"]["components"] == [alpha[0], alpha[2], beta[0], beta[2]]
    assert tree["共享业务"]["components"] == [alpha[1], beta[1]]


def test_cluster_batch_output_budget_never_exceeds_configured_limit():
    assert not _cluster_batch_fits(["src/a.py::A"], _config(max_tokens=1))
