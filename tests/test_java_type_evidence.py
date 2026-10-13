# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Java data contracts keep their declaration and type-use evidence."""

from codewiki.src.be.dependency_analyzer.analyzers.java import analyze_java_file


def test_signature_and_generic_arguments_expose_payload_usage(tmp_path):
    source = """package sample;
import java.util.List;
import java.util.Map;
class StudentInfoService<T> {
    private Map<Long, SearchUserRespDTO.UserInfo> users;
    public List<? extends SearchUserRespDTO[]> search(SearchUserReqDTO request, T context) {
        return null;
    }
    public void notify(TaskCancelMqNotify... messages) {}
}
"""
    nodes, relationships = analyze_java_file(
        str(tmp_path / "StudentInfoService.java"), source, str(tmp_path)
    )
    edges = {(rel.caller, rel.callee) for rel in relationships}
    assert (
        "StudentInfoService.java::StudentInfoService.search",
        "sample.SearchUserReqDTO",
    ) in edges
    assert (
        "StudentInfoService.java::StudentInfoService.search",
        "sample.SearchUserRespDTO",
    ) in edges
    assert (
        "StudentInfoService.java::StudentInfoService.notify",
        "sample.TaskCancelMqNotify",
    ) in edges
    assert (
        "StudentInfoService.java::StudentInfoService",
        "sample.SearchUserRespDTO.UserInfo",
    ) in edges
    assert not any(
        rel.callee in {"sample.T", "sample.List", "sample.Map", "sample.Long"}
        for rel in relationships
    )
    assert any(node.name == "StudentInfoService.search" for node in nodes)


def test_nested_declarations_and_javadoc_survive_the_graph_adapter(tmp_path):
    from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser
    from codewiki.src.be.dependency_analyzer.analysis.call_graph_analyzer import CallGraphAnalyzer

    source = """package sample;
import java.util.List;
/** 学员全局查询的结果。 */
class SearchUserRespDTO {
    private List<UserInfo> users;
    /** 查询结果中的单个学员。 */
    static class UserInfo { private String name; }
}
class Companion { }
"""
    nodes, relationships = analyze_java_file(
        str(tmp_path / "SearchUserRespDTO.java"), source, str(tmp_path)
    )
    outer = next(n for n in nodes if n.name == "SearchUserRespDTO")
    inner = next(n for n in nodes if n.name == "UserInfo")
    companion = next(n for n in nodes if n.name == "Companion")
    assert inner.class_name == "SearchUserRespDTO"
    assert companion.class_name is None
    assert "学员全局查询" in outer.docstring
    assert "单个学员" in inner.docstring
    analyzer = CallGraphAnalyzer()
    analyzer.functions = {n.id: n for n in nodes}
    analyzer.call_relationships = relationships
    analyzer._resolve_call_relationships()
    parser = DependencyParser(str(tmp_path))
    parser._build_components_from_analysis(
        {
            "functions": [n.model_dump() for n in nodes],
            "relationships": [r.model_dump() for r in analyzer.call_relationships],
        }
    )
    assert inner.id in parser.components[outer.id].depends_on
    assert parser.components[inner.id].qualified_name == "sample.SearchUserRespDTO.UserInfo"
    assert parser.components[inner.id].language == "java"


def test_local_creation_is_attributed_to_its_method_not_the_whole_file(tmp_path):
    source = """package sample;
class TaskService {
    void cancel() { TaskCancelMqNotify payload = new TaskCancelMqNotify(); }
}
class Companion { void unrelated() {} }
"""
    _, relationships = analyze_java_file(str(tmp_path / "TaskService.java"), source, str(tmp_path))
    assert any(
        rel.caller == "TaskService.java::TaskService.cancel"
        and rel.callee == "sample.TaskCancelMqNotify"
        for rel in relationships
    )


def test_jdk_wildcard_does_not_hide_project_payloads(tmp_path):
    from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser

    (tmp_path / "Event.java").write_text("package sample; class Event {}", encoding="utf-8")
    (tmp_path / "Worker.java").write_text(
        """package sample;
import java.util.*;
class Worker { List<Event> handle(Event input) { return null; } }
""",
        encoding="utf-8",
    )
    components = DependencyParser(str(tmp_path)).parse_repository()
    method = components["Worker.java::Worker.handle"]
    assert "Event.java::Event" in method.type_depends_on


def test_wildcard_project_nested_types_resolve_without_colliding_inner_names(tmp_path):
    from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser

    (tmp_path / "Result.java").write_text(
        "package sample.dto;\nclass Result { static class Detail { Long id; } }\n",
        encoding="utf-8",
    )
    (tmp_path / "Other.java").write_text(
        "package sample.other;\nclass Other { static class Detail {} }\n",
        encoding="utf-8",
    )
    (tmp_path / "Worker.java").write_text(
        """package sample.worker;
import java.util.*;
import sample.dto.*;
class Worker { List<Result.Detail> handle(Result.Detail payload) { return null; } }
""",
        encoding="utf-8",
    )
    components = DependencyParser(str(tmp_path)).parse_repository()
    assert "Result.java::Detail" in components["Worker.java::Worker.handle"].type_depends_on
    assert "Other.java::Detail" not in components["Worker.java::Worker.handle"].depends_on


def test_type_evidence_roundtrips_through_both_graph_writers(tmp_path):
    from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser
    from codewiki.src.be.updater.graph_store import load_graph, save_graph

    (tmp_path / "Event.java").write_text("package sample; class Event {}", encoding="utf-8")
    (tmp_path / "Worker.java").write_text(
        "package sample; class Worker { void handle(Event event) {} }",
        encoding="utf-8",
    )
    parser = DependencyParser(str(tmp_path))
    components = parser.parse_repository()
    for name in ("initial.json", "published.json"):
        path = str(tmp_path / name)
        if name == "initial.json":
            parser.save_dependency_graph(path)
        else:
            save_graph(components, path)
        loaded = load_graph(path)
        assert loaded["Worker.java::Worker.handle"].type_depends_on == {"Event.java::Event"}
        assert loaded["Worker.java::Worker.handle"].language == "java"
        assert loaded["Worker.java::Worker.handle"].qualified_name == "sample.Worker.handle"
