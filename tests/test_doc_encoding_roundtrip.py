# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Documents must survive editing unchanged in encoding.

The editor used to try the locale default encoding before UTF-8. On a Chinese
Windows console that is GBK, and many UTF-8 Chinese strings are also valid
GBK, so the file was read as mojibake without any error and written straight
back as UTF-8 — a silent, permanent corruption that made the page
unopenable. The encoding was also kept as a single instance attribute, so the
result depended on which file was edited before.
"""

from pathlib import Path

from codewiki.src.be.agent_tools.str_replace_editor import EditTool

# Windows converts "\n" to "\r\n" on write; that is expected and unrelated.
NL = "\r\n"


def editor(tmp_path: Path) -> EditTool:
    return EditTool(REGISTRY={}, absolute_docs_path=str(tmp_path))


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


def read_again(instance: EditTool, path: Path) -> str:
    """Round-trip a page and assert the text survived unchanged."""
    before = instance.read_file(path)
    assert before is not None
    assert instance.write_file(path, before) is True
    after = instance.read_file(path)
    assert normalize(after) == normalize(before)
    return after


def test_utf8_text_that_is_also_valid_gbk_is_not_decoded_as_gbk(tmp_path):
    """This exact phrase decodes without error as GBK — the corruption case."""
    assert "配置文件解析模块".encode().decode("gbk") != "配置文件解析模块"

    path = tmp_path / "page.md"
    path.write_bytes("# 配置文件解析模块\n".encode())
    instance = editor(tmp_path)
    assert read_again(instance, path) == "# 配置文件解析模块\n"
    assert path.read_bytes().decode("utf-8").replace(NL, "\n") == "# 配置文件解析模块\n"


def test_encoding_is_tracked_per_file_not_shared(tmp_path):
    """Editing a UTF-8 page must not change how the next page is written."""
    ascii_page = tmp_path / "english.md"
    chinese_page = tmp_path / "chinese.md"
    ascii_page.write_bytes(b"# Orders\n\nPure ASCII.\n")
    chinese_page.write_bytes("# 配置文件解析模块\n\n金额标注币种。\n".encode())

    instance = editor(tmp_path)
    for page in (chinese_page, ascii_page, chinese_page):
        read_again(instance, page)

    assert ascii_page.read_bytes().decode("utf-8").replace(NL, "\n") == "# Orders\n\nPure ASCII.\n"
    assert (
        chinese_page.read_bytes().decode("utf-8").replace(NL, "\n")
        == "# 配置文件解析模块\n\n金额标注币种。\n"
    )


def test_utf8_bom_file_keeps_its_bom(tmp_path):
    path = tmp_path / "bom.md"
    path.write_bytes(b"\xef\xbb\xbf" + "# 项目\n".encode())
    instance = editor(tmp_path)
    text = instance.read_file(path)
    assert text == "# 项目\n"
    assert instance.write_file(path, text) is True
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_missing_file_is_reported_not_raised(tmp_path):
    assert editor(tmp_path).read_file(tmp_path / "nope.md") is None


def test_undecodable_bytes_do_not_crash_the_editor(tmp_path):
    path = tmp_path / "binary.md"
    path.write_bytes(b"\xff\xfe\x00\x01binary")
    read_again(editor(tmp_path), path)


def test_new_file_defaults_to_utf8(tmp_path):
    path = tmp_path / "fresh.md"
    instance = editor(tmp_path)
    assert instance.write_file(path, "# 新建页面\n") is True
    assert path.read_bytes().decode("utf-8").replace(NL, "\n") == "# 新建页面\n"
