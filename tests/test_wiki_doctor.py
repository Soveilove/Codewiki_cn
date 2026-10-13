# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Deterministic wiki page doctor: byte/fence/link repair without any agent."""

from codewiki.src.be.wiki_doctor import doctor_wiki


def _write(tmp_path, name, raw):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(raw, bytes):
        path.write_bytes(raw)
    else:
        path.write_text(raw, encoding="utf-8")
    return path


def test_bom_invalid_utf8_and_control_chars_are_repaired(tmp_path):
    # BOM + a NUL control byte + an invalid UTF-8 continuation byte.
    path = _write(tmp_path, "页.md", b"\xef\xbb\xbf# \xe6\xa0\x87\xe9\xa2\x98\x00\nbody \x80 tail")
    report = doctor_wiki(tmp_path, {})
    assert report.scanned == 1
    assert any(k == "编码/BOM/控制字符" for _, k in report.fixed)
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = path.read_text(encoding="utf-8")
    assert "\x00" not in text
    assert "# 标题" in text
    assert report.healthy


def test_unclosed_code_fence_is_closed(tmp_path):
    path = _write(tmp_path, "图.md", "# 图\n\n```mermaid\nflowchart TD\n A-->B\n")
    report = doctor_wiki(tmp_path, {})
    assert any("代码围栏" in k for _, k in report.fixed)
    text = path.read_text(encoding="utf-8")
    assert text.rstrip().endswith("```")


def test_balanced_and_tilde_fences_are_left_alone(tmp_path):
    original = "# x\n\n~~~\n```not a fence, tilde owns it\n~~~\n\n```mermaid\nA-->B\n```\n"
    path = _write(tmp_path, "页.md", original)
    report = doctor_wiki(tmp_path, {})
    assert path.read_text(encoding="utf-8") == original
    assert not report.fixed
    assert report.healthy


def test_backslash_links_normalized(tmp_path):
    path = _write(tmp_path, "父.md", "# 父\n[子](文件夹\\子页.md)\n")
    doctor_wiki(tmp_path, {})
    assert "[子](文件夹/子页.md)" in path.read_text(encoding="utf-8")


def test_empty_page_is_reported_not_fabricated(tmp_path):
    path = _write(tmp_path, "空.md", "  \n\t\n")
    report = doctor_wiki(tmp_path, {})
    assert any("为空" in issue for _, issue in report.warnings)
    assert path.read_text(encoding="utf-8").strip() == ""
    assert not report.healthy


def test_dangling_and_directory_links_are_reported(tmp_path):
    _write(tmp_path, "存在.md", "# 存在\n[没了](幽灵.md)\n[夹](子目录/页面.md)\n")
    # A directory that ends in .md is a real (if odd) directory target.
    (tmp_path / "子目录" / "页面.md").mkdir(parents=True)
    tree = {"存在": {"components": []}}
    report = doctor_wiki(tmp_path, tree)
    issues = " ".join(i for _, i in report.warnings)
    assert "幽灵.md" in issues
    assert "目录" in issues


def test_healthy_wiki_has_no_findings(tmp_path):
    _write(tmp_path, "a.md", "# A\n正文\n")
    _write(tmp_path, "b.md", "# B\n[A](a.md)\n```\ncode\n```\n")
    report = doctor_wiki(tmp_path, {"a": {"components": []}, "b": {"components": []}})
    assert report.scanned == 2
    assert report.warnings == []
    assert report.fixed == []
    assert "扫描 2" in report.summary_line()


def test_temp_and_backup_dirs_are_excluded(tmp_path):
    (tmp_path / "temp").mkdir()
    _write(tmp_path, "temp/scratch.md", "\x00broken")
    (tmp_path / ".prev-module-tree").mkdir()
    _write(tmp_path, ".prev-module-tree/old.md", "```unclosed")
    _write(tmp_path, "正式.md", "# ok")
    report = doctor_wiki(tmp_path, {})
    assert report.scanned == 1
    assert report.healthy


def test_unreadable_entry_never_aborts_the_scan(tmp_path):
    _write(tmp_path, "好.md", "# ok\n")
    # A directory whose name ends in .md is listed by rglob but cannot be
    # read as bytes: it becomes a warning while the good page still scans.
    (tmp_path / "坏.md").mkdir()
    report = doctor_wiki(tmp_path, {})
    assert report.scanned == 2
    rels = {rel for rel, _ in report.warnings}
    assert "好.md" not in rels
    assert any(rel == "坏.md" for rel in rels)
