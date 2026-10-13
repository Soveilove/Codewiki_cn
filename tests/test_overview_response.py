# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Overview transport formatting does not turn valid Markdown into warnings."""

import asyncio
import json
import logging
import pytest

from codewiki.src.be.backend import LLMBackend
from codewiki.src.be.documentation_generator import (
    DocumentationGenerator,
    _parse_overview_response,
)
from codewiki.src.be.prompt_template import (
    MODULE_OVERVIEW_PROMPT,
    REPO_OVERVIEW_PROMPT,
    format_overview_system_prompt,
)
from codewiki.src.config import Config


@pytest.mark.parametrize(
    "response",
    [
        "<OVERVIEW>\n# 概览\n\n正文\n</OVERVIEW>",
        "# 概览\n\n正文",
        "```markdown\n# 概览\n\n正文\n```",
        "~~~md\n# 概览\n\n正文\n~~~",
        "<OVERVIEW>\n```markdown\n# 概览\n\n正文\n```\n</OVERVIEW>",
        "```xml\n<OVERVIEW>\n# 概览\n\n正文\n</OVERVIEW>\n```",
    ],
)
def test_valid_overview_formats_do_not_warn(response, caplog):
    with caplog.at_level(logging.WARNING):
        assert _parse_overview_response(response, "身份、组织与基础数据服务") == "# 概览\n\n正文"
    assert not caplog.records


def test_internal_code_fences_are_preserved():
    content = "# 概览\n\n```python\ndef example():\n    pass\n```"
    assert _parse_overview_response(content, "模块") == content


@pytest.mark.parametrize(
    "response",
    ["", "  \n", "<OVERVIEW></OVERVIEW>", "```markdown\n\n```"],
)
def test_empty_overviews_are_rejected(response):
    with pytest.raises(RuntimeError, match="empty content"):
        _parse_overview_response(response, "模块")


@pytest.mark.parametrize(
    "response", ["<OVERVIEW>\n# 截断", "# 概览\n</OVERVIEW>", "</OVERVIEW><OVERVIEW>"]
)
def test_incomplete_envelope_is_not_saved_as_raw_markdown(response):
    with pytest.raises(RuntimeError, match="Incomplete <OVERVIEW>"):
        _parse_overview_response(response, "模块")


def test_nonstandard_response_still_warns(caplog):
    with caplog.at_level(logging.WARNING):
        assert (
            _parse_overview_response("Unexpected provider response", "模块")
            == "Unexpected provider response"
        )
    assert "review the generated page" in caplog.text


def test_localized_title_belongs_inside_envelope():
    system = format_overview_system_prompt("Start each page with a Chinese # heading.")
    assert system is not None
    assert "Markdown INSIDE the envelope" in system
    for prompt in [MODULE_OVERVIEW_PROMPT, REPO_OVERVIEW_PROMPT]:
        assert "transport delimiters" in prompt
        assert "INSIDE the tags" in prompt


@pytest.mark.parametrize("module_path", [[], ["教学与学习服务"]])
@pytest.mark.parametrize(
    "response, expected",
    [
        ("# 中文概览\n\n正文", "# 中文概览\n\n正文"),
        ("<OVERVIEW># 中文概览\n\n正文</OVERVIEW>", "# 中文概览\n\n正文"),
        ("```markdown\n# 中文概览\n\n正文\n```", "# 中文概览\n\n正文"),
        ("<OVERVIEW># 截断", None),
        ("<OVERVIEW> </OVERVIEW>", None),
    ],
)
def test_parent_and_repo_overview_save_only_parsed_content(
    tmp_path, caplog, module_path, response, expected
):
    class Backend(LLMBackend):
        def complete(self, prompt, *, model=None, system_prompt=None):
            assert system_prompt and "Markdown INSIDE the envelope" in system_prompt
            return response

        async def run_module_agent(
            self, module_name, components, core_component_ids, module_path, working_dir
        ):
            raise AssertionError("Overview must not invoke module agent")

    tree = {"教学与学习服务": {"components": [], "children": {}}}
    (tmp_path / "module_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    if not module_path:
        (tmp_path / "教学与学习服务.md").write_text(
            "# 教学与学习服务\n子页面内容", encoding="utf-8"
        )
    generator = object.__new__(DocumentationGenerator)
    generator.config = Config.from_cli(
        repo_path=str(tmp_path / "repo"),
        output_dir=str(tmp_path),
        llm_base_url="http://localhost",
        llm_api_key="test",
        main_model="m",
        cluster_model="m",
        agent_instructions={"language": "zh"},
    )
    generator.backend = Backend()
    page = tmp_path / ("教学与学习服务.md" if module_path else "overview.md")
    if expected is None:
        with pytest.raises(RuntimeError):
            asyncio.run(generator.generate_parent_module_docs(module_path, str(tmp_path)))
        assert not page.exists()
    else:
        with caplog.at_level(logging.WARNING):
            asyncio.run(generator.generate_parent_module_docs(module_path, str(tmp_path)))
        assert page.read_text(encoding="utf-8") == expected
        assert not caplog.records
        asyncio.run(generator.generate_parent_module_docs(module_path, str(tmp_path)))
        assert page.read_text(encoding="utf-8") == expected
