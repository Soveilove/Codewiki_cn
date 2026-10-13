# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Direct-write directive: only when every needed source is already in full."""

import pytest

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.prompt_template import format_user_prompt

BIG = "class Big:\n" + "    def x(self):\n        pass\n" * 400


def write(tmp_path, name, code):
    """Create a real file: the prompt inlines source by reading it from disk."""
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code, encoding="utf-8")
    return str(target)


def node(relative, absolute, name, code):
    return Node(
        id=f"{relative}::{name}",
        relative_path=relative,
        file_path=absolute,
        name=name,
        source_code=code,
        component_type="class",
    )


def build(tmp_path, specs):
    components = {}
    for relative, code in specs.items():
        name = relative.rsplit("/", 1)[-1].removesuffix(".java")
        components[f"{relative}::{name}"] = node(
            relative, write(tmp_path, relative, code), name, code
        )
    return components


def render(components, business_analysis=True, **kwargs):
    core = list(components)
    return format_user_prompt(
        "订单",
        core,
        components,
        {"订单": {"components": core, "children": {}}},
        ["订单"],
        "hierarchical",
        business_analysis=business_analysis,
        **kwargs,
    )


def test_direct_write_note_appears_when_all_sources_are_complete(tmp_path):
    components = build(
        tmp_path,
        {
            "a/A.java": "class A:\n    pass\n",
            "b/B.java": "class B:\n    pass\n",
        },
    )
    prompt = render(components)
    assert "<SOURCE_COMPLETE>" in prompt
    assert "str_replace_editor create" in prompt
    assert "<SOURCE_ALREADY_PROVIDED>" in prompt


def test_fully_provided_module_needs_no_reading(tmp_path):
    components = build(
        tmp_path,
        {
            "a/A.java": "class A:\n    pass\n",
            "b/B.java": "class B:\n    pass\n",
        },
    )
    required, provided = set(), set()
    render(components, required_source_ids=required, prompt_read_ids=provided)
    assert required == set()
    assert provided == set(components)


def test_splitting_clause_only_when_delegation_is_available(tmp_path):
    components = build(tmp_path, {"a/A.java": "class A:\n    pass\n"})
    write_now = "go straight to str_replace_editor create"

    allowed = render(components, can_delegate=True)
    assert "<SOURCE_COMPLETE>" in allowed
    assert write_now in allowed
    assert "generate_sub_module_documentation" in allowed

    # A module that cannot delegate must not be told to call a missing tool:
    # naming an unregistered tool only burns a request.
    blocked = render(components, can_delegate=False)
    assert "<SOURCE_COMPLETE>" in blocked
    assert write_now in blocked
    assert "generate_sub_module_documentation" not in blocked
    assert "split it" not in blocked


def test_no_direct_write_note_when_a_file_was_truncated(tmp_path, monkeypatch):
    components = build(
        tmp_path,
        {
            "a/A.java": "class A:\n    pass\n",
            "big/Big.java": BIG,
        },
    )
    # A cap the small file survives but the large one does not: the truncation
    # budget is the whole inlined payload, so it has to leave room for the head.
    monkeypatch.setattr("codewiki.src.be.prompt_template.MAX_USER_PROMPT_CHARS", 3_000)
    required = set()
    prompt = render(components, required_source_ids=required)
    assert "<SOURCE_COMPLETE>" not in prompt
    assert "<SOURCE_ALREADY_PROVIDED>" in prompt
    # The cut file is partial evidence, so it is still reported as required...
    assert "big/Big.java::Big" in required
    # ...while the file that survived in full does not need re-reading.
    assert "a/A.java::A" not in required


def test_everything_cut_off_leaves_no_provided_files(tmp_path, monkeypatch):
    components = build(tmp_path, {"big/Big.java": BIG})
    monkeypatch.setattr("codewiki.src.be.prompt_template.MAX_USER_PROMPT_CHARS", 500)
    required, provided = set(), set()
    prompt = render(components, required_source_ids=required, prompt_read_ids=provided)
    assert provided == set()
    assert required == {"big/Big.java::Big"}
    assert "<SOURCE_ALREADY_PROVIDED>" not in prompt


def test_no_direct_write_note_when_a_file_is_absent(tmp_path):
    components = build(tmp_path, {"a/A.java": "class A:\n    pass\n"})
    components["gone/Gone.java::Gone"] = Node(
        id="gone/Gone.java::Gone",
        relative_path="gone/Gone.java",
        file_path=str(tmp_path / "gone" / "Gone.java"),
        name="Gone",
        source_code="class Gone: pass",
        component_type="class",
    )
    required = set()
    prompt = render(components, required_source_ids=required)
    assert "<SOURCE_COMPLETE>" not in prompt
    assert "gone/Gone.java::Gone" in required


def test_no_direct_write_note_without_business_analysis(tmp_path):
    components = build(tmp_path, {"a/A.java": "class A:\n    pass\n"})
    assert "<SOURCE_COMPLETE>" not in render(components, business_analysis=False)


def test_dependency_budget_measures_full_file_not_method_snippet(tmp_path):
    # The inline budget is measured against the WHOLE dependency file size, not
    # the single method component's snippet: the evidence validator accepts a
    # full file or an explicit read, never a fragment. A file larger than the
    # remaining ~870k-char budget stays deferred even though the referenced
    # method body is tiny.
    components = build(tmp_path, {"Owner.java": "class Owner {}\n" * 10})
    dependency_path = write(tmp_path, "Dependency.java", "class Dependency {}\n" * 45_000)
    dependency = node("Dependency.java", dependency_path, "method", "void method() {}")
    components[dependency.id] = dependency
    components["Owner.java::Owner"].depends_on = {dependency.id}
    reads, deferred = set(), set()
    prompt = format_user_prompt(
        "订单",
        ["Owner.java::Owner"],
        components,
        {"订单": {"components": ["Owner.java::Owner"]}},
        ["订单"],
        "hierarchical",
        business_analysis=True,
        prompt_read_ids=reads,
        deferred_source_ids=deferred,
    )
    assert "# File: Dependency.java" not in prompt
    assert dependency.id in prompt, "deferred IDs stay listed for a deliberate read"
    assert dependency.id in deferred
    assert dependency.id not in reads
    assert "NOT in this prompt" in prompt
    assert "<SOURCE_COMPLETE>" not in prompt


@pytest.mark.parametrize("formatter", ["format_system_prompt", "format_leaf_system_prompt"])
def test_system_workflow_preserves_deferred_dependency_reads(formatter):
    import codewiki.src.be.prompt_template as prompts

    text = getattr(prompts, formatter)("订单", module_path=["订单"], direct_write=True)
    assert "{doc_file}" not in text
    assert "Inspect deferred or missing dependencies" in text
    assert "redundant exploration" in text


def test_direct_write_requires_assigned_paths(tmp_path):
    # Nothing assigned to this module: there is no source to declare complete.
    components = build(tmp_path, {"a/A.java": "class A:\n    pass\n"})
    assert "<SOURCE_COMPLETE>" not in render({}, business_analysis=True)
    assert list(components)
