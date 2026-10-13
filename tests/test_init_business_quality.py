# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Initialization planning preserves business context without directory supplements."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic_ai import ModelRetry

from codewiki.src.be import business_clustering as business
from codewiki.src.be.agent_tools.read_code_components import read_code_components
from codewiki.src.be.cluster_modules import collect_module_tree_component_ids
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.documentation_generator import DocumentationGenerator
from codewiki.src.be.generation_runtime import (
    GenerationRuntime,
    MissingDocumentationError,
    format_evidence_gap_summary,
    validate_document_complete,
    validate_source_reads,
)
from codewiki.src.be.prompt_template import format_user_prompt
from codewiki.src.config import Config


def node(cid, source="class Example {}", dependencies=(), kind="class"):
    path, _, name = cid.partition("::")
    return Node(
        id=cid,
        name=name,
        component_type=kind,
        relative_path=path,
        file_path=path,
        source_code=source,
        depends_on=set(dependencies),
    )


def config(tmp_path, **kwargs):
    return Config.from_cli(
        repo_path=str(tmp_path),
        output_dir=str(tmp_path),
        llm_base_url="http://localhost:1/v1",
        llm_api_key="test",
        main_model="main",
        cluster_model="cluster",
        agent_instructions={"language": "zh"},
        **kwargs,
    )


def inputs(prompt):
    return json.loads(prompt.split("<INPUT_EVIDENCE>\n")[1].split("\n</INPUT_EVIDENCE>")[0])


def response(name, refs, purpose="测评变更通知的生成、传递与处理"):
    return json.dumps({name: {"purpose": purpose, "components": refs}}, ensure_ascii=False)


def force_planning(monkeypatch):
    original = business.count_tokens
    monkeypatch.setattr(
        business, "count_tokens", lambda text: 50000 if text == "large" else original(text)
    )


def test_selection_retains_pruned_classes_and_isolated_function_files():
    keys = ["api/Controller.java::Controller", "dto/Event.java::Event", "scripts/send.js::send"]
    components = {cid: node(cid, kind="function" if cid == keys[2] else "class") for cid in keys}
    selected = business.select_business_components([keys[0]], components)
    assert set(selected) == set(keys)
    assert selected == business.select_business_components(
        [keys[0]], dict(reversed(list(components.items())))
    )


def test_selection_prefers_parser_structure_per_file_without_type_whitelist():
    java = [
        "svc/Order.java::OrderService",
        "svc/Order.java::OrderService.create",
        "svc/Order.java::formatMoney",
    ]
    go = ["queue/worker.go::enqueue", "queue/worker.go::drain"]
    rust = ["graph/node.rs::Node", "graph/node.rs::walk"]
    artifact_id = "pyproject.toml::mytool"
    components = {
        java[0]: node(java[0]),
        java[1]: node(java[1], kind="method"),
        java[2]: node(java[2], kind="function"),
        go[0]: node(go[0], kind="function"),
        go[1]: node(go[1], kind="function"),
        rust[0]: node(rust[0], kind="struct"),
        rust[1]: node(rust[1], kind="function"),
        artifact_id: node(artifact_id, kind="artifact"),
    }
    selected = business.select_business_components([go[0]], components)
    # One primary entry per unrepresented file: struct for Rust, function file
    # keeps its leaf, class for Java, artifact file keeps the artifact. Every
    # type can be an entry; nothing is filtered by a server-side whitelist.
    assert selected == [rust[0], artifact_id, go[0], java[0]]
    assert selected == business.select_business_components(
        [go[0]], dict(reversed(list(components.items())))
    )


def test_source_excerpt_is_syntax_neutral():
    source = "\n".join(f"fn helper{i}(x: u32) -> u32 {{ x + {i} }}" for i in range(120))
    excerpt = business._source_excerpt(node("graph/node.rs::walk", source))
    assert excerpt.startswith("fn helper0")
    assert "read full implementation" in excerpt
    assert business._source_excerpt(node("a.go::f", "package main")) == "package main"


def test_shared_completion_validation_is_backend_agnostic(tmp_path):
    cid = "svc/Order.java::OrderService"
    components = {cid: node(cid)}
    tree = {
        "订单": {
            "components": [cid],
            "children": {
                "明细": {"components": [], "children": {}},
            },
        }
    }
    deps = SimpleNamespace(
        components=components,
        module_tree=tree,
        path_to_current_module=["订单"],
        business_analysis=True,
        source_reads=set(),
        source_files_read=set(),
        unresolved_components=set(),
        config=config(tmp_path, layout="flat"),
    )
    document = tmp_path / "订单.md"
    with pytest.raises(MissingDocumentationError, match="missing or empty"):
        validate_document_complete(
            document, config=deps.config, working_dir=str(tmp_path), module_tree=tree
        )
    document.write_text("# 订单", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Child documentation incomplete"):
        validate_document_complete(
            document, config=deps.config, working_dir=str(tmp_path), module_tree=tree
        )
    (tmp_path / "明细.md").write_text("# 明细", encoding="utf-8")
    with pytest.raises(MissingDocumentationError, match="Unread exact repository IDs"):
        validate_source_reads(deps)
    deps.source_reads.add(cid)
    validate_document_complete(
        document, config=deps.config, working_dir=str(tmp_path), module_tree=tree
    )
    validate_source_reads(deps)
    deps.unresolved_components.add("svc/Missing.java")
    with pytest.raises(MissingDocumentationError, match="NOT verified repository IDs"):
        validate_source_reads(deps)


def test_evidence_gap_summary_is_file_aggregated_and_keeps_full_list_for_model():
    # One real case dumped ~100 unread IDs from 7 files into the terminal.
    ids = [f"svc/feign/Big.java::Big.m{i}" for i in range(90)]
    ids += [
        "svc/facade/Facade.java::Facade.a",
        "svc/facade/Facade.java::Facade.b",
        "svc/order/Order.java::Order.run",
    ]
    full_message = (
        "Source analysis incomplete. Unread exact repository IDs; "
        "use read_code_components: " + "; ".join(ids)
    )
    exc = MissingDocumentationError(full_message, missing_ids=ids)
    summary = format_evidence_gap_summary(exc)

    assert "93 个组件 / 3 个文件" in summary
    assert "feign/Big.java（90 个组件）" in summary
    assert "facade/Facade.java（2 个组件）" in summary
    # The human-facing summary must not enumerate every method-level ID.
    assert "Big.m0" not in summary
    assert "Big.m89" not in summary
    # The model continuation prompt still needs the complete list.
    message = str(exc)
    assert "Big.m0" in message and "Big.m89" in message
    assert "Unread exact repository IDs" in message
    # Structured access for telemetry.
    assert len(exc.missing_ids) == 93


def test_evidence_gap_summary_truncates_long_file_lists():
    ids = [f"svc/f{i}/F{i}.java::F{i}.go" for i in range(8)]
    exc = MissingDocumentationError("x", missing_ids=ids)
    summary = format_evidence_gap_summary(exc, max_files=5)
    assert summary.count("  - ") == 5
    assert "另有 3 个文件" in summary


def test_caw_backend_applies_shared_completion_validation():
    # Importing caw fails on Windows (fcntl), so assert against the source
    # file instead of inspecting the imported class.
    source = (Path(__file__).resolve().parents[1] / "codewiki/src/be/caw_backend.py").read_text(
        encoding="utf-8"
    )
    assert "validate_document_complete(" in source
    assert "validate_source_reads(" in source
    assert "required_source_ids" in source


def test_caw_editor_view_records_full_source_file(tmp_path):
    pytest.importorskip("caw")
    from codewiki.src.be.caw_toolkit import CawToolKit

    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "Order.java").write_text("class Order {}", encoding="utf-8")
    cid = "Order.java::Order"
    deps = SimpleNamespace(
        registry={},
        absolute_docs_path=str(tmp_path / "docs"),
        absolute_repo_path=str(tmp_path / "repo"),
        source_files_read=set(),
        unresolved_components={cid},
    )
    kit = object.__new__(CawToolKit)
    kit._deps = deps
    result = asyncio.run(kit.str_replace_editor("repo", "view", path="Order.java"))
    assert "class Order" in result
    assert deps.source_files_read == {"Order.java"}
    assert not deps.unresolved_components


def test_method_dependencies_join_business_chain_across_technical_directories():
    controller = "controller/Evaluation.java::Evaluation"
    method = "controller/Evaluation.java::Evaluation.change"
    service = "service/Change.java::Change"
    event = "dto/Event.java::Event"
    components = {
        controller: node(controller),
        method: node(method, dependencies=[service], kind="method"),
        service: node(service, dependencies=[event]),
        event: node(event),
    }
    ids = [controller, service, event]
    outgoing, incoming = business.dependency_neighborhoods(ids, components)
    assert service in outgoing[controller]
    assert controller in incoming[service]
    batches = business.partition_by_relationships(
        ids, components, outgoing, incoming, lambda batch: len(batch) <= 3
    )
    assert len(batches) == 1
    evidence = business.planning_evidence(ids, components, outgoing, incoming)
    assert service in evidence[controller]["depends_on"]
    assert "class Example" in evidence[controller]["source_excerpt"]


def test_relationship_partition_is_bounded_complete_and_deterministic():
    ids = [f"layer{i}/Topic{i}.java::Topic{i}" for i in range(6)]
    components = {
        cid: node(cid, dependencies=[ids[i + 1]] if i % 2 == 0 else []) for i, cid in enumerate(ids)
    }
    outgoing, incoming = business.dependency_neighborhoods(ids, components)
    first = business.partition_by_relationships(
        ids, components, outgoing, incoming, lambda batch: len(batch) <= 2
    )
    second = business.partition_by_relationships(
        list(reversed(ids)), components, outgoing, incoming, lambda batch: len(batch) <= 2
    )
    assert first == second
    assert all(len(batch) <= 2 for batch in first)
    assert {cid for batch in first for cid in batch} == set(ids)
    assert all({ids[i], ids[i + 1]} in map(set, first) for i in range(0, 6, 2))


def test_missing_assignment_is_reanalyzed_against_existing_topics(tmp_path, monkeypatch):
    force_planning(monkeypatch)
    ids = ["dto/Event.java::Event", "service/Change.java::Change"]
    components = {cid: node(cid, "large") for cid in ids}
    calls = []

    def complete(prompt):
        calls.append(prompt)
        rows = inputs(prompt)
        if len(calls) == 1:
            assert "source_excerpt" in rows[0]
            return response("测评变更通知", ["C0"])
        assert len(rows) == 1
        assert "测评变更通知" in prompt.split("<EXISTING_TOPICS>")[1].split("</EXISTING_TOPICS>")[0]
        # The retry carries only the unassigned reference, but keeps its
        # original number: a renumbered retry asks the model about a reference
        # that no longer exists, which is why the real run never converged.
        assert rows[0]["ref"] == "C1"
        assert "C1." in prompt.split("unassigned or unusable:")[-1]
        return response("测评变更通知", [rows[0]["ref"]])

    tree = business.cluster_business_modules(ids, components, config(tmp_path), complete)
    assert len(calls) == 2
    assert set(tree) == {"测评变更通知"}
    assert collect_module_tree_component_ids(tree) == set(ids)
    assert all(info["analysis_mode"] == "business" for info in tree.values())


def test_cross_batch_synonyms_reconcile_without_losing_source(tmp_path, monkeypatch):
    force_planning(monkeypatch)
    ids = [f"layer{i}/Change{i}.java::Change{i}" for i in range(3)]
    components = {cid: node(cid, "large") for cid in ids}
    calls = []

    def complete(prompt):
        calls.append(prompt)
        rows = inputs(prompt)
        if "Reconcile candidate topics" in prompt:
            assert len(rows) == 3
            return response("测评变更通知", [row["ref"] for row in rows])
        return response(f"变更处理{len(calls)}", [row["ref"] for row in rows])

    tree = business.cluster_business_modules(
        ids, components, config(tmp_path, max_leaf_nodes_per_cluster=1), complete
    )
    assert len(calls) == 4
    assert set(tree) == {"测评变更通知"}
    assert collect_module_tree_component_ids(tree) == set(ids)


def test_failed_planning_reports_missing_ids_not_directory_fallback(tmp_path, monkeypatch):
    force_planning(monkeypatch)
    ids = ["dto/Event.java::Event", "service/Change.java::Change"]
    components = {cid: node(cid, "large") for cid in ids}
    with pytest.raises(
        business.BusinessClusteringError, match="unresolved repository IDs"
    ) as error:
        business.cluster_business_modules(ids, components, config(tmp_path), lambda prompt: None)
    message = str(error.value)
    assert all(cid in message for cid in ids)
    # The failure must name the real response problem, not only the leftovers.
    assert "Empty clustering response" in message
    assert not list(tmp_path.glob("*.md"))


@pytest.mark.parametrize(
    "result,expected_problem",
    [
        (response("测评", ["C0", "C0"]), "duplicate"),
        (response("测评", ["C99"]), "unknown"),
        (response("dto_补充分析_10", ["C0"]), "coverage placeholder"),
    ],
)
def test_invalid_primary_assignment_is_rejected_not_salvaged(result, expected_problem):
    assignments, problems = business._read_assignments(result, ["a.java::A"], {})
    if expected_problem == "duplicate":
        # The first occurrence is still a valid assignment; only the repeat is dropped.
        assert assignments["测评"]["components"] == ["a.java::A"]
    else:
        assert assignments == {}
    assert any(expected_problem in problem for problem in problems)


def test_new_topic_without_purpose_is_rejected():
    payload = json.dumps({"新主题": {"components": ["C0"]}}, ensure_ascii=False)
    assignments, problems = business._read_assignments(payload, ["a.java::A"], {})
    assert assignments == {}
    assert any("purpose" in problem for problem in problems)


def test_existing_topic_may_be_extended_without_purpose(tmp_path, monkeypatch):
    # Regression: the repair round asks the model to reuse an existing topic's
    # exact name. A response that only adds components was rejected outright,
    # which aborted a full run over two unassigned converters.
    force_planning(monkeypatch)
    ids = ["dto/Event.java::Event", "dto/converter/QueryRespConverter.java::QueryRespConverter"]
    components = {cid: node(cid, "large") for cid in ids}
    calls = []

    def complete(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return response("用户学时查询", ["C0"])
        # The repair round names exactly which references are still unassigned.
        assert "C1." in prompt.split("unassigned or unusable:")[-1]
        return json.dumps(
            {"用户学时查询": {"components": [row["ref"] for row in inputs(prompt)]}},
            ensure_ascii=False,
        )

    tree = business.cluster_business_modules(ids, components, config(tmp_path), complete)
    assert len(calls) == 2
    assert set(tree) == {"用户学时查询"}
    assert tree["用户学时查询"]["purpose"] == "测评变更通知的生成、传递与处理"
    assert collect_module_tree_component_ids(tree) == set(ids)


def test_planning_failure_names_the_real_response_problem(tmp_path, monkeypatch):
    force_planning(monkeypatch)
    ids = ["dto/Event.java::Event", "service/Change.java::Change"]
    components = {cid: node(cid, "large") for cid in ids}
    with pytest.raises(business.BusinessClusteringError) as error:
        business.cluster_business_modules(ids, components, config(tmp_path), lambda prompt: None)
    message = str(error.value)
    assert "unresolved repository IDs" in message
    assert all(cid in message for cid in ids)
    assert "Empty clustering response" in message


def _studentinfo_ids():
    """The shape that failed a live run: passive payloads, an inner class and
    MQ carriers the model refused to place under any business topic."""
    base = "src/main/java/com/i61/eos/lp/service/business/studentinfo"
    return [
        f"{base}/service/StudentInfoService.java::StudentInfoService",
        f"{base}/dto/req/SearchUserReqDTO.java::SearchUserReqDTO",
        f"{base}/dto/resp/SearchUserRespDTO.java::UserInfo",
        "src/main/java/com/i61/eos/lp/service/business/task/dto/TaskCancelMqNotify.java::TaskCancelMqNotify",
    ]


def test_unclassifiable_payload_without_usage_evidence_remains_unresolved(tmp_path, monkeypatch):
    force_planning(monkeypatch)
    ids = _studentinfo_ids()
    components = {cid: node(cid, "large") for cid in ids}
    payload = ids[1]

    def refuses_payloads(prompt):
        return response("学员信息查询", [r["ref"] for r in inputs(prompt) if r["id"] != payload])

    with pytest.raises(business.BusinessClusteringError, match=payload):
        business.cluster_business_modules(ids, components, config(tmp_path), refuses_payloads)


def test_refusal_topic_is_rejected_and_cannot_fake_completed_ownership(tmp_path, monkeypatch):
    force_planning(monkeypatch)
    ids = _studentinfo_ids()
    components = {cid: node(cid, "large") for cid in ids}

    def refuses_with_meta_topic(prompt):
        return response("证据不足，无法归类", [r["ref"] for r in inputs(prompt)])

    with pytest.raises(
        business.BusinessClusteringError, match="analysis-about-analysis topic rejected"
    ):
        business.cluster_business_modules(
            ids, components, config(tmp_path), refuses_with_meta_topic
        )


def test_meta_topic_names_are_recognised():
    for name in (
        "证据不足，无法归类",
        "未归类组件",
        "无法分类",
        "Unclassified",
        "Insufficient evidence",
    ):
        assert business._is_meta_topic(name), name
    for name in (
        "学员信息查询",
        "停课次数核对",
        "任务转派",
        "Shared Data Contracts",
        "待确认订单审核",
        "其他订单来源",
        "未知用户识别",
    ):
        assert not business._is_meta_topic(name), name


def test_totally_failed_planning_still_raises_rather_than_inventing_owners(tmp_path, monkeypatch):
    """The fallback must never turn "the model produced nothing" into a
    directory listing dressed as business topics."""
    force_planning(monkeypatch)
    ids = ["a/A.java::A", "b/B.java::B"]
    components = {cid: node(cid, "large") for cid in ids}
    with pytest.raises(business.BusinessClusteringError, match="unresolved repository IDs"):
        business.cluster_business_modules(ids, components, config(tmp_path), lambda prompt: None)


def test_reference_numbers_survive_a_retry(tmp_path, monkeypatch):
    """A shrinking pending list used to be renumbered from C0, so the retry
    asked the model about references that were not in its input."""
    force_planning(monkeypatch)
    ids = ["a/A.java::A", "b/B.java::B", "c/C.java::C", "d/D.java::D"]
    components = {cid: node(cid, "large") for cid in ids}
    rounds = []

    def drops_one_each_round(prompt):
        rows = inputs(prompt)
        rounds.append([r["ref"] for r in rows])
        keep = rows[:-1]
        return response("业务处理", [r["ref"] for r in keep])

    with pytest.raises(business.BusinessClusteringError, match="d/D.java::D"):
        business.cluster_business_modules(ids, components, config(tmp_path), drops_one_each_round)
    assert rounds[0] == ["C0", "C1", "C2", "C3"]
    # Each retry drops exactly the references it has not yet assigned, with
    # their original numbers — never renumbered back to C0.
    assert rounds[1] == ["C3"]
    assert rounds[2] == ["C3"]


def test_full_file_path_read_recovers_real_ids_and_clears_unresolved_request():
    path = "business/evaluation/dto/NewEvaluationChangeMqNotify.java"
    ids = [path + "::NewEvaluationChangeMqNotify", path + "::NewEvaluationChangeMqNotify.getId"]
    deps = SimpleNamespace(
        components={cid: node(cid) for cid in ids},
        source_reads=set(),
        unresolved_components={path},
        source_files_read=set(),
    )
    text = asyncio.run(read_code_components(SimpleNamespace(deps=deps), [path]))
    assert all(cid in text for cid in ids)
    assert deps.source_reads == set(ids)
    assert not deps.unresolved_components


def test_failed_batch_does_not_record_source_not_delivered_to_model():
    cid = "a.java::A"
    deps = SimpleNamespace(
        components={cid: node(cid)}, source_reads=set(), unresolved_components=set()
    )
    with pytest.raises(ModelRetry):
        asyncio.run(read_code_components(SimpleNamespace(deps=deps), [cid, "missing.java"]))
    assert not deps.source_reads


def test_same_symbol_read_does_not_clear_other_unresolved_file():
    ids = ["one/a.java::Service", "two/b.java::Service"]
    deps = SimpleNamespace(
        components={cid: node(cid) for cid in ids},
        source_reads=set(),
        unresolved_components={ids[1]},
    )
    asyncio.run(read_code_components(SimpleNamespace(deps=deps), [ids[0]]))
    assert deps.unresolved_components == {ids[1]}


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_dependency_source_is_inlined_and_marked_as_read(tmp_path):
    """Dependencies are provided in the prompt, so no tool read is required."""
    from codewiki.src.be.prompt_template import format_user_prompt

    (tmp_path / "Order.java").write_text(
        "package sample;\nclass Order { void pay() { new Payment(); } }\n", encoding="utf-8"
    )
    (tmp_path / "Payment.java").write_text(
        "package sample;\nclass Payment { void charge() {} }\n", encoding="utf-8"
    )
    order = node("Order.java::Order")
    order.file_path = str(tmp_path / "Order.java")
    payment = node("Payment.java::Payment", dependencies=[])
    payment.file_path = str(tmp_path / "Payment.java")
    components = {
        "Order.java::Order": order,
        "Order.java::Order.pay": node(
            "Order.java::Order.pay", dependencies=["Payment.java::Payment"], kind="method"
        ),
        "Payment.java::Payment": payment,
    }
    components["Order.java::Order"].depends_on = {"Payment.java::Payment"}

    reads, required = set(), set()
    prompt = format_user_prompt(
        "订单",
        ["Order.java::Order"],
        components,
        {},
        business_analysis=True,
        required_source_ids=required,
        prompt_read_ids=reads,
    )
    assert "# File: Payment.java" in prompt, "dependency source must be inlined"
    assert "class Payment" in prompt
    assert "SOURCE_ALREADY_PROVIDED" in prompt
    assert "Payment.java" in prompt.split("SOURCE_ALREADY_PROVIDED")[1]
    # Dependency counts as read evidence without a tool call.
    assert "Payment.java::Payment" in reads
    # And it is no longer demanded as an unread repository ID.
    assert "Payment.java::Payment" not in required
    validate_source_reads(
        SimpleNamespace(
            components=components,
            module_tree={"订单": {"components": ["Order.java::Order"]}},
            path_to_current_module=["订单"],
            business_analysis=True,
            source_reads=reads,
            source_files_read=set(),
            unresolved_components=set(),
        )
    )


def test_provided_source_list_hides_truncated_files(tmp_path, monkeypatch):
    """A file whose text was cut must not be advertised as fully provided."""
    from codewiki.src.be import prompt_template as prompts

    source = "class Change {" + "int changed;" * 2000 + "}"
    (tmp_path / "Change.java").write_text(source, encoding="utf-8")
    (tmp_path / "Other.java").write_text("class Other {}", encoding="utf-8")
    comp = node("Change.java::Change")
    comp.file_path = str(tmp_path / "Change.java")
    other = node("Other.java::Other")
    other.file_path = str(tmp_path / "Other.java")
    components = {"Change.java::Change": comp, "Other.java::Other": other}
    monkeypatch.setattr(prompts, "MAX_USER_PROMPT_CHARS", 2500)

    # Inlining follows the given order and truncation cuts from the end, so the
    # small file must come first for it to survive.
    prompt = format_user_prompt(
        "测评变更",
        ["Other.java::Other", "Change.java::Change"],
        components,
        {},
        business_analysis=True,
    )
    assert (
        "truncated inlined file contents" in prompt or len(prompt) <= prompts.MAX_USER_PROMPT_CHARS
    )
    provided = (
        prompt.split("SOURCE_ALREADY_PROVIDED")[1].split("</SOURCE_ALREADY_PROVIDED>")[0]
        if "SOURCE_ALREADY_PROVIDED" in prompt
        else ""
    )
    assert "Other.java" in provided, "the small file is fully provided"
    assert "\nChange.java\n" not in provided, "the truncated file must not be advertised"
    assert "Change.java" in prompt.split("SOURCE_ALREADY_PROVIDED")[0], "its source is still shown"


def test_truncated_artifact_head_is_not_full_evidence(tmp_path):
    """Artifact files with a capped head must stay 'not provided'."""
    from codewiki.src.be.prompt_template import format_user_prompt
    from codewiki.src.be.dependency_analyzer.analyzers.artifact import TRUNCATION_MARKER

    cid = "build.yaml::build"
    comp = node(cid, kind="artifact")
    comp.source_code = "steps: []" + TRUNCATION_MARKER.format(shown=10, total=9999)
    components = {cid: comp}
    reads, required = set(), set()
    format_user_prompt(
        "构建",
        [cid],
        components,
        {},
        business_analysis=True,
        required_source_ids=required,
        prompt_read_ids=reads,
    )
    assert cid not in reads, "a truncated artifact head is not full source evidence"


def test_delegation_prompt_is_omitted_when_depth_is_exhausted():
    """With max_depth=1 the delegation step and tool must not be advertised."""
    from codewiki.src.be.prompt_template import format_system_prompt

    delegating = format_system_prompt("订单", None, ["订单"], "flat", can_delegate=True)
    assert "generate_sub_module_documentation" in delegating
    assert "After all sub-modules are documented" in delegating

    flat = format_system_prompt("订单", None, ["订单"], "flat", can_delegate=False)
    assert "generate_sub_module_documentation" not in flat
    assert "After all sub-modules are documented" not in flat
    assert "Review the main documentation file once at the end" in flat
    # The rest of the prompt must be unchanged.
    assert "<ROLE>" in flat and "MERMAID" not in flat or "<ROLE>" in flat


def test_delegation_tool_is_unregistered_when_depth_is_exhausted(tmp_path, monkeypatch):
    """The tool must be dropped from the agent, not just from the prompt text.

    pydantic-ai sends every registered tool's schema to the model regardless of
    the prompt, so a prompt-only removal still lets the model call it and spawn
    a whole extra agent session.
    """
    from codewiki.src.be import pydantic_ai_backend as backend_mod

    captured = {}

    class RecordingAgent:
        def __init__(self, *args, **kwargs):
            captured.setdefault("tools", []).append(kwargs.get("tools"))

        def __getattr__(self, name):
            raise AssertionError(f"agent used beyond construction: {name}")

    monkeypatch.setattr(backend_mod, "Agent", RecordingAgent)
    monkeypatch.setattr(backend_mod, "create_fallback_models", lambda config: object())
    monkeypatch.setattr(backend_mod, "format_leaf_system_prompt", lambda *a, **k: "leaf")
    monkeypatch.setattr(backend_mod, "format_system_prompt", lambda *a, **k: "system")

    cfg = config(tmp_path, max_depth=1)
    components = {cid: node(cid) for cid in ["a/A.java::A", "b/B.java::B"]}
    backend = object.__new__(backend_mod.PydanticAIBackend)
    backend._config = cfg
    backend._fallback_models = object()
    backend._custom_instructions = None
    backend.generation_runtime = None

    import asyncio

    (tmp_path / "module_tree.json").write_text(
        json.dumps({"订单": {"components": list(components), "children": {}}}),
        encoding="utf-8",
    )
    # We only care about how the agent was constructed; the stub refuses to run.
    with pytest.raises(Exception):
        asyncio.run(
            backend.run_module_agent(
                module_name="订单",
                components=components,
                core_component_ids=list(components),
                module_path=["订单"],
                working_dir=str(tmp_path),
            )
        )

    assert captured["tools"], "an agent must have been constructed"
    for tools in captured["tools"]:
        names = {getattr(t, "name", None) for t in tools}
        assert "generate_sub_module_documentation" not in names, (
            "delegation tool must be unregistered when max_depth is exhausted"
        )
        assert {"read_code_components", "str_replace_editor"} <= names


def test_delegation_tool_stays_registered_when_depth_allows(tmp_path, monkeypatch):
    """With max_depth>1 the delegation tool must still reach the model."""
    from codewiki.src.be import pydantic_ai_backend as backend_mod

    captured = {}

    class RecordingAgent:
        def __init__(self, *args, **kwargs):
            captured.setdefault("tools", []).append(kwargs.get("tools"))

        def __getattr__(self, name):
            raise AssertionError(f"agent used beyond construction: {name}")

    monkeypatch.setattr(backend_mod, "Agent", RecordingAgent)
    monkeypatch.setattr(backend_mod, "create_fallback_models", lambda config: object())
    monkeypatch.setattr(backend_mod, "format_leaf_system_prompt", lambda *a, **k: "leaf")
    monkeypatch.setattr(backend_mod, "format_system_prompt", lambda *a, **k: "system")

    cfg = config(tmp_path, max_depth=2, max_token_per_leaf_module=1)
    components = {cid: node(cid) for cid in ["a/A.java::A", "b/B.java::B"]}
    backend = object.__new__(backend_mod.PydanticAIBackend)
    backend._config = cfg
    backend._fallback_models = object()
    backend._custom_instructions = None
    backend.generation_runtime = None

    import asyncio

    (tmp_path / "module_tree.json").write_text(
        json.dumps({"订单": {"components": list(components), "children": {}}}),
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        asyncio.run(
            backend.run_module_agent(
                module_name="订单",
                components=components,
                core_component_ids=list(components),
                module_path=["订单"],
                working_dir=str(tmp_path),
            )
        )

    names = {getattr(t, "name", None) for t in captured["tools"][-1]}
    assert "generate_sub_module_documentation" in names, (
        "delegation must stay available when the depth budget allows it"
    )


def test_small_module_is_not_delegated_even_with_depth_budget(tmp_path, monkeypatch):
    """The token threshold must gate delegation, matching CawBackend.

    A two-file module whose source is far below
    ``max_token_per_leaf_module`` is written as one page; delegating it would
    spawn an extra agent session for nothing.
    """
    from codewiki.src.be import pydantic_ai_backend as backend_mod

    captured = {}

    class RecordingAgent:
        def __init__(self, *args, **kwargs):
            captured.setdefault("tools", []).append(kwargs.get("tools"))

        def __getattr__(self, name):
            raise AssertionError(f"agent used beyond construction: {name}")

    monkeypatch.setattr(backend_mod, "Agent", RecordingAgent)
    monkeypatch.setattr(backend_mod, "create_fallback_models", lambda config: object())
    monkeypatch.setattr(backend_mod, "format_leaf_system_prompt", lambda *a, **k: "leaf")
    monkeypatch.setattr(backend_mod, "format_system_prompt", lambda *a, **k: "system")

    cfg = config(tmp_path, max_depth=2)
    assert cfg.max_token_per_leaf_module > 1_000, "fixture assumes a large threshold"
    components = {cid: node(cid) for cid in ["a/A.java::A", "b/B.java::B"]}
    backend = object.__new__(backend_mod.PydanticAIBackend)
    backend._config = cfg
    backend._fallback_models = object()
    backend._custom_instructions = None
    backend.generation_runtime = None

    import asyncio

    (tmp_path / "module_tree.json").write_text(
        json.dumps({"订单": {"components": list(components), "children": {}}}),
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        asyncio.run(
            backend.run_module_agent(
                module_name="订单",
                components=components,
                core_component_ids=list(components),
                module_path=["订单"],
                working_dir=str(tmp_path),
            )
        )

    names = {getattr(t, "name", None) for t in captured["tools"][-1]}
    assert "generate_sub_module_documentation" not in names, (
        "a small module must not be delegated even when depth budget allows it"
    )


def test_sub_module_agent_does_not_offer_delegation_at_the_last_level(tmp_path, monkeypatch):
    """A depth-1 sub-module agent must not carry the delegation tool either.

    Otherwise the same bug reappears one level down: the model can still call
    it (pydantic-ai sends every registered tool's schema) and each call spawns
    another agent session.
    """
    import asyncio

    from codewiki.src.be.agent_tools import generate_sub_module_documentations as sub

    captured = {}

    class RecordingAgent:
        def __init__(self, *args, **kwargs):
            captured.setdefault("tools", []).append(kwargs.get("tools"))

        def __getattr__(self, name):
            raise AssertionError(f"agent used beyond construction: {name}")

    monkeypatch.setattr(sub, "Agent", RecordingAgent)
    monkeypatch.setattr(sub, "create_fallback_models", lambda config: object())

    cid = "svc/Change.java::Change"
    cid2 = "svc/Other.java::Other"
    components = {cid: node(cid, "class Change {" + "int changed;" * 200 + "}"), cid2: node(cid2)}
    deps = SimpleNamespace(
        components=components,
        module_tree={"变更": {"components": [cid, cid2], "children": {}}},
        path_to_current_module=["变更"],
        absolute_docs_path=str(tmp_path / "docs"),
        current_module_name="变更",
        max_depth=2,
        current_depth=1,
        custom_instructions=None,
        config=config(tmp_path, max_depth=2, max_token_per_leaf_module=1),
        business_analysis=False,
        registry={},
        generation_runtime=None,
        allowed_write_paths=None,
    )

    # max_depth=2 and current_depth=1, so the parent's guard passes and a sub
    # agent is built; that sub agent sits at depth 2 and must not delegate on.
    with pytest.raises(Exception):
        asyncio.run(
            sub.generate_sub_module_documentation(
                SimpleNamespace(deps=deps),
                {"子": ["svc/Change.java: Change"]},
            )
        )

    assert captured["tools"], "a sub agent must have been constructed"
    for tools in captured["tools"]:
        names = {getattr(t, "name", None) for t in tools}
        assert "generate_sub_module_documentation" not in names, (
            "the last usable depth must not carry the delegation tool"
        )


@pytest.mark.parametrize("backend", ["api", "caw"])
def test_confirmed_missing_external_path_clears_only_that_request(tmp_path, backend):
    from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor

    cid = "business/LessonService.java::LessonService"
    absent_path = "feign/ILiveStudentServiceApi.java"
    requested = absent_path + "::ILiveStudentServiceApi"
    deps = SimpleNamespace(
        registry={},
        absolute_docs_path=str(tmp_path / "docs"),
        absolute_repo_path=str(tmp_path),
        components={cid: node(cid)},
        source_reads={cid},
        source_files_read=set(),
        source_files_missing=set(),
        unresolved_components=set(),
        module_tree={"课程": {"components": [cid]}},
        path_to_current_module=["课程"],
        business_analysis=True,
    )
    ctx = SimpleNamespace(deps=deps)
    if backend == "caw":
        pytest.importorskip("caw")
        from codewiki.src.be.caw_toolkit import CawToolKit

        kit = object.__new__(CawToolKit)
        kit._deps = deps
        view = kit.str_replace_editor
    else:

        async def view(*args, **kwargs):
            return await str_replace_editor(ctx, *args, **kwargs)

    # A path-like read that is absent from disk is verified deterministically
    # (same evidence as a repo view) instead of looping ModelRetry: the page
    # cannot burn its request budget repeating an unverified path.
    result = asyncio.run(read_code_components(ctx, [requested]))
    assert "File not found (verified on disk)" in result
    assert deps.source_files_missing == {absent_path}
    assert not deps.unresolved_components
    assert deps.source_reads == {cid}
    validate_source_reads(deps)
    # ...but clearing is scoped to the inspected path: another unverified ID
    # still fails validation.
    unresolved_other = "other/Missing.java::Missing"
    deps.unresolved_components.add(unresolved_other)
    result = asyncio.run(view("repo", "view", path="./" + absent_path))
    assert "does not exist" in result
    assert "not recorded as read source" in result
    assert deps.unresolved_components == {unresolved_other}
    assert deps.source_files_missing == {absent_path}
    assert not deps.source_files_read
    deps.unresolved_components.remove(unresolved_other)
    validate_source_reads(deps)
    deps.unresolved_components.add(requested)
    validate_source_reads(deps)
    # Repeating the read returns the verified-absence answer, not a retry.
    result = asyncio.run(read_code_components(ctx, [requested]))
    assert "File not found (verified on disk)" in result
    assert not deps.unresolved_components
    deps.source_reads.clear()
    with pytest.raises(MissingDocumentationError, match="Unread exact repository IDs"):
        validate_source_reads(deps)


@pytest.mark.parametrize("graph_path", ["feign/Api.java", r"feign\Api.java"])
def test_missing_graph_file_still_requires_source_evidence(tmp_path, graph_path):
    from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor

    cid = graph_path + "::Api"
    deps = SimpleNamespace(
        registry={},
        absolute_docs_path=str(tmp_path / "docs"),
        absolute_repo_path=str(tmp_path),
        components={cid: node(cid, source=None)},
        source_reads=set(),
        source_files_read=set(),
        unresolved_components={cid},
        module_tree={"课程": {"components": [cid]}},
        path_to_current_module=["课程"],
        business_analysis=True,
    )
    result = asyncio.run(
        str_replace_editor(SimpleNamespace(deps=deps), "repo", "view", path="feign/Api.java")
    )
    assert "does not exist" in result
    assert deps.unresolved_components == {cid}
    assert not deps.source_files_read
    with pytest.raises(MissingDocumentationError, match="Unread exact repository IDs"):
        validate_source_reads(deps)


def test_path_read_surfaces_unindexed_file_and_directory(tmp_path):
    """Mapper XML/resource paths live on disk but outside the Java graph:
    read them deterministically instead of looping ModelRetry."""
    resources = tmp_path / "sample-service" / "src" / "main" / "resources"
    resources.mkdir(parents=True)
    (resources / "mapper.xml").write_text("<mapper/>", encoding="utf-8")

    deps = SimpleNamespace(
        components={},
        source_reads=set(),
        source_files_read=set(),
        unresolved_components={
            "sample-service/src/main/resources/mapper.xml::Mapper",
            "sample-service/src/main/resources",
        },
        absolute_repo_path=str(tmp_path),
    )
    ctx = SimpleNamespace(deps=deps)

    text = asyncio.run(
        read_code_components(ctx, ["sample-service/src/main/resources/mapper.xml::Mapper"])
    )
    assert "<mapper/>" in text
    assert "sample-service/src/main/resources/mapper.xml" in deps.source_files_read
    assert not any("mapper.xml" in u for u in deps.unresolved_components)

    text = asyncio.run(read_code_components(ctx, ["sample-service/src/main/resources"]))
    assert "mapper.xml" in text
    assert deps.unresolved_components == set()


def test_bare_symbol_without_repo_root_keeps_model_retry():
    # No absolute_repo_path and a bare, unknown symbol: no filesystem
    # verification is possible, so the candidate/ModelRetry path stays.
    requested = "Service::guess"
    deps = SimpleNamespace(
        components={"a/A.java::A": node("a/A.java::A")},
        source_reads=set(),
        unresolved_components=set(),
    )
    with pytest.raises(ModelRetry):
        asyncio.run(read_code_components(SimpleNamespace(deps=deps), [requested]))
    assert requested in deps.unresolved_components


def test_unread_existing_file_is_not_confirmed_missing(tmp_path):
    from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor

    (tmp_path / "External.java").write_text("interface External {}", encoding="utf-8")
    requested = "External.java::External"
    deps = SimpleNamespace(
        registry={},
        absolute_docs_path=str(tmp_path / "docs"),
        absolute_repo_path=str(tmp_path),
        components={},
        source_reads=set(),
        source_files_read=set(),
        unresolved_components={requested},
        module_tree={},
        path_to_current_module=[],
    )
    ctx = SimpleNamespace(deps=deps)
    result = asyncio.run(
        str_replace_editor(ctx, "repo", "view", path="External.java", view_range=[1, 1])
    )
    assert "interface External" in result
    assert not getattr(deps, "source_files_missing", set())
    with pytest.raises(MissingDocumentationError, match="NOT verified repository IDs"):
        validate_source_reads(deps)
    asyncio.run(str_replace_editor(ctx, "repo", "view", path="External.java"))
    assert deps.source_files_read == {"External.java"}
    assert not deps.unresolved_components


def test_outside_repository_view_does_not_confirm_absent_source(tmp_path):
    from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor

    requested = "../External.java::External"
    deps = SimpleNamespace(
        registry={},
        absolute_docs_path=str(tmp_path / "docs"),
        absolute_repo_path=str(tmp_path),
        components={},
        source_reads=set(),
        source_files_read=set(),
        unresolved_components={requested},
    )
    result = asyncio.run(
        str_replace_editor(SimpleNamespace(deps=deps), "repo", "view", path="../External.java")
    )
    assert "escapes" in result
    assert not getattr(deps, "source_files_missing", set())
    assert deps.unresolved_components == {requested}


def test_prompt_truncation_keeps_scope_and_requires_missing_implementation(tmp_path, monkeypatch):
    from codewiki.src.be import prompt_template as prompts

    cid = "service/Change.java::Change"
    source = tmp_path / "Change.java"
    source.write_text("class Change {" + "int changed;" * 2000 + "}", encoding="utf-8")
    comp = node(cid)
    comp.file_path = str(source)
    reads, required = set(), set()
    monkeypatch.setattr(prompts, "MAX_USER_PROMPT_CHARS", 2500)
    prompt = format_user_prompt(
        "测评变更",
        [cid],
        {cid: comp},
        {},
        business_analysis=True,
        required_source_ids=required,
        prompt_read_ids=reads,
    )
    assert cid in prompt
    assert "state changes" in prompt
    assert required == {cid}
    assert reads == set()


def test_shared_source_is_available_to_each_business_theme(tmp_path):
    cid = "shared/Event.java::Event"
    deps_a = SimpleNamespace(
        components={cid: node(cid)}, source_reads=set(), unresolved_components=set()
    )
    deps_b = SimpleNamespace(
        components=deps_a.components, source_reads=set(), unresolved_components=set()
    )
    asyncio.run(read_code_components(SimpleNamespace(deps=deps_a), [cid]))
    assert not deps_b.source_reads
    asyncio.run(read_code_components(SimpleNamespace(deps=deps_b), [cid]))
    assert deps_a.source_reads == deps_b.source_reads == {cid}


def test_unresolved_request_is_not_mislabeled_exact_repository_id(tmp_path):
    (tmp_path / "module_tree.json").write_text(
        json.dumps({"测评": {"components": []}}), encoding="utf-8"
    )
    runtime = GenerationRuntime(config(tmp_path), tmp_path)
    deps = SimpleNamespace(
        components={},
        module_tree=runtime.tree,
        path_to_current_module=["测评"],
        business_analysis=False,
        unresolved_components={"unknown.java"},
        source_reads=set(),
        source_files_read=set(),
    )
    with pytest.raises(MissingDocumentationError, match="NOT verified repository IDs"):
        runtime.validate_source_evidence(deps)


def test_backend_init_uses_business_planner_and_preserves_isolated_file(tmp_path, monkeypatch):
    cid = "scripts/orphan.js::send"
    components = {cid: node(cid, kind="function")}
    cfg = config(tmp_path)
    generator = DocumentationGenerator(cfg, backend=SimpleNamespace())
    generator.graph_builder = SimpleNamespace(build_dependency_graph=lambda: (components, []))
    captured = []

    async def generate(comps, entries):
        captured.extend(entries)
        return cfg.docs_dir

    generator.generate_module_documentation = generate
    generator.create_documentation_metadata = lambda *args: None
    generator.validate_generated_docs = lambda *args: []
    with patch(
        "codewiki.src.be.documentation_generator.cluster_business_modules", return_value={}
    ) as planner:
        asyncio.run(generator.run())
    assert planner.call_args.args[0] == [cid]
    assert captured == [cid]
    assert generator.business_analysis


def test_init_two_concurrency_keeps_business_source_and_page_evidence(tmp_path, monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from codewiki.src.be import pydantic_ai_backend as api
    from codewiki.src.be.generation_runtime import request_slot

    ids = ["evaluation/Change.java::Change", "fulfillment/Deliver.java::Deliver"]
    components = {}
    names = ["测评变更", "课程履约"]
    tree = {}
    for cid, name in zip(ids, names):
        source = tmp_path / cid.partition("::")[0]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("class " + cid.partition("::")[2] + " {}", encoding="utf-8")
        components[cid] = node(cid)
        components[cid].file_path = str(source)
        tree[name] = {"purpose": name + "的规则与流程", "components": [cid], "children": {}}
    (tmp_path / "module_tree.json").write_text(
        json.dumps(tree, ensure_ascii=False), encoding="utf-8"
    )
    cfg = config(tmp_path, concurrency=2, layout="flat")
    calls = []
    active = peak = 0

    async def respond(messages, info):
        nonlocal active, peak
        if any(isinstance(message, ModelResponse) for message in messages):
            return ModelResponse(parts=[TextPart("done")])
        text = "\n".join(
            str(part.content)
            for message in messages
            for part in message.parts
            if hasattr(part, "content")
        )
        name = "测评变更" if "Business topic purpose: 测评变更" in text else "课程履约"
        calls.append(name)
        assert "state changes" in text
        async with request_slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "str_replace_editor",
                    {
                        "working_dir": "docs",
                        "command": "create",
                        "path": name + ".md",
                        "file_text": "# " + name + "\n源码规则与流程",
                    },
                )
            ]
        )

    monkeypatch.setattr(api, "create_fallback_models", lambda config: FunctionModel(respond))
    backend = api.PydanticAIBackend(cfg)
    backend.complete = lambda *args, **kwargs: "<OVERVIEW># 总览</OVERVIEW>"
    generator = DocumentationGenerator(cfg, backend=backend)
    generator.business_analysis = True
    asyncio.run(generator.generate_module_documentation(components, ids))
    assert peak == 2
    assert sorted(calls) == sorted(names)
    runtime = generator.generation_runtime
    assert not runtime.pending
    assert all(
        runtime.evidence_current(tmp_path / (name + ".md"), components, [name]) for name in names
    )


def test_planning_requests_respect_input_budget(tmp_path, monkeypatch):
    force_planning(monkeypatch)
    ids = [f"layer{i}/Change{i}.java::Change{i}" for i in range(30)]
    components = {cid: node(cid, "large") for cid in ids}
    cfg = config(tmp_path, max_token_per_module=1800)
    calls = []

    def complete(prompt):
        calls.append(prompt)
        assert business.count_tokens(prompt) <= cfg.max_token_per_module
        return response("测评变更", [row["ref"] for row in inputs(prompt)])

    tree = business.cluster_business_modules(ids, components, cfg, complete)
    assert len(calls) > 1
    assert collect_module_tree_component_ids(tree) == set(ids)


def test_unassigned_source_expands_implementation_only_when_needed(tmp_path):
    cid = "service/Change.java::Change"
    source = (
        "class Change {\n" + "private int value;\n" * 150 + "void change() { notifyChange(); }\n}"
    )
    components = {cid: node(cid, source)}
    outgoing, incoming = business.dependency_neighborhoods([cid], components)
    rows = business.planning_evidence([cid], components, outgoing, incoming)
    assert rows[cid]["source_excerpt"] != source
    calls = []

    def complete(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return json.dumps({"测评变更": {"purpose": "处理变更", "components": []}})
        assert inputs(prompt)[0]["source_excerpt"] == source
        return response("测评变更", ["C0"])

    tree = {}
    business._assign_batch([cid], rows, tree, config(tmp_path), complete, components=components)
    assert tree["测评变更"]["components"] == [cid]


def test_update_reclustering_uses_business_planner():
    import inspect
    from codewiki.src.be.updater.orchestrator import IncrementalUpdater

    source = inspect.getsource(IncrementalUpdater._recluster)
    assert "cluster_business_modules(" in source
    assert "cluster_modules(" not in source


def test_update_editing_agent_demands_business_accuracy():
    from codewiki.src.be.updater.prompts import UPDATE_LEAF_SYSTEM_PROMPT

    assert "Business accuracy outranks minimal diff" in UPDATE_LEAF_SYSTEM_PROMPT
    for topic in ("business rule", "state transition", "data conversion"):
        assert topic in UPDATE_LEAF_SYSTEM_PROMPT
    # The graded rule still protects untouched prose.
    assert "word for word" in UPDATE_LEAF_SYSTEM_PROMPT


def test_update_leaf_deps_are_business_analysis():
    import inspect
    from codewiki.src.be.updater.leaf_agent import LeafAgentRunner

    assert "business_analysis=True" in inspect.getsource(LeafAgentRunner._deps)


def test_regenerated_page_validated_and_recorded_honestly(tmp_path):
    import asyncio
    from codewiki.src.be.updater import pages as P
    from codewiki.src.be.updater.leaf_agent import LeafAgentRunner
    from codewiki.src.be.updater.record import UpdateRecord

    cid = "svc/Order.java::OrderService"
    components = {cid: node(cid)}
    tree = {"订单": {"components": [cid], "children": {}}}
    (tmp_path / "module_tree.json").write_text(
        json.dumps(tree, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "temp").mkdir()
    cfg = config(tmp_path, layout="flat")

    class Backend:
        last_usage = None

        def __init__(self, text):
            self.text = text

        async def run_module_agent(self, module_name, working_dir, **kwargs):
            target = Path(P.page_path(str(tmp_path), module_name))
            target.write_text(self.text, encoding="utf-8")

    record = UpdateRecord(options={})
    runner = LeafAgentRunner(
        cfg,
        Backend("# 订单\n业务规则"),
        str(tmp_path),
        components,
        tree,
        SimpleNamespace(),
        SimpleNamespace(agent_may_patch_leaf=True, agent_patches_related=True),
        record,
    )
    asyncio.run(runner._regenerate_leaf("订单", ["订单"], [cid], "rewrite"))
    assert record.pages_written == ["订单"]
    assert record.verdicts[0]["changed_on_disk"] is True

    empty = UpdateRecord(options={})
    runner = LeafAgentRunner(
        cfg,
        Backend("   "),
        str(tmp_path),
        components,
        tree,
        SimpleNamespace(),
        SimpleNamespace(agent_may_patch_leaf=True, agent_patches_related=True),
        empty,
    )
    asyncio.run(runner._regenerate_leaf("订单", ["订单"], [cid], "rewrite"))
    assert empty.pages_written == []
    assert "completion validation failed" in empty.verdicts[0]["reason"]
    assert empty.verdicts[0]["changed_on_disk"] is False
