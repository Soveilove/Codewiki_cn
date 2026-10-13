# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""New behaviour from the live run: ownership, salvage, batching, sub-topics."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from codewiki.src.be import business_clustering as business
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.config import Config


def _node(cid, source="class Example {}", kind="class"):
    path, _, name = cid.partition("::")
    return Node(
        id=cid,
        name=name,
        component_type=kind,
        relative_path=path,
        file_path=path,
        source_code=source,
        depends_on=set(),
    )


def _config(tmp_path, **kwargs):
    return Config.from_cli(
        repo_path=str(tmp_path),
        output_dir=str(tmp_path),
        llm_base_url="http://localhost:1/v1",
        llm_api_key="k",
        main_model="m",
        cluster_model="m",
        agent_instructions={"language": "zh"},
        **kwargs,
    )


# --------------------------------------------------------------------- salvage


def test_salvage_recovers_complete_topics_from_truncated_response():
    truncated = (
        '{"订单": {"purpose": "订单处理", "components": ["C0", "C1"]}, '
        '"学员": {"purpose": "学员管理", "components": ["C2"]}, '
        '"停课": {"purpose": "停课流程", "compon'
    )
    salvaged = business.salvage_partial_json(truncated)
    assert set(salvaged) == {"订单", "学员"}
    assert salvaged["订单"]["components"] == ["C0", "C1"]


def test_salvage_handles_escaped_quotes_in_topic_name():
    truncated = '{"a\\"b": {"purpose": "p", "components": ["C0"]}, "x": {"pur'
    salvaged = business.salvage_partial_json(truncated)
    # The key is captured as raw source text; only its VALUE must decode.
    assert salvaged and salvaged['a\\"b'] == {"purpose": "p", "components": ["C0"]}


def test_salvage_returns_none_when_nothing_complete():
    assert business.salvage_partial_json('{"broken": {"pur') is None
    assert business.salvage_partial_json("") is None
    assert business.salvage_partial_json(None) is None


def test_truncated_planning_response_still_assigns(tmp_path, monkeypatch):
    """A response that stops mid-object must not lose its complete topics."""
    monkeypatch.setattr(business, "count_tokens", lambda text: 50000 if text == "large" else 10)
    ids = ["a/A.java::A", "b/B.java::B", "c/C.java::C"]
    components = {cid: _node(cid, "large") for cid in ids}

    def complete(prompt):
        return (
            '<GROUPED_COMPONENTS>{"订单": {"purpose": "订单处理", "components": ["C0"]}, '
            '"学员": {"purpose": "学员管理", "components": ["C1", "C2"]}, '
            '"停课": {"purpose": "停课流程", "compone'
        )

    tree = business.cluster_business_modules(ids, components, _config(tmp_path), complete)
    # Complete topics survived; the truncated one is repaired in a later round.
    assert "订单" in tree and "学员" in tree
    assert "C0" not in [r for i in tree.values() for r in i["components"]]
    owned = [c for i in tree.values() for c in i["components"]]
    assert set(owned) == set(ids)


# ------------------------------------------------------------------- ownership


def test_shared_component_gets_one_primary_and_marked_references():
    ids = ["dto/Event.java::Event", "svc/A.java::A", "svc/B.java::B"]
    tree = {
        "订单主题": {"purpose": "订单", "components": [ids[0], ids[1]]},
        "学员主题": {"purpose": "学员", "components": [ids[0], ids[2]]},
    }
    resolved = business.resolve_primary_ownership(tree, ids)
    assert resolved["订单主题"]["components"] == [ids[0], ids[1]]
    assert resolved["订单主题"]["components_shared_with"] == [(ids[0], "学员主题")]
    assert resolved["学员主题"]["components"] == [ids[2]]
    assert resolved["学员主题"]["shared_components"] == [ids[0]]
    owned = [c for i in resolved.values() for c in i["components"]]
    assert len(owned) == len(set(owned)) == len(ids)


def test_ownership_never_loses_a_component():
    with pytest.raises(business.BusinessClusteringError, match="missing repository IDs"):
        business.resolve_primary_ownership(
            {"A": {"components": ["only.java::Only"]}}, ["only.java::Only", "gone.java::Gone"]
        )


def test_document_prompt_explains_shared_components(tmp_path, monkeypatch):
    from codewiki.src.be.prompt_template import format_user_prompt

    cid = "dto/Event.java::Event"
    components = {cid: _node(cid)}
    tree = {
        "订单": {
            "purpose": "订单处理",
            "components": [],
            "children": {
                "子主题": {
                    "purpose": "订单子主题",
                    "components": [cid],
                    "children": {},
                    "shared_components": [cid],
                    "components_shared_with": [(cid, "学员")],
                },
            },
        }
    }
    (tmp_path / "Event.java").write_text("class Event {}", encoding="utf-8")
    components[cid].file_path = str(tmp_path / "Event.java")
    prompt = format_user_prompt(
        "子主题",
        [cid],
        components,
        tree,
        ["订单", "子主题"],
        layout="hierarchical",
        business_analysis=True,
        required_source_ids=set(),
        prompt_read_ids=set(),
    )
    assert "SHARED_COMPONENTS" in prompt
    assert "NOT yours to" in prompt
    assert "订单子主题" in prompt


def test_near_identical_topic_names_are_merged(tmp_path, monkeypatch):
    """The model sometimes splits one topic into two names differing only in spacing."""
    monkeypatch.setattr(business, "count_tokens", lambda t: 50000 if t == "large" else 10)
    ids = ["a/A.java::A", "b/B.java::B"]
    components = {cid: _node(cid, "large") for cid in ids}

    def complete(prompt):
        return json.dumps(
            {
                "跨业务线接口转发": {"purpose": "转发", "components": ["C0"]},
                "跨业务线接口 转发": {"purpose": "转发", "components": ["C1"]},
            },
            ensure_ascii=False,
        )

    tree = business.cluster_business_modules(ids, components, _config(tmp_path), complete)
    assert len(tree) == 1, f"expected one merged topic, got {list(tree)}"
    owned = [c for i in tree.values() for c in i["components"]]
    assert set(owned) == set(ids)


def test_canonical_topic_name_ignores_spacing_only():
    tree = {"跨业务线接口转发": {}}
    assert business.canonical_topic_name(tree, "跨业务线接口 转发") == "跨业务线接口转发"
    assert business.canonical_topic_name(tree, "请假与考勤") == "请假与考勤"
    assert business.canonical_topic_name({}, "新主题") == "新主题"


def test_sub_topic_ownership_is_unique_and_marked():
    from codewiki.src.be.agent_tools.generate_sub_module_documentations import (
        _resolve_sub_topic_ownership,
    )

    specs = {"班级查询": ["a::A", "b::B"], "停课次数": ["b::B", "c::C"]}
    children = {name: {"components": list(ids)} for name, ids in specs.items()}
    _resolve_sub_topic_ownership(children, specs)
    assert children["班级查询"]["components"] == ["a::A", "b::B"]
    assert "shared_components" not in children["班级查询"]
    assert children["停课次数"]["components"] == ["c::C"]
    assert children["停课次数"]["shared_components"] == ["b::B"]
    assert children["班级查询"]["components_shared_with"] == [("b::B", "停课次数")]
    owned = [c for n in children.values() for c in n["components"]]
    assert len(owned) == len(set(owned)) == 3


def test_recursive_sub_topic_inherits_parent_purpose(tmp_path, monkeypatch):
    from codewiki.src.be.agent_tools import generate_sub_module_documentations as sub

    cid = "svc/A.java::A"
    tree = {"父主题": {"components": [cid], "children": {}}}
    deps = SimpleNamespace(
        current_module_name="父主题",
        components={cid: _node(cid)},
        module_tree=tree,
        absolute_docs_path=str(tmp_path),
        path_to_current_module=["父主题"],
        current_depth=1,
        max_depth=3,
        custom_instructions="",
        config=_config(tmp_path, layout="flat"),
        generation_runtime=None,
    )

    class Agent:
        def __init__(self, *a, **k):
            pass

        async def run(self, *a, **k):
            return SimpleNamespace(output="ok")

    monkeypatch.setattr(sub, "Agent", Agent)
    monkeypatch.setattr(sub, "create_fallback_models", lambda config: None)
    monkeypatch.setattr(sub, "format_leaf_system_prompt", lambda *a, **k: "s")
    monkeypatch.setattr(sub, "format_user_prompt", lambda *a, **k: "p")
    monkeypatch.setattr(sub, "sub_module_report", lambda *a, **k: "done")
    monkeypatch.setattr(
        sub,
        "plan_sub_module_specs",
        lambda *a, **k: SimpleNamespace(name_map={"子主题": "子主题"}, skipped={}),
    )
    asyncio.run(
        sub.generate_sub_module_documentation(SimpleNamespace(deps=deps), {"子主题": [cid]})
    )
    child = tree["父主题"]["children"]["子主题"]
    assert "父主题" in child["purpose"]


# -------------------------------------------------------------------- batching


def test_planning_batch_respects_reference_cap(tmp_path, monkeypatch):
    original = business.count_tokens
    monkeypatch.setattr(business, "count_tokens", lambda t: 50000 if t == "large" else original(t))
    ids = [f"layer{i}/T{i}.java::T{i}" for i in range(200)]
    components = {cid: _node(cid, "large") for cid in ids}
    sizes = []

    def complete(prompt):
        rows = json.loads(prompt.split("<INPUT_EVIDENCE>\n")[1].split("\n</INPUT_EVIDENCE>")[0])
        sizes.append(len(rows))
        return json.dumps({"主题": {"purpose": "p", "components": [r["ref"] for r in rows]}})

    tree = business.cluster_business_modules(
        ids, components, _config(tmp_path, max_planning_refs_per_call=25), complete
    )
    assert max(sizes) <= 25
    assert len(sizes) > 1
    owned = [c for i in tree.values() for c in i["components"]]
    assert set(owned) == set(ids)
