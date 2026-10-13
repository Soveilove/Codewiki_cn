"""Utilities for keeping LLM-chosen module names unique and file-safe.

Every module doc is saved as ``{module_name}.md`` (in a folder mirroring the
module tree, or in the docs root with ``--flat``; see ``doc_layout``), and the
module-tree key must stay equal to the filename stem (the HTML viewer, link
repair and ``--update`` invalidation rely on it). Names are kept unique across
the whole tree in both layouts. Names are chosen freely by the LLM at every
hierarchy level, so collisions must be resolved before a name is inserted
into the tree (issue #76).
"""

import os
from dataclasses import dataclass, field
from typing import Any

import logging

from codewiki.src.be.doc_layout import (
    OVERVIEW_STEM,
    find_doc,
    list_doc_files,
    module_doc_file,
    module_doc_relpath,
    relative_link,
)

logger = logging.getLogger(__name__)

# Filename stems used by CodeWiki itself; never assign them to a module.
RESERVED_STEMS = {"overview", "module_tree", "first_module_tree", "metadata", "index", "prompts"}

# ``&`` is legal in filenames, but agents running shell tools escape it
# (``A_\\&_B/``) and write pages into a stray folder; nested layouts make
# that common, so module names never contain it.
_UNSAFE_FILENAME_CHARS = set('/\\:*?"<>|\0&')


def sanitize_module_name(name: str) -> str:
    """Make a module name safe to use as a filename stem (no case changes)."""
    cleaned = "".join("_" if c in _UNSAFE_FILENAME_CHARS else c for c in str(name).strip())
    cleaned = cleaned.replace(" ", "_").strip("._")
    return cleaned or "module"


def collect_module_tree_names(module_tree: dict[str, Any]) -> set[str]:
    """Collect all module names at every depth of the tree."""
    names = set()
    stack = [module_tree]
    while stack:
        level = stack.pop()
        for name, info in level.items():
            names.add(name)
            if isinstance(info, dict) and isinstance(info.get("children"), dict):
                stack.append(info["children"])
    return names


def strip_parent_prefix(name: str, parent_name: str | None) -> str:
    """Drop a leading ``Parent_`` delegation prefix if the request carries it.

    Agents sometimes return the already-prefixed name (e.g. parent ``课程管理``
    and request ``课程管理_固班排班``); planning must treat it as ``固班排班`` or
    the prefix gets doubled and cross-run reuse never matches.
    """
    if not parent_name:
        return name
    parent_san = sanitize_module_name(parent_name)
    head = parent_san + "_"
    if parent_san and name.startswith(head):
        return name[len(head) :]
    return name


def subtopic_name_variants(
    requested_name: str, parent_name: str | None, children_keys
) -> list[str]:
    """Existing child keys that could be the same sub-topic as ``requested_name``.

    Ordered canonical-first: the plain name, then the parent-prefixed name and
    any numeric variants. Covers the model returning either naming style across
    runs (observed: ``魔力课程与固班排班`` in one run,
    ``课程管理_魔力课程与固班排班`` in the next -> duplicate children).
    """
    name = sanitize_module_name(requested_name)
    base = strip_parent_prefix(name, parent_name)
    variants = [base]
    parent_san = sanitize_module_name(parent_name) if parent_name else ""
    if parent_san:
        prefixed = f"{parent_san}_{base}"
        variants.append(prefixed)
        variants += [key for key in children_keys if key.startswith(prefixed + "_")]
    seen = set()
    ordered = []
    for variant in variants:
        if variant not in seen:
            seen.add(variant)
            ordered.append(variant)
    return ordered


def resolve_unique_name(name: str, parent_name: str | None, taken: set[str]) -> str:
    """Return ``name`` if free, else prefix with parent, else add a numeric suffix."""
    occupied = {os.path.normcase(item) for item in taken}
    if os.path.normcase(name) not in occupied:
        return name
    if parent_name:
        prefixed = f"{sanitize_module_name(parent_name)}_{name}"
        if os.path.normcase(prefixed) not in occupied:
            return prefixed
    else:
        prefixed = name
    n = 2
    while os.path.normcase(f"{prefixed}_{n}") in occupied:
        n += 1
    return f"{prefixed}_{n}"


def _existing_doc_stems(working_dir: str) -> set[str]:
    return set(list_doc_files(working_dir))


def normalize_sub_module_specs(
    sub_module_specs: dict[str, Any],
    parent_name: str | None,
    module_tree: dict[str, Any],
    working_dir: str,
) -> dict[str, str]:
    """Map requested sub-module names to unique, file-safe final names.

    A name is taken if it already appears anywhere in the module tree, if a
    page with that stem exists in the docs dir, if it is reserved, or
    if it was assigned earlier in this batch.
    """
    taken = collect_module_tree_names(module_tree)
    taken |= _existing_doc_stems(working_dir)
    taken |= RESERVED_STEMS

    name_map: dict[str, str] = {}
    for requested_name in sub_module_specs:
        final_name = resolve_unique_name(sanitize_module_name(requested_name), parent_name, taken)
        taken.add(final_name)
        name_map[requested_name] = final_name
        if final_name != requested_name:
            logger.info(
                "Sub-module name '%s' collides with an existing module or file; renamed to '%s'.",
                requested_name,
                final_name,
            )
    return name_map


@dataclass
class SubModulePlan:
    """What ``generate_sub_module_documentation`` will actually create.

    ``name_map``: requested name -> final, unique file stem to generate.
    ``skipped``: requested name -> reason it is not generated (already
    documented under that name or its parent-prefixed variant).
    """

    name_map: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)


def plan_sub_module_specs(
    sub_module_specs: dict[str, Any],
    parent_name: str | None,
    module_tree: dict[str, Any],
    working_dir: str,
) -> SubModulePlan:
    """Decide which requested sub-modules to generate (issue #113).

    Like :func:`normalize_sub_module_specs`, a name that collides with the
    tree, a ``.md`` on disk or a reserved stem gets the parent prefix. Unlike
    it, a request whose plain *and* prefixed names are both taken is treated
    as a repeat of something already documented and skipped, never renamed
    with a numeric suffix: that suffixing is what let one agent regenerate the
    same modules as ``x_2``, ``x_3``, ... without ever converging.
    """
    taken = collect_module_tree_names(module_tree)
    taken |= _existing_doc_stems(working_dir)
    taken |= RESERVED_STEMS

    plan = SubModulePlan()
    parent_san = sanitize_module_name(parent_name) if parent_name else None
    for requested_name in sub_module_specs:
        # Normalize away a parent prefix the model may already have added,
        # otherwise an unprefixed child from an earlier run is not recognized
        # and a second, prefixed copy is created.
        raw = sanitize_module_name(requested_name)
        carried_prefix = bool(parent_san and raw.startswith(parent_san + "_"))
        name = strip_parent_prefix(raw, parent_name)
        prefixed = f"{parent_san}_{name}" if parent_san else name
        if name not in taken:
            final_name = name
        elif prefixed in taken:
            existing = name if name in taken else prefixed
            reason = f"already documented as {existing}.md; do not request it again"
            plan.skipped[requested_name] = reason
            logger.info(
                "Sub-module '%s' already documented as '%s'; skipping duplicate request.",
                requested_name,
                existing,
            )
            continue
        elif carried_prefix:
            # The request already carries the parent prefix and the plain
            # child exists: same topic documented in the earlier naming style.
            reason = f"already documented as {name}.md; do not request it again"
            plan.skipped[requested_name] = reason
            continue
        else:
            final_name = prefixed
            logger.info(
                "Sub-module name '%s' collides with an existing module or file; renamed to '%s'.",
                requested_name,
                final_name,
            )
        taken.add(final_name)
        plan.name_map[requested_name] = final_name
    return plan


def sub_module_report(
    name_map: dict[str, str],
    skipped: dict[str, str],
    docs_dir: str,
    module_tree: dict,
    parent_name: str,
    parent_path: list[str],
    layout: str,
) -> str:
    """Report what actually landed on disk so the parent agent links real files.

    Each saved page is given as the link to use from the parent's page.
    """
    parent_doc = module_doc_file(parent_name, parent_path, layout)
    saved = []
    missing = []
    for requested_name, final_name in name_map.items():
        entry = relative_link(parent_doc, module_doc_relpath(parent_path + [final_name], layout))
        if final_name != requested_name:
            entry += f" (requested '{requested_name}', renamed to avoid a collision)"
        if find_doc(docs_dir, final_name, module_tree) is not None:
            saved.append(entry)
        else:
            missing.append(entry)

    report = f"Saved documentations (link them from `{parent_doc}` as): "
    report += f"{', '.join(saved) if saved else 'none'}."
    if missing:
        report += f" MISSING (generation did not produce these files): {', '.join(missing)}."
        logger.warning("Sub-module documentation missing after generation: %s", ", ".join(missing))
    if skipped:
        report += " " + skipped_report(skipped, parent_doc)
    return report


def skipped_report(skipped: dict[str, str], parent_doc: str) -> str:
    """Tell the parent agent, unambiguously, not to retry skipped sub-modules."""
    if not skipped:
        return "No sub-modules were generated."
    items = ", ".join(f"'{name}' ({reason})" for name, reason in skipped.items())
    return (
        f"Skipped sub-modules: {items}. Do NOT call generate_sub_module_documentation again "
        f"for these; link the existing pages from `{parent_doc}` instead."
    )


def dedupe_module_tree_names(module_tree: dict[str, Any]) -> dict[str, Any]:
    """Sanitize and uniquify all module names in a freshly clustered tree.

    Must only run on trees whose docs have not been generated yet — renaming
    a key whose ``.md`` already exists would orphan the doc.
    """
    taken: set[str] = set(RESERVED_STEMS)

    def dedupe_level(level: dict[str, Any], parent_name: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, info in level.items():
            final_name = resolve_unique_name(sanitize_module_name(name), parent_name, taken)
            taken.add(final_name)
            if final_name != name:
                logger.info("Module name '%s' collides; renamed to '%s'.", name, final_name)
            if isinstance(info, dict) and isinstance(info.get("children"), dict):
                info = {**info, "children": dedupe_level(info["children"], final_name)}
            result[final_name] = info
        return result

    return dedupe_level(module_tree, None)


def resolve_module_doc_path(
    working_dir: str, module_name: str, module_tree: dict[str, Any] | None = None
) -> str | None:
    """Resolve the on-disk path for a module's .md doc, in either layout.

    Sub-agents sometimes save files under a sanitized variant of the module
    name (spaces → underscores, lowercased, etc.) rather than the exact key
    in the module tree. Try a small set of common variants before giving up.
    """
    found = find_doc(working_dir, module_name, module_tree)
    if found is not None:
        return found
    candidates = []
    seen = set()
    base_variants = [
        module_name,
        module_name.replace(" ", "_"),
        module_name.replace(" ", "-"),
        module_name.replace(" ", ""),
    ]
    for variant in base_variants:
        for cased in (variant, variant.lower()):
            if cased not in seen:
                seen.add(cased)
                candidates.append(f"{cased}.md")

    for filename in candidates:
        candidate_path = os.path.join(working_dir, filename)
        if os.path.exists(candidate_path):
            return candidate_path
    return None


def find_missing_module_docs(
    module_tree: dict[str, Any],
    working_dir: str,
    overview_required: bool = True,
) -> list[str]:
    """Return module names from the tree whose docs are missing on disk."""
    missing = []
    for name in sorted(collect_module_tree_names(module_tree)):
        if resolve_module_doc_path(working_dir, name, module_tree) is None:
            missing.append(name)
    if overview_required and not os.path.exists(os.path.join(working_dir, "overview.md")):
        missing.append("overview")
    return missing


def _first_h1(path: str) -> str | None:
    in_fence = False
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(("```", "~~~")):
                    in_fence = not in_fence
                elif not in_fence and stripped.startswith("# "):
                    return stripped[2:].strip().strip("#").strip() or None
    except (OSError, UnicodeDecodeError):
        return None
    return None


def extract_page_titles(working_dir: str, module_tree: dict[str, Any]) -> dict[str, str]:
    """Map module names (and "overview") to the first `# ` heading of their page.

    Used as viewer display titles for docs written in another language, so the
    navigation can be translated while filenames stay equal to module names.
    Pages that are missing or have no H1 are left out.
    """
    titles = {}
    for name in [OVERVIEW_STEM, *sorted(collect_module_tree_names(module_tree))]:
        path = resolve_module_doc_path(working_dir, name, module_tree)
        title = _first_h1(path) if path and os.path.exists(path) else None
        if title:
            titles[name] = title
    return titles
