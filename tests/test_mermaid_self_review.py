# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Mermaid 图不再有独立语法校验，改由生成模型自查。

The Node.js parser was removed: it forced a JavaScript runtime on every
install just to re-check a handful of diagrams. What must survive the removal
is the instruction that makes the writing model review its own diagrams in the
same step, and the guarantee that no code path still calls a parser.
"""

import asyncio
import json

import pytest

from codewiki.src.be import prompt_template as prompts


@pytest.mark.parametrize("kind", ["module", "leaf", "repository_overview", "module_overview"])
def test_generation_prompts_require_self_review(kind):
    formatted = {
        "module": prompts.format_system_prompt("订单管理"),
        "leaf": prompts.format_leaf_system_prompt("订单管理"),
        "repository_overview": prompts.REPO_OVERVIEW_PROMPT.format(
            repo_name="demo", repo_structure="x"
        ),
        "module_overview": prompts.MODULE_OVERVIEW_PROMPT.format(
            module_name="订单管理", repo_structure="x"
        ),
    }[kind]
    assert prompts.MERMAID_SELF_REVIEW in formatted


def test_self_review_does_not_claim_tool_verification():
    review = prompts.MERMAID_SELF_REVIEW
    # The model must not describe self-review as parsing or rendering.
    assert "Do not launch a separate review agent" in review
    assert "do not claim diagrams passed tool validation" in review
    assert "make extra model calls" in review


def test_update_prompt_tells_the_agent_to_keep_diagrams_valid():
    """The incremental updater still edits existing pages with diagrams."""
    from codewiki.src.be.updater.prompts import format_update_system_prompt

    system_prompt = format_update_system_prompt("订单管理", None)
    assert "Mermaid diagrams must stay valid" in system_prompt


def test_no_mermaid_parser_remains_in_the_backend():
    """Deleting the feature must not leave a half-wired code path behind."""
    from pathlib import Path

    import importlib

    root = Path(__file__).resolve().parents[1]
    for relative in (
        "codewiki/src/be/utils.py",
        "codewiki/src/be/agent_tools/str_replace_editor.py",
        "codewiki/src/be/caw_toolkit.py",
        "codewiki/mcp/tools/doc_writer.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "validate_mermaid_diagrams" not in source, relative
        assert "_parse_via_local_node" not in source, relative

    assert not hasattr(
        importlib.import_module("codewiki.src.be.utils"), "validate_mermaid_diagrams"
    )

    from codewiki.mcp.tools import doc_writer

    assert doc_writer.MERMAID_STATUS.startswith("未做独立语法校验")


def test_mcp_write_reports_that_diagrams_were_not_validated(tmp_path):
    from codewiki.mcp.session import SessionStore
    from codewiki.mcp.tools.doc_writer import handle_write_doc_file
    from codewiki.mcp.workspace import SessionWorkspace

    repo = tmp_path / "repo"
    repo.mkdir()
    store = SessionStore()
    session = store.create(str(repo), str(repo / ".codewiki"), {}, [])
    session.workspace = SessionWorkspace(repo, session.session_id)
    result = json.loads(
        asyncio.run(
            handle_write_doc_file(
                {
                    "session_id": session.session_id,
                    "filename": "page.md",
                    "content": "# 页面\n\n```mermaid\ngraph TD; A-->B\n```\n",
                },
                store,
            )
        )
    )
    assert result["status"] == "created"
    assert "未做独立语法校验" in result["mermaid_validation"]
    session.workspace.cleanup()


def test_node_is_not_a_declared_dependency():
    """The Node-based parser must not come back as a dependency."""
    from importlib.metadata import PackageNotFoundError, distribution

    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = list(data["project"]["dependencies"])
    for group in data["project"].get("optional-dependencies", {}).values():
        declared += list(group)
    assert not any("mermaid" in item for item in declared), declared

    try:
        distribution("codewiki")
    except PackageNotFoundError:
        pass


def test_no_mermaid_runner_script_ships():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert not list(root.glob("codewiki/**/*.cjs"))
