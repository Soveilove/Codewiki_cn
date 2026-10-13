"""Where each module's page lives in the docs directory.

Two layouts are supported:

* ``hierarchical`` (default): pages mirror the module tree. A module's page
  sits next to the folder holding its children, so a page never moves when
  sub-modules are added to it mid-run::

      overview.md
      auth.md
      auth/login.md
      auth/session.md
      auth/session/store.md

* ``flat``: every page is ``{module_name}.md`` in the docs root (for small
  models that keep getting relative links wrong).

Module names stay unique across the whole tree in both layouts, so the
module-tree key is still the page's filename stem; only the folder differs.
Lookups are layout-agnostic (they try the nested path, then the root), so
docs written in either layout, or by an agent that saved a page in the wrong
place, still resolve. :func:`organize_docs` moves misplaced pages to where the
layout expects them and rewrites links between pages to correct relative
paths.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import posixpath
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from codewiki.src.config import (
    DEFAULT_LAYOUT,
    DEFAULT_OUTPUT_DIR,
    LAYOUT_FLAT,
    LAYOUT_HIERARCHICAL,
    MODULE_TREE_FILENAME,
    OVERVIEW_FILENAME,
)
from codewiki.src.prompt_assets import PROMPTS_FILENAME

logger = logging.getLogger(__name__)

OVERVIEW_STEM = OVERVIEW_FILENAME[: -len(".md")]
METADATA_FILENAME = "metadata.json"
# Working files (dependency graphs, reference index) live in the output's ``temp``
TEMP_DIR = "temp"
LAYOUTS = (LAYOUT_HIERARCHICAL, LAYOUT_FLAT)


def resolve_output_dir(repo_path: str | Path, output_dir: str | Path | None = None) -> Path:
    """Keep explicit paths; discover existing CodeWiki output before creating new docs."""
    if output_dir is not None:
        return Path(output_dir).expanduser().resolve()
    repo = Path(repo_path).expanduser().resolve()
    default = repo / DEFAULT_OUTPUT_DIR
    legacy = repo / "docs"
    for candidate in (default, legacy):
        if (candidate / MODULE_TREE_FILENAME).is_file():
            return candidate
    return default


def preserve_generated_docs(
    repo_path: str | Path,
    output_dir: str | Path,
    commit_id: str | None = None,
) -> Path:
    """Back up generated output while leaving repository-owned prompt assets in place."""
    import shutil

    repo, output = Path(repo_path).resolve(), Path(output_dir).resolve()
    shared = output == repo / DEFAULT_OUTPUT_DIR
    suffix = (commit_id or "unknown")[:8]
    root = output if shared else output.parent
    name = f".prev-{suffix}" if shared else f"{output.name}.prev-{suffix}"
    target = root / name
    index = 2
    while target.exists():
        target = root / f"{name}-{index}"
        index += 1
    if shared:
        generated = list(list_doc_files(str(output)).values()) + [
            "first_module_tree.json",
            "module_tree.json",
            "metadata.json",
            "update_record.json",
            "index.html",
            "temp",
        ]
        target.mkdir(parents=True)
        for relative in generated:
            source = output / relative
            if source.exists():
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
    else:
        shutil.move(str(output), str(target))
        output.mkdir(parents=True, exist_ok=True)
    return target


def normalize_layout(layout: str | None) -> str:
    return LAYOUT_FLAT if layout == LAYOUT_FLAT else LAYOUT_HIERARCHICAL


def config_layout(config: Any) -> str:
    """Layout of ``config`` (configs built without one use the default)."""
    return normalize_layout(getattr(config, "layout", None))


def module_doc_relpath(module_path: list[str], layout: str | None) -> str:
    """Docs-relative POSIX path of the page for the module at ``module_path``.

    ``module_path`` includes the module itself; ``[]`` is the repository
    overview.
    """
    if not module_path:
        return OVERVIEW_FILENAME
    if normalize_layout(layout) == LAYOUT_FLAT:
        return f"{module_path[-1]}.md"
    return posixpath.join(*module_path[:-1], f"{module_path[-1]}.md")


def module_doc_file(module_name: str, module_path: list[str] | None, layout: str | None) -> str:
    """Docs-relative file the agent documenting ``module_name`` must write.

    Like :func:`module_doc_relpath`, except that the whole-repository agent
    (``module_path == []``) writes ``{module_name}.md`` in the docs root,
    which is renamed to ``overview.md`` afterwards.
    """
    if not module_path:
        return f"{module_name}.md"
    return module_doc_relpath(module_path, layout)


def iter_tree_paths(module_tree: dict[str, Any] | None):
    """Yield the path (list of names) of every module in the tree."""
    stack: list[tuple[list[str], Any]] = [([], module_tree or {})]
    while stack:
        prefix, level = stack.pop()
        if not isinstance(level, dict):
            continue
        for name, info in level.items():
            path = prefix + [name]
            yield path
            if isinstance(info, dict) and isinstance(info.get("children"), dict):
                stack.append((path, info["children"]))


def doc_relpaths(module_tree: dict[str, Any] | None, layout: str | None) -> dict[str, str]:
    """Map every module name (and ``overview``) to its page's relative path."""
    paths = {OVERVIEW_STEM: OVERVIEW_FILENAME}
    for path in iter_tree_paths(module_tree):
        paths.setdefault(path[-1], module_doc_relpath(path, layout))
    return paths


def _load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_module_tree(docs_dir: str) -> dict[str, Any]:
    tree = _load_json(os.path.join(docs_dir, MODULE_TREE_FILENAME))
    return tree if isinstance(tree, dict) else {}


def read_layout(docs_dir: str) -> str:
    """Layout recorded in ``metadata.json``.

    Docs without a recorded layout predate hierarchical output, so they are
    flat.
    """
    metadata = _load_json(os.path.join(docs_dir, METADATA_FILENAME))
    info = metadata.get("generation_info") if isinstance(metadata, dict) else None
    layout = info.get("layout") if isinstance(info, dict) else None
    return layout if layout in LAYOUTS else LAYOUT_FLAT


def docs_layout(docs_dir: str) -> str:
    """Layout of existing docs (see :func:`read_layout`), or the default for new ones."""
    if os.path.exists(os.path.join(docs_dir, METADATA_FILENAME)):
        return read_layout(docs_dir)
    return DEFAULT_LAYOUT


def _candidate_relpaths(name: str, module_tree: dict[str, Any] | None) -> list[str]:
    """Places a page named ``name`` may be: its nested path, then the root."""
    if name == OVERVIEW_STEM:
        return [OVERVIEW_FILENAME]
    candidates = []
    for path in iter_tree_paths(module_tree):
        if path[-1] == name:
            candidates.append(module_doc_relpath(path, LAYOUT_HIERARCHICAL))
            break
    flat = f"{name}.md"
    if flat not in candidates:
        candidates.append(flat)
    return candidates


def find_doc(
    docs_dir: str,
    name: str,
    module_tree: dict[str, Any] | None = None,
    search: bool = False,
) -> str | None:
    """Absolute path of the existing page for ``name``, in either layout.

    ``search`` also walks the docs folders for ``{name}.md``, for a page whose
    module was already dropped from the tree (the updater removes those).
    """
    if module_tree is None:
        module_tree = load_module_tree(docs_dir)
    for rel in _candidate_relpaths(name, module_tree):
        path = os.path.normpath(os.path.join(docs_dir, rel))
        if os.path.isfile(path):
            return path
    if search and name != OVERVIEW_STEM:
        target = f"{name}.md"
        for root, dirs, files in os.walk(docs_dir):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != TEMP_DIR)
            if target in files:
                return os.path.join(root, target)
    return None


def list_doc_files(docs_dir: str, module_tree: dict[str, Any] | None = None) -> dict[str, str]:
    """Map page stem -> relative path for every page in the docs directory.

    Covers every ``.md`` in the docs root plus the nested page of each module
    in the tree. Other nested Markdown (e.g. a user's own ``docs/guides``)
    is deliberately not treated as a page.
    """
    pages: dict[str, str] = {}
    try:
        for entry in os.listdir(docs_dir):
            if entry.endswith(".md") and not entry.startswith(".") and entry != PROMPTS_FILENAME:
                if os.path.isfile(os.path.join(docs_dir, entry)):
                    pages[entry[: -len(".md")]] = entry
    except OSError:
        return {}
    if module_tree is None:
        module_tree = load_module_tree(docs_dir)
    for path in iter_tree_paths(module_tree):
        rel = module_doc_relpath(path, LAYOUT_HIERARCHICAL)
        if "/" in rel and os.path.isfile(os.path.join(docs_dir, rel)):
            pages[path[-1]] = rel
    return pages


def doc_path_map(docs_dir: str, module_tree: dict[str, Any] | None = None) -> dict[str, str]:
    """Page stem -> relative path for viewers: where each page is, else where it belongs."""
    if module_tree is None:
        module_tree = load_module_tree(docs_dir)
    paths = doc_relpaths(module_tree, read_layout(docs_dir))
    paths.update(list_doc_files(docs_dir, module_tree))
    return paths


def target_doc_path(
    docs_dir: str, name: str, layout: str | None, module_tree: dict[str, Any] | None = None
) -> str:
    """Absolute path where the page for ``name`` should be written."""
    if name == OVERVIEW_STEM:
        return os.path.join(docs_dir, OVERVIEW_FILENAME)
    if module_tree is None:
        module_tree = load_module_tree(docs_dir)
    for path in iter_tree_paths(module_tree):
        if path[-1] == name:
            return os.path.normpath(os.path.join(docs_dir, module_doc_relpath(path, layout)))
    return os.path.join(docs_dir, f"{name}.md")


def relative_link(from_rel: str, to_rel: str) -> str:
    """Relative link from page ``from_rel`` to page ``to_rel`` (both docs-relative)."""
    return posixpath.relpath(to_rel, posixpath.dirname(from_rel) or ".")


# [text](target.md#anchor "title") — target without scheme, spaces or '#'
_LINK_RE = re.compile(
    r"(\[[^\]\n]*\]\(\s*<?)([^)\s<>#]+\.md)((?:#[^)\s>]*)?>?(?:\s+\"[^\"]*\")?\s*\))"
)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

# Near-miss link repair. Agents occasionally write a page name that is one
# synonym/word-order/prefix away from the real one (observed: 退款审批 ->
# 退费审批, 任务管理_阶段事件联动 -> 阶段事件任务联动). Exact matching must stay
# primary; a fuzzy redirect needs a high score AND a clear unique winner.
FUZZY_LINK_MIN_RATIO = 0.72
FUZZY_LINK_MARGIN = 0.08


def _link_alias(stem: str) -> str:
    """Comparable form of a page/link stem for near-miss matching."""
    return re.sub(r"[\s_　]+", "", stem).lower()


def _stem_aliases(stem: str) -> set[str]:
    """Exact stem plus the de-prefixed delegation name (``父_子`` -> ``子``)."""
    aliases = {stem}
    if "_" in stem:
        aliases.add(stem.split("_", 1)[1])
    return aliases


def fuzzy_page_match(stem: str, relpaths: dict[str, str]) -> str | None:
    """Best unique known page for a near-miss ``stem``, or ``None`` when unsure."""
    if not stem:
        return None
    # Split the delegation prefix BEFORE underscore normalization, otherwise
    # ``任务管理_阶段事件联动`` could never shed its ``任务管理_`` head.
    wanted_aliases = {_link_alias(a) for a in _stem_aliases(stem)}
    scored: list[tuple[float, str]] = []
    for candidate in relpaths:
        forms = {_link_alias(a) for a in _stem_aliases(candidate)}
        ratio = max(
            (
                difflib.SequenceMatcher(None, form, other).ratio()
                for form in forms
                for other in wanted_aliases
            ),
            default=0.0,
        )
        if ratio >= FUZZY_LINK_MIN_RATIO:
            scored.append((ratio, candidate))
    if not scored:
        return None
    scored.sort(reverse=True)
    best_ratio, best = scored[0]
    if len(scored) > 1 and best_ratio - scored[1][0] < FUZZY_LINK_MARGIN:
        return None
    return relpaths[best]


def rewrite_page_links(text: str, page_rel: str, relpaths: dict[str, str]) -> str:
    """Point every link to a known page at its correct path relative to ``page_rel``.

    Exact stem matches win outright; an unmatched link gets one unique
    near-miss redirect. Anything ambiguous stays untouched.
    """
    lower = {k.lower(): v for k, v in relpaths.items()}

    def fix(m: re.Match) -> str:
        target = m.group(2)
        if "://" in target or target.startswith("/"):
            return m.group(0)
        stem = posixpath.basename(unquote(target))[: -len(".md")]
        dest = relpaths.get(stem) or lower.get(stem.lower())
        if dest is None:
            dest = fuzzy_page_match(stem, relpaths)
        if dest is None:
            return m.group(0)
        return f"{m.group(1)}{relative_link(page_rel, dest)}{m.group(3)}"

    out = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
        elif in_fence or ".md" not in line:
            out.append(line)
        else:
            out.append(_LINK_RE.sub(fix, line))
    return "".join(out)


def relocate_docs(docs_dir: str, module_tree: dict[str, Any], layout: str | None) -> list[str]:
    """Move pages found in the other layout's place to where ``layout`` expects them."""
    moved = []
    for path in iter_tree_paths(module_tree):
        target_rel = module_doc_relpath(path, layout)
        target = os.path.join(docs_dir, target_rel)
        if os.path.isfile(target):
            continue
        for rel in (
            module_doc_relpath(path, LAYOUT_HIERARCHICAL),
            module_doc_relpath(path, LAYOUT_FLAT),
        ):
            source = os.path.join(docs_dir, rel)
            if rel != target_rel and os.path.isfile(source):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(source, target)
                _remove_empty_parents(docs_dir, source)
                moved.append(target_rel)
                logger.info("Moved %s to %s", rel, target_rel)
                break
    return moved


def find_dangling_links(
    docs_dir: str, module_tree: dict[str, Any] | None = None
) -> list[tuple[str, str]]:
    """Internal ``.md`` links whose target file does not exist (page-relative).

    Only links that name a ``.md`` file are checked: anchors and external URLs
    are out of scope. Used to report links neither exact nor fuzzy repair could
    resolve instead of shipping a wiki with dead cross-references silently.
    """
    if module_tree is None:
        module_tree = load_module_tree(docs_dir)
    dangling: list[tuple[str, str]] = []
    for rel in sorted(set(list_doc_files(docs_dir, module_tree).values())):
        path = os.path.join(docs_dir, rel)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        in_fence = False
        for line in text.splitlines():
            if _FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            if in_fence or ".md" not in line:
                continue
            for match in _LINK_RE.finditer(line):
                target = match.group(2)
                if "://" in target or target.startswith("/"):
                    continue
                target_path = target.split("#", 1)[0].strip().strip("<>")
                if not target_path:
                    continue
                resolved = os.path.normpath(
                    os.path.join(os.path.dirname(path), unquote(target_path))
                )
                if not os.path.isfile(resolved):
                    dangling.append((rel, target_path))
    return dangling


def organize_docs(
    docs_dir: str, layout: str | None, module_tree: dict[str, Any] | None = None
) -> list[tuple[str, str]]:
    """Put every page where ``layout`` expects it and fix links between pages.

    Agents occasionally save a page at the docs root instead of its nested
    path, or compute a relative link wrongly; this repairs both. Returns the
    internal links that still point at missing pages after exact + fuzzy
    repair, so callers can warn rather than ship dead links silently.
    """
    if module_tree is None:
        module_tree = load_module_tree(docs_dir)
    relocate_docs(docs_dir, module_tree, layout)
    relpaths = doc_relpaths(module_tree, layout)
    for stem, rel in list_doc_files(docs_dir, module_tree).items():
        relpaths.setdefault(stem, rel)
    for rel in sorted(set(list_doc_files(docs_dir, module_tree).values())):
        path = os.path.join(docs_dir, rel)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        fixed = rewrite_page_links(text, rel, relpaths)
        if fixed != text:
            with open(path, "w", encoding="utf-8") as f:
                f.write(fixed)
    dangling = find_dangling_links(docs_dir, module_tree)
    for page, target in dangling:
        logger.warning("悬空文档链接（未找到匹配页面，未自动改写）：%s -> %s", page, target)
    return dangling


def remove_doc(docs_dir: str, path: str) -> None:
    """Delete a page and the folders its removal leaves empty (up to the docs root)."""
    os.remove(path)
    _remove_empty_parents(docs_dir, path)


def _remove_empty_parents(docs_dir: str, path: str) -> None:
    root = os.path.abspath(docs_dir)
    parent = os.path.dirname(os.path.abspath(path))
    while parent != root and parent.startswith(root + os.sep):
        try:
            os.rmdir(parent)
        except OSError:
            break
        parent = os.path.dirname(parent)
