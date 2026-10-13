# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Scoped slim module tree and dynamic dependency prefetch in user prompts.

Cost facts these tests lock in:
- the page prompt carries page names/hierarchy plus a dependency->page
  ownership index, NOT every other page's component list (measured ~40k
  tokens/request of re-sent ownership data on a 75-page tree);
- direct dependencies fill the REMAINING prompt budget (small files first),
  because every deferred dependency is forced back as a full-context repair
  turn;
- direct write (<SOURCE_COMPLETE>) is promised only when assigned files are
  complete AND no dependency was deferred.
"""

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.llm_services import measure_messages
from codewiki.src.be.prompt_template import format_user_prompt


def _node(tmp_path, cid, body, deps=(), kind="class"):
    rel_path, _, name = cid.partition("::")
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return Node(
        id=cid,
        name=name,
        component_type=kind,
        relative_path=rel_path,
        file_path=str(full),
        source_code=body,
        depends_on=set(deps),
    )


def _tree_section(prompt: str) -> str:
    return prompt.split("<MODULE_TREE>", 1)[1].split("</MODULE_TREE>", 1)[0]


def test_module_tree_is_names_only_and_keeps_other_page_names(tmp_path):
    order = _node(tmp_path, "a/Order.java::Order", "class Order {}")
    pay = _node(tmp_path, "b/Pay.java::Pay", "class Pay {}")
    tree = {
        "订单": {"components": ["a/Order.java::Order"]},
        "支付": {"components": ["b/Pay.java::Pay"]},
    }
    prompt = format_user_prompt(
        "订单",
        ["a/Order.java::Order"],
        {"a/Order.java::Order": order, "b/Pay.java::Pay": pay},
        tree,
        ["订单"],
        business_analysis=True,
        required_source_ids=set(),
        prompt_read_ids=set(),
    )
    section = _tree_section(prompt)
    assert "支付" in section, "other pages stay reachable for cross-links"
    assert "订单" in section
    assert "b/Pay.java::Pay" not in section, "other pages' component IDs must be gone"
    assert "a/Order.java::Order" not in section, "even this page's ID list is dropped"


def test_dependency_ownership_index_maps_only_cross_page_dependencies(tmp_path):
    pay = _node(tmp_path, "b/Pay.java::Pay", "class Pay {}")
    order = _node(
        tmp_path,
        "a/Order.java::Order",
        "class Order { Pay p; }",
        deps=["b/Pay.java::Pay"],
    )
    tree = {
        "订单": {"components": ["a/Order.java::Order"]},
        "支付": {"components": ["b/Pay.java::Pay"]},
    }
    reads, required, deferred = set(), set(), set()
    prompt = format_user_prompt(
        "订单",
        ["a/Order.java::Order"],
        {"a/Order.java::Order": order, "b/Pay.java::Pay": pay},
        tree,
        ["订单"],
        business_analysis=True,
        required_source_ids=required,
        prompt_read_ids=reads,
        deferred_source_ids=deferred,
    )
    assert "<DEPENDENCY_OWNERSHIP>" in prompt
    assert "b/Pay.java::Pay -> 支付" in prompt
    assert "a/Order.java::Order -> 订单" not in prompt, "current page is not cross-page"
    # The small dependency is inlined and evidenced without a tool call.
    assert "# File: b/Pay.java" in prompt
    assert "b/Pay.java::Pay" in reads
    assert deferred == set()
    assert "SOURCE_COMPLETE" in prompt


def test_dependency_prefetch_uses_remaining_budget_beyond_old_cap(tmp_path):
    # ~200k chars: the OLD fixed 150k dependency cap deferred this file; the
    # dynamic budget must inline it because the prompt is nowhere near the
    # 900k hard limit.
    body = "class Payment { int field; // " + ("x" * 200_000) + "\n}"
    payment = _node(tmp_path, "svc/Payment.java::Payment", body)
    order = _node(
        tmp_path,
        "svc/Order.java::Order",
        "class Order { Payment pay; }",
        deps=["svc/Payment.java::Payment"],
    )
    reads, required, deferred = set(), set(), set()
    prompt = format_user_prompt(
        "订单",
        ["svc/Order.java::Order"],
        {"svc/Order.java::Order": order, "svc/Payment.java::Payment": payment},
        {"订单": {"components": ["svc/Order.java::Order", "svc/Payment.java::Payment"]}},
        ["订单"],
        business_analysis=True,
        required_source_ids=required,
        prompt_read_ids=reads,
        deferred_source_ids=deferred,
    )
    assert deferred == set()
    assert "svc/Payment.java::Payment" in reads
    assert "# File: svc/Payment.java" in prompt
    assert "SOURCE_COMPLETE" in prompt
    assert len(prompt) <= 900_000 + 5_000


def test_deferred_dependency_blocks_direct_write(tmp_path, monkeypatch):
    from codewiki.src.be import prompt_template as prompts

    monkeypatch.setattr(prompts, "MAX_USER_PROMPT_CHARS", 12_000)
    dep_body = "class Wallet { " + "long field;" * 2000 + " }"
    wallet = _node(tmp_path, "svc/Wallet.java::Wallet", dep_body)
    order = _node(
        tmp_path,
        "svc/Order.java::Order",
        "class Order { Wallet w; }",
        deps=["svc/Wallet.java::Wallet"],
    )
    reads, required, deferred = set(), set(), set()
    prompt = format_user_prompt(
        "订单",
        ["svc/Order.java::Order"],
        {"svc/Order.java::Order": order, "svc/Wallet.java::Wallet": wallet},
        {"订单": {"components": ["svc/Order.java::Order", "svc/Wallet.java::Wallet"]}},
        ["订单"],
        business_analysis=True,
        required_source_ids=required,
        prompt_read_ids=reads,
        deferred_source_ids=deferred,
    )
    assert "svc/Wallet.java::Wallet" in deferred
    assert "svc/Wallet.java::Wallet" not in reads, "a deferred file is not read evidence"
    assert "SOURCE_COMPLETE" not in prompt
    assert "NOT in this prompt" in prompt


def test_small_dependency_files_win_budget_first(tmp_path, monkeypatch):
    from codewiki.src.be import prompt_template as prompts

    # Own source ~12k; cap 30k leaves roughly enough for the small dependency
    # but not the large one.
    monkeypatch.setattr(prompts, "MAX_USER_PROMPT_CHARS", 30_000)
    small = _node(tmp_path, "svc/Small.java::Small", "class Small {}")
    big = _node(
        tmp_path,
        "svc/Big.java::Big",
        "class Big { " + "long field;" * 3000 + " }",
    )
    order = _node(
        tmp_path,
        "svc/Order.java::Order",
        "class Order { Small s; Big b; } " + "// own\n" * 600,
        deps=["svc/Small.java::Small", "svc/Big.java::Big"],
    )
    components = {
        "svc/Order.java::Order": order,
        "svc/Small.java::Small": small,
        "svc/Big.java::Big": big,
    }
    deferred = set()
    prompt = format_user_prompt(
        "订单",
        ["svc/Order.java::Order"],
        components,
        {"订单": {"components": list(components)}},
        ["订单"],
        business_analysis=True,
        required_source_ids=set(),
        prompt_read_ids=set(),
        deferred_source_ids=deferred,
    )
    assert "# File: svc/Small.java" in prompt
    provided = prompt.split("SOURCE_ALREADY_PROVIDED")[1]
    assert "svc/Small.java" in provided
    assert "# File: svc/Big.java" not in prompt
    assert "svc/Big.java::Big" in deferred


def test_measure_messages_splits_first_prompt_sections():
    content = (
        "<MODULE_TREE>\nx\n</MODULE_TREE>\n<CORE_COMPONENT_CODES>\nyyy\n</CORE_COMPONENT_CODES>"
    )
    result = measure_messages([{"role": "user", "content": content}])
    assert result["prompt_tree"] == 1
    assert result["prompt_code"] == 3
    # Sections are subsets of prompt, not extra buckets.
    assert result["prompt"] == len(content)
    assert sum(result[k] for k in ("system", "prompt", "user_extra", "assistant", "tool")) == len(
        content
    )
