# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Business ownership is recovered from parsed source, never package proximity."""

import json
from types import SimpleNamespace

import pytest

from codewiki.src.be import business_clustering as business
from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser


def _parse(tmp_path, sources):
    for name, source in sources.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    return DependencyParser(str(tmp_path)).parse_repository()


def _inputs(prompt):
    return json.loads(prompt.split("<INPUT_EVIDENCE>\n")[1].split("\n</INPUT_EVIDENCE>")[0])


def _response(topic, refs):
    return json.dumps({topic: {"purpose": topic, "components": refs}}, ensure_ascii=False)


def _config(monkeypatch, components):
    original = business.count_tokens
    sources = {node.source_code for node in components.values()}
    monkeypatch.setattr(
        business, "count_tokens", lambda text: 50000 if text in sources else original(text)
    )
    return SimpleNamespace(
        language="zh",
        max_token_per_module=12000,
        max_tokens=4000,
        max_leaf_nodes_per_cluster=80,
        max_planning_refs_per_call=80,
    )


def test_omitted_request_and_response_inherit_their_actual_consumer(tmp_path, monkeypatch):
    components = _parse(
        tmp_path,
        {
            "SearchUserReqDTO.java": "package sample; class SearchUserReqDTO { String query; }",
            "SearchUserRespDTO.java": "package sample; class SearchUserRespDTO { Long id; }",
            "StudentInfoService.java": """package sample;
import java.util.List;
class StudentInfoService {
    public List<SearchUserRespDTO> search(SearchUserReqDTO request) { return null; }
}
""",
        },
    )
    ids = [cid for cid, node in components.items() if node.component_type == "class"]
    calls = []

    def complete(prompt):
        calls.append(prompt)
        return _response(
            "学员查询", [r["ref"] for r in _inputs(prompt) if "::StudentInfoService" in r["id"]]
        )

    tree = business.cluster_business_modules(
        ids, components, _config(monkeypatch, components), complete
    )
    assert tree["学员查询"]["components"] == [
        "StudentInfoService.java::StudentInfoService",
        "SearchUserReqDTO.java::SearchUserReqDTO",
        "SearchUserRespDTO.java::SearchUserRespDTO",
    ]
    assert set(tree) == {"学员查询"}
    assert len(calls) == 1


def test_shared_payload_repair_sees_both_consumers_not_a_majority_guess(tmp_path, monkeypatch):
    components = _parse(
        tmp_path,
        {
            "Event.java": "package sample; class Event { Long id; }",
            "Orders.java": "package sample; class Orders { void publish(Event event) {} }",
            "Students.java": "package sample; class Students { void consume(Event event) {} }",
        },
    )
    ids = [cid for cid, node in components.items() if node.component_type == "class"]
    calls = []

    def complete(prompt):
        rows = _inputs(prompt)
        calls.append(rows)
        if len(calls) == 1:
            return json.dumps(
                {
                    "订单事件": {
                        "purpose": "生成订单事件",
                        "components": [r["ref"] for r in rows if "::Orders" in r["id"]],
                    },
                    "学员事件接收": {
                        "purpose": "处理事件影响",
                        "components": [r["ref"] for r in rows if "::Students" in r["id"]],
                    },
                },
                ensure_ascii=False,
            )
        row = rows[0]
        assert row["id"] == "Event.java::Event"
        usages = row["usage_evidence"]
        assert {u["topic"] for u in usages} == {"订单事件", "学员事件接收"}
        assert any("publish(Event event)" in u["source_excerpt"] for u in usages)
        assert any("consume(Event event)" in u["source_excerpt"] for u in usages)
        return _response("订单事件", [row["ref"]])

    tree = business.cluster_business_modules(
        ids, components, _config(monkeypatch, components), complete
    )
    assert "Event.java::Event" in tree["订单事件"]["components"]
    assert "Event.java::Event" in tree["学员事件接收"]["shared_components"]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "sources,omitted",
    [
        ({"Together.java": "package sample; class Owner {} class Independent {}"}, "Independent"),
        (
            {
                "Owner.java": "package sample; class Owner {}",
                "Independent.java": "package sample; class Independent {}",
            },
            "Independent",
        ),
        (
            {
                "Owner.java": "package sample; class Owner {}",
                "Independent.java": "package sample; class Independent { Owner tool; }",
            },
            "Independent",
        ),
        (
            {
                "Owner.java": "package sample; class Owner { Independent service; }",
                "Independent.java": "package sample; class Independent { void execute() {} }",
            },
            "Independent",
        ),
    ],
)
def test_uncertain_or_independent_responsibilities_are_not_force_assigned(
    tmp_path, monkeypatch, sources, omitted
):
    components = _parse(tmp_path, sources)
    ids = [cid for cid, node in components.items() if node.component_type == "class"]

    def complete(prompt):
        return _response(
            "已有能力", [r["ref"] for r in _inputs(prompt) if not r["id"].endswith("::" + omitted)]
        )

    with pytest.raises(business.BusinessClusteringError, match="unresolved repository IDs"):
        business.cluster_business_modules(
            ids, components, _config(monkeypatch, components), complete
        )


def test_a_normal_business_topic_may_contain_uncertainty_words(tmp_path, monkeypatch):
    components = _parse(tmp_path, {"Review.java": "package sample; class Review {}"})
    ids = list(components)
    tree = business.cluster_business_modules(
        ids,
        components,
        _config(monkeypatch, components),
        lambda prompt: _response("待确认订单审核", [r["ref"] for r in _inputs(prompt)]),
    )
    assert set(tree) == {"待确认订单审核"}


def test_stalled_repair_splits_missing_items_without_renumbering(tmp_path, monkeypatch):
    components = _parse(
        tmp_path, {f"T{i}.java": f"package sample; class T{i} {{}}" for i in range(4)}
    )
    ids = list(components)
    sizes = []
    seen_refs = {}

    def complete(prompt):
        rows = _inputs(prompt)
        sizes.append(len(rows))
        for row in rows:
            if row["id"] in seen_refs:
                assert row["ref"] == seen_refs[row["id"]]
            seen_refs[row["id"]] = row["ref"]
        if len(sizes) <= 2:
            return _response("业务处理", [])
        assert len(rows) <= 2
        return _response("业务处理", [row["ref"] for row in rows])

    tree = business.cluster_business_modules(
        ids, components, _config(monkeypatch, components), complete
    )
    assert sizes == [4, 4, 2, 2]
    assert set(tree["业务处理"]["components"]) == set(ids)


def test_later_batch_owns_an_earlier_omitted_data_contract(tmp_path, monkeypatch):
    components = _parse(
        tmp_path,
        {
            "Event.java": "package sample; class Event { Long id; }",
            "Worker.java": "package sample; class Worker { void handle(Event event) {} }",
        },
    )
    ids = [cid for cid, node in components.items() if node.component_type == "class"]
    config = _config(monkeypatch, components)
    config.max_planning_refs_per_call = 1
    calls = []

    def complete(prompt):
        rows = _inputs(prompt)
        calls.append(rows[0]["id"])
        return _response("事件处理", [row["ref"] for row in rows if "::Worker" in row["id"]])

    tree = business.cluster_business_modules(ids, components, config, complete)
    assert set(tree["事件处理"]["components"]) == set(ids)
    assert calls == ["Event.java::Event", "Worker.java::Worker"]


def test_nested_type_inherits_only_its_confirmed_parent(tmp_path, monkeypatch):
    components = _parse(
        tmp_path,
        {
            "Result.java": """package sample;
class Result {
    static class Detail { Long id; }
}
class Independent {}
"""
        },
    )
    ids = [cid for cid, node in components.items() if node.component_type == "class"]
    calls = []

    def complete(prompt):
        rows = _inputs(prompt)
        calls.append(rows)
        return json.dumps(
            {
                "查询结果": {
                    "purpose": "查询返回结构",
                    "components": [r["ref"] for r in rows if r["id"].endswith("::Result")],
                },
                "独立能力": {
                    "purpose": "另一独立声明",
                    "components": [r["ref"] for r in rows if r["id"].endswith("::Independent")],
                },
            },
            ensure_ascii=False,
        )

    tree = business.cluster_business_modules(
        ids, components, _config(monkeypatch, components), complete
    )
    assert "Result.java::Detail" in tree["查询结果"]["components"]
    assert "Result.java::Independent" not in tree["查询结果"]["components"]
    assert len(calls) == 1


def test_expanded_repair_respects_input_budget(tmp_path, monkeypatch):
    source = "package sample;\nclass Huge {\n" + "Long value;\n" * 500 + "}\n"
    components = _parse(tmp_path, {"Huge.java": source})
    config = _config(monkeypatch, components)
    config.max_token_per_module = 1700
    calls = []

    def complete(prompt):
        assert business.count_tokens(prompt) <= config.max_token_per_module
        rows = _inputs(prompt)
        calls.append(rows)
        return _response("长数据结构", [r["ref"] for r in rows] if len(calls) > 1 else [])

    tree = business.cluster_business_modules(list(components), components, config, complete)
    assert set(tree["长数据结构"]["components"]) == set(components)
    assert len(calls) == 2


def test_real_failed_entry_evidence_when_repository_is_supplied(monkeypatch):
    import os

    repo = os.environ.get("CODEWIKI_EVIDENCE_REPO")
    if not repo:
        pytest.skip("supply CODEWIKI_EVIDENCE_REPO for the read-only real-source audit")
    components = DependencyParser(repo).parse_repository()
    ids = business.select_business_components(list(components), components)
    relations = business._ownership_relations(ids, components)
    expected = {
        "SearchUserReqDTO",
        "StudentIdDTO",
        "UpdatePasswordDTO",
        "ConvertStuIdOrUuidRespDTO",
        "SearchUserRespDTO",
        "UserInfo",
        "StudentInfoForAdvisorDTO",
        "UserGeneralInformationRespDTO",
        "TaskCancelMqNotify",
        "ChildTaskTemplateComplete",
        "TaskTransferNotify",
        "TaskCompleteNoticeMsg",
    }
    targets = {
        node.name: cid
        for cid, node in components.items()
        if node.name in expected
        and (
            "business/studentinfo/" in node.relative_path
            or "business/task/dto/" in node.relative_path
        )
    }
    assert set(targets) == expected
    for name in expected - {"TaskCancelMqNotify", "TaskTransferNotify"}:
        relation = relations[targets[name]]
        assert relation["users"] or relation["parent"], name
    for name in ("UserInfo", "ChildTaskTemplateComplete"):
        assert relations[targets[name]]["parent"], name
    assert "员工角色转移" in components[targets["TaskTransferNotify"]].docstring
