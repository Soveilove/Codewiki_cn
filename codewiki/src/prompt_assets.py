"""Team prompt assets stored next to the repository.

A repository may hold ``<repo>/.codewiki/prompts.md``. It is a team asset: it
travels with the repository and is committed beside the code it documents.

The file has two parts: a YAML front matter carrying the structured instruction
fields, and a Markdown body that becomes the free-form prompt. Whatever the file
sets **replaces** the same prompt field coming from the persistent CLI
configuration, because the repository is the more specific source.

This file never replaces the built-in system prompt. Business-knowledge rules,
evidence discipline and unique primary ownership are ours and stay in place; a
team file only adds its own conventions on top.

:func:`load_prompt_asset` returns a :class:`PromptAsset` that carries both the
instructions and a plain-language diagnosis, so the CLI can always show whether
the team file took effect and the MCP server can surface the same summary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

PROMPTS_DIRNAME = ".codewiki"
PROMPTS_FILENAME = "prompts.md"

# Front-matter keys that map onto agent instruction fields. Anything else is
# ignored rather than silently merged, so a typo cannot change generation
# behaviour in an unexpected way.
STRUCTURED_KEYS = (
    "language",
    "doc_type",
    "focus_modules",
    "include_patterns",
    "exclude_patterns",
    "artifact_exclude",
)

# load_prompt_asset statuses, from best to worst:
#   absent  — no file in this repository (nothing to report)
#   loaded  — every setting in the file took effect
#   partial — some settings took effect; others were dropped (unrecognised
#             keys, or a front matter that failed to parse)
#   invalid — the file exists but nothing in it could be used
STATUS_ABSENT = "absent"
STATUS_LOADED = "loaded"
STATUS_PARTIAL = "partial"
STATUS_INVALID = "invalid"


@dataclass
class PromptAsset:
    """Outcome of reading ``<repo>/.codewiki/prompts.md``.

    ``instructions`` is ``None`` when the file is absent or unusable; ``status``
    says which. ``ignored`` lists front-matter keys that were dropped, and
    ``reason`` explains a partial or invalid load (with a line number for YAML
    errors whenever PyYAML provides one).
    """

    instructions: dict[str, Any] | None
    status: str
    applied: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    reason: str | None = None
    path: str | None = None
    body_chars: int = 0
    sha256: str | None = None


def prompts_path(repo_path: str) -> str:
    return os.path.join(repo_path, PROMPTS_DIRNAME, PROMPTS_FILENAME)


def _split_front_matter(text: str) -> tuple[str, str]:
    """Return ``(front_matter, body)``; front matter is empty when absent."""
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        return "", stripped
    lines = stripped.splitlines()
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    # Unterminated front matter: keep the whole file as the prompt body rather
    # than dropping the user's text.
    return "", stripped


def _parse_front_matter(front_matter: str) -> tuple[dict[str, Any], str | None]:
    """Return ``(data, error)``; ``error`` explains why parsing failed."""
    if not front_matter.strip():
        return {}, None
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a hard dependency
        logger.warning("PyYAML unavailable; ignoring prompts.md front matter")
        return {}, "PyYAML 不可用，front matter 未解析"
    try:
        data = yaml.safe_load(front_matter)
    except Exception as exc:  # noqa: BLE001 - a bad team file must not abort a run
        mark = getattr(exc, "problem_mark", None)
        # problem_mark.line is 0-based inside the front matter; the opening
        # ``---`` is file line 1, so the file line is mark.line + 2.
        where = f"第 {mark.line + 2} 行" if mark is not None else "格式错误"
        logger.debug("Could not parse prompts.md front matter: %s", exc)
        return {}, f"YAML 解析失败（{where}），结构化字段未生效"
    return (data if isinstance(data, dict) else {}), None


def load_prompt_asset(repo_path: str | None) -> PromptAsset:
    """Read ``<repo>/.codewiki/prompts.md`` into agent-instruction fields.

    The instructions are ``None`` when the repository has no prompt file or the
    file is unusable, so callers fall back to the persistent configuration
    unchanged. The accompanying diagnosis lets the CLI report the outcome in
    plain language instead of hiding it in logs.
    """
    if not repo_path:
        return PromptAsset(None, STATUS_ABSENT)
    path = prompts_path(repo_path)
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return PromptAsset(None, STATUS_ABSENT)
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return PromptAsset(None, STATUS_INVALID, reason=f"无法读取文件：{exc}", path=path)
    fingerprint = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return PromptAsset(
            None, STATUS_INVALID, reason=f"不是有效的 UTF-8：{exc}", path=path, sha256=fingerprint
        )

    front_matter, body = _split_front_matter(text)
    data, parse_error = _parse_front_matter(front_matter)
    unknown = sorted(set(data) - set(STRUCTURED_KEYS))
    if unknown:
        # Carried on the PromptAsset: the CLI prints a plain-language warning
        # and MCP surfaces the same diagnosis. Keep the log at debug so the
        # friendly message is not duplicated on stderr.
        logger.debug("%s: ignoring unsupported front-matter keys: %s", path, ", ".join(unknown))

    instructions: dict[str, Any] = {}
    for key in STRUCTURED_KEYS:
        value = data.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            if not items:
                continue
            instructions[key] = items
            continue
        instructions[key] = value

    prompt_text = body.strip()
    if prompt_text:
        instructions["custom_instructions"] = prompt_text
    if not instructions:
        logger.debug("%s has no usable settings; ignoring it", path)
        return PromptAsset(
            None,
            STATUS_INVALID,
            ignored=unknown,
            reason=parse_error or "没有可用的设置",
            path=path,
            sha256=fingerprint,
        )
    logger.info("Loaded team prompt asset: %s", path)
    status = STATUS_PARTIAL if (unknown or parse_error) else STATUS_LOADED
    return PromptAsset(
        instructions,
        status,
        applied=sorted(instructions),
        ignored=unknown,
        reason=parse_error,
        path=path,
        body_chars=len(prompt_text),
        sha256=fingerprint,
    )


def merge_prompt_asset(
    base: dict[str, Any] | None, asset: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Let the repository prompt file win over weaker prompt sources.

    Only prompt fields merge here: run parameters such as concurrency stay in
    the persistent configuration, because they describe the machine rather than
    the documentation the team wants.
    """
    if not asset:
        return base
    merged = dict(base or {})
    merged.update(asset)
    return merged


def resolve_prompt_instructions(
    repo_path: str,
    base: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, PromptAsset]:
    """Resolve team conventions identically for CLI and MCP entry points."""
    asset = load_prompt_asset(repo_path)
    return merge_prompt_asset(base, asset.instructions), asset


def prompt_asset_summary(asset: PromptAsset) -> dict[str, Any]:
    """Small diagnosis suitable for MCP responses without embedding the prompt body."""
    return {
        "status": asset.status,
        "path": asset.path,
        "applied": asset.applied,
        "ignored": asset.ignored,
        "reason": asset.reason,
        "body_chars": asset.body_chars,
    }


# --------------------------------------------------------------------------- #
# Change detection: did the team conventions move since the last build?
# --------------------------------------------------------------------------- #

# detect_prompt_change statuses:
#   no_baseline — the previous build predates fingerprinting; record the
#                 current fingerprint silently, nothing to warn about
#   unchanged   — same bytes as the fingerprinted build
#   changed     — different bytes (or the file appeared / disappeared)
STATUS_NO_BASELINE = "no_baseline"
STATUS_UNCHANGED = "unchanged"
STATUS_CHANGED = "changed"

# Key under metadata.json's generation_info.
FINGERPRINT_KEY = "prompt_asset_sha256"


@dataclass
class PromptAssetChange:
    """Outcome of comparing the team prompt file with the stored fingerprint."""

    status: str
    stored: str | None
    current: str | None


def prompt_asset_fingerprint(repo_path: str | None) -> str | None:
    """SHA256 of the team prompt file's raw bytes; ``None`` when it is absent.

    Any byte-level difference (whitespace included) changes the fingerprint:
    a false positive costs one keypress, a false negative ships a wiki whose
    pages silently mix old and new conventions.
    """
    if not repo_path:
        return None
    try:
        with open(prompts_path(repo_path), "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def read_stored_prompt_fingerprint(output_dir: str) -> tuple[bool, str | None]:
    """Return baseline presence and value; null records a known absent file."""
    try:
        with open(os.path.join(output_dir, "metadata.json"), encoding="utf-8") as handle:
            info = (json.load(handle) or {}).get("generation_info") or {}
        return FINGERPRINT_KEY in info, info.get(FINGERPRINT_KEY)
    except (OSError, json.JSONDecodeError, AttributeError):
        return False, None


def detect_prompt_change(repo_path: str, output_dir: str) -> PromptAssetChange:
    """Compare the team prompt file against the fingerprint in metadata.json."""
    has_baseline, stored = read_stored_prompt_fingerprint(output_dir)
    current = prompt_asset_fingerprint(repo_path)
    if not has_baseline:
        return PromptAssetChange(STATUS_NO_BASELINE, stored, current)
    if stored == current:
        return PromptAssetChange(STATUS_UNCHANGED, stored, current)
    return PromptAssetChange(STATUS_CHANGED, stored, current)


def record_prompt_baseline(output_dir: str, change: PromptAssetChange) -> None:
    """Baseline old metadata without changing generation info or update history."""
    if change.status != STATUS_NO_BASELINE:
        return
    path = os.path.join(output_dir, "metadata.json")
    try:
        with open(path, encoding="utf-8") as handle:
            metadata = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(metadata, dict):
        return
    info = metadata.get("generation_info") or {}
    if not isinstance(info, dict):
        return
    if FINGERPRINT_KEY not in info:
        info[FINGERPRINT_KEY] = change.current
        metadata["generation_info"] = info
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, indent=4, ensure_ascii=False)
