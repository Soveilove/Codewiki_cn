"""Deterministic, agent-free health check for generated wiki pages.

Runs on every init/update regardless of cache: a preview that cannot open a
page (bad encoding, unbalanced code fences swallowing the rest of the
document, empty files, dead links) must be repaired deterministically instead
of spending model calls or relying on Node tooling.

Scope is deliberately structural — bytes, fences, links — not prose or Mermaid
semantics (Mermaid syntax is explicitly not validated by the backend).
Everything safe to fix is fixed in place; everything else is reported.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from codewiki.src.be.doc_layout import find_dangling_links

logger = logging.getLogger(__name__)

# Working tree files never count as deliverable pages.
_EXCLUDED_DIRS = {"temp", "sessions", ".git", ".prev-module-tree", "node_modules"}
_EXCLUDED_FILES = {"prompts.md"}
# C0 controls that break terminals/previews; tab/newline/CR are legitimate.
_ILLEGAL_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FENCE_LINE = re.compile(r"^(\s*)(`{3,}|~{3,})(?P<info>.*)$")
# [text](target...) — internal targets with a Windows-style backslash path.
_BACKSLASH_LINK = re.compile(r"(\]\(\s*)([^)\s<>#][^)\n<>]*?)(\s*(?:#[^)\n]*)?\))")
_EMPTY_MERMAID = re.compile(r"```mermaid[ \t]*\n[ \t]*```", re.IGNORECASE)


@dataclass
class DoctorReport:
    scanned: int = 0
    fixed: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.warnings

    def summary_line(self) -> str:
        parts = [f"扫描 {self.scanned} 个页面", f"自愈 {len(self.fixed)} 处"]
        if self.warnings:
            parts.append(f"待人工确认 {len(self.warnings)} 处")
        return "文档体检 | " + " | ".join(parts)


def _doc_pages(docs_dir: Path) -> list[Path]:
    pages = []
    for path in sorted(docs_dir.rglob("*.md")):
        rel_parts = path.relative_to(docs_dir).parts
        if any(part in _EXCLUDED_DIRS or part.startswith(".prev-") for part in rel_parts[:-1]):
            continue
        if path.name in _EXCLUDED_FILES or path.name.startswith("."):
            continue
        pages.append(path)
    return pages


def _normalize_bytes(raw: bytes) -> tuple[str, bool]:
    """Return decoded text and whether a byte-level repair was applied.

    UTF-8 BOM is stripped; genuinely invalid UTF-8 is decoded with replacement
    characters so the page opens instead of failing, and illegal control bytes
    are removed.
    """
    repaired = False
    data = raw
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
        repaired = True
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        repaired = True
    cleaned = _ILLEGAL_CONTROL.sub("", text)
    if cleaned != text:
        text, repaired = cleaned, True
    return text, repaired


def _fence_state(lines: list[str]) -> tuple[str, int] | None:
    """The currently-open fence (marker, line index) or None when balanced.

    Backtick and tilde fences are independent (a tilde fence may contain
    triple backticks), matching CommonMark behavior.
    """
    open_fence: tuple[str, int, int] | None = None
    for index, line in enumerate(lines):
        match = _FENCE_LINE.match(line)
        if not match:
            continue
        fence = match.group(2)
        marker = fence[0]
        if open_fence is None:
            open_fence = (marker, index, len(fence))
        elif marker == open_fence[0] and len(fence) >= open_fence[2]:
            open_fence = None
    return None if open_fence is None else (open_fence[0], open_fence[1])


def _repair_fences(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    open_fence = _fence_state(lines)
    if open_fence is None:
        return text, False
    marker, _index = open_fence
    closer = "```" if marker == "`" else "~~~"
    fixed = text.rstrip() + f"\n\n{closer}\n"
    return fixed, True


def _repair_backslash_links(text: str) -> tuple[str, bool]:
    out = []
    pos = 0
    changed = False
    for match in _BACKSLASH_LINK.finditer(text):
        target = match.group(2)
        if "\\" in target and "://" not in target:
            out.append(text[pos : match.start()])
            out.append(match.group(1) + target.replace("\\", "/") + match.group(3))
            pos = match.end()
            changed = True
    if not changed:
        return text, False
    out.append(text[pos:])
    return "".join(out), True


def _check_internal_links(path: Path, docs_dir: Path, text: str) -> list[str]:
    """Warnings for internal .md links that point at directories."""
    warnings = []
    for match in re.finditer(r"\[[^\]\n]*\]\(\s*<?([^)\s<>#]+?\.md)>?(?:#[^)\n]*)?\)", text):
        target = unquote(match.group(1)).strip()
        if "://" in target or target.startswith(("/", "mailto:")):
            continue
        resolved = (path.parent / target).resolve()
        try:
            resolved.relative_to(docs_dir.resolve())
        except ValueError:
            continue
        if resolved.is_dir():
            warnings.append(f"链接指向目录而非页面：{target}")
    return warnings


def _check_mermaid(text: str) -> list[str]:
    warnings = []
    if _EMPTY_MERMAID.search(text):
        warnings.append("存在空的 mermaid 代码块")
    return warnings


def doctor_wiki(
    docs_dir: str | Path,
    module_tree: dict[str, Any] | None = None,
) -> DoctorReport:
    """Validate every deliverable ``.md`` and repair what is safely fixable.

    Deterministic and side-effect-bounded: only byte normalization, closing an
    unbalanced code fence and rewriting Windows-style link paths are written.
    Missing targets (empty pages, unrepairable links) are reported, never
    fabricated. Never raises: one broken file must not abort a generation.
    """
    root = Path(docs_dir)
    report = DoctorReport()
    if not root.is_dir():
        return report

    for path in _doc_pages(root):
        rel = path.relative_to(root).as_posix()
        report.scanned += 1
        try:
            raw = path.read_bytes()
        except OSError as exc:
            report.warnings.append((rel, f"文件不可读：{type(exc).__name__}"))
            continue

        text, byte_repaired = _normalize_bytes(raw)
        if byte_repaired:
            report.fixed.append((rel, "编码/BOM/控制字符"))

        if not text.strip():
            report.warnings.append((rel, "页面为空或仅空白字符，需要重新生成该页"))
            if byte_repaired:
                _safe_write(path, text)
            continue

        text, fence_repaired = _repair_fences(text)
        if fence_repaired:
            report.fixed.append((rel, "补全未闭合的代码围栏"))

        text, link_path_repaired = _repair_backslash_links(text)
        if link_path_repaired:
            report.fixed.append((rel, "链接反斜杠路径归一化"))

        for issue in _check_mermaid(text):
            report.warnings.append((rel, issue))
        for issue in _check_internal_links(path, root, text):
            report.warnings.append((rel, issue))

        if byte_repaired or fence_repaired or link_path_repaired:
            _safe_write(path, text)

    # Dangling links after organize/fuzzy repair: reported, not guessed at.
    try:
        for page, target in find_dangling_links(str(root), module_tree):
            report.warnings.append((page, f"悬空链接（目标不存在）：{target}"))
    except Exception as exc:  # noqa: BLE001 — diagnostics must never fail a run
        logger.debug("悬空链接扫描失败：%s", exc)

    for rel, kind in report.fixed:
        logger.info("文档体检自愈：%s（%s）", rel, kind)
    for rel, issue in report.warnings:
        logger.warning("文档体检待处理：%s — %s", rel, issue)
    return report


def _safe_write(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8", newline="")
    except OSError as exc:  # pragma: no cover — surfaced through logging only
        logger.warning("文档体检写回失败 %s：%s", path, exc)
