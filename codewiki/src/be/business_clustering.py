"""Business-topic planning for initialization; update keeps its existing planner."""

from __future__ import annotations

import heapq
import json
import logging
import math
import re
from collections import defaultdict
from typing import Any

from codewiki.src.be.cluster_modules import (
    _parse_cluster_response,
    resolve_component_ids,
)
from codewiki.src.be.utils import count_tokens
from codewiki.src.config import DEFAULT_MAX_PLANNING_REFS
from codewiki.src.language import format_module_naming_directive

logger = logging.getLogger(__name__)


class BusinessClusteringError(RuntimeError):
    """Planning could not establish complete, evidence-based topic ownership."""


# Preferred representative within ONE file, applied only as a tie-break:
# whatever type a language's parser produces can become an entry, so
# class-first (Java/C#/TS) and function-first (Go/Rust/C/Python) repos get
# entries matching their own structure instead of one hardcoded paradigm.
STRUCTURAL_TYPES = ("class", "interface", "struct")


def _file_primary_entry(file_ids, components):
    ordered = sorted(file_ids)
    structural = [cid for cid in ordered if components[cid].component_type in STRUCTURAL_TYPES]
    if structural:
        return structural[0]
    artifacts = [cid for cid in ordered if components[cid].component_type == "artifact"]
    if artifacts:
        return artifacts[0]
    return max(ordered, key=lambda cid: len(components[cid].depends_on))


def select_business_components(leaf_nodes, components):
    """Language-agnostic analysis entries for init.

    Keeps the parser's own leaf entry points and adds one primary entry per
    parsed source file that has none, so no file silently loses coverage and
    no component type is excluded by a single-language whitelist.
    """
    selected = set(resolve_component_ids(leaf_nodes, components, strict=True))
    by_file = defaultdict(list)
    for cid, node in components.items():
        by_file[node.relative_path.replace("\\", "/")].append(cid)
    for ids in by_file.values():
        if not any(cid in selected for cid in ids):
            selected.add(_file_primary_entry(ids, components))
    return sorted(selected, key=lambda cid: (components[cid].relative_path.replace("\\", "/"), cid))


def _entry_anchors(cid, by_file, components):
    node = components[cid]
    candidates = by_file.get(node.relative_path.replace("\\", "/"), [])
    if cid in candidates:
        return [cid]
    declared = [
        key
        for key in candidates
        if node.class_name
        and components[key].name == node.class_name
        and components[key].start_line > 0
        and components[key].start_line <= node.start_line
        and components[key].end_line >= node.end_line
    ]
    if len(declared) == 1:
        return declared
    containers = [
        key
        for key in candidates
        if components[key].start_line > 0
        and components[key].start_line <= node.start_line
        and components[key].end_line >= node.end_line
    ]
    if containers:
        width = min(components[key].end_line - components[key].start_line for key in containers)
        return [
            key
            for key in containers
            if components[key].end_line - components[key].start_line == width
        ]
    if len(candidates) == 1 and (node.start_line <= 0 or components[candidates[0]].start_line <= 0):
        return candidates
    return []


def dependency_neighborhoods(component_ids, components):
    """Project edges onto their nearest declaration, not every type in a file."""
    by_file = defaultdict(list)
    for cid in component_ids:
        by_file[components[cid].relative_path.replace("\\", "/")].append(cid)
    anchors = {cid: _entry_anchors(cid, by_file, components) for cid in components}
    outgoing = {cid: set() for cid in component_ids}
    incoming = {cid: set() for cid in component_ids}
    for cid, node in components.items():
        for dep in node.depends_on:
            for owner in anchors[cid]:
                for target in anchors.get(dep, []):
                    if owner != target:
                        outgoing[owner].add(target)
                        incoming[target].add(owner)
    return outgoing, incoming


def _source_excerpt(node):
    """Syntax-neutral excerpt: never assumes keywords of any one language."""
    source = node.source_code or ""
    if len(source) <= 1600:
        return source
    return source[:1400] + "\n[excerpt truncated; read full implementation when documenting]"


def _usage_excerpt(node, target):
    source = node.source_code or ""
    if len(source) <= 1600:
        return source
    lines = source.splitlines()
    symbols = set(re.findall(r"[A-Za-z_]\w*", target.name))
    matched = [
        i for i, line in enumerate(lines) if symbols.intersection(re.findall(r"[A-Za-z_]\w*", line))
    ]
    if not matched:
        return _source_excerpt(node)
    selected = sorted(
        {i for match in matched for i in range(max(0, match - 2), min(len(lines), match + 3))}
    )
    return "\n".join(f"{node.start_line + i}: {lines[i]}" for i in selected)[:1600]


def planning_evidence(component_ids, components, outgoing, incoming, relations=None):
    relations = (
        relations if relations is not None else _ownership_relations(component_ids, components)
    )
    rows = {}
    for cid in component_ids:
        node = components[cid]
        dependencies = sorted(outgoing[cid])
        callers = sorted(incoming[cid])
        rows[cid] = {
            "id": cid.replace("\\", "/"),
            "kind": node.component_type,
            "doc": (node.docstring or "")[:600],
            "source_excerpt": _source_excerpt(node),
            "depends_on": [key.replace("\\", "/") for key in dependencies[:12]],
            "used_by": [key.replace("\\", "/") for key in callers[:12]],
            "dependency_count": len(dependencies),
            "caller_count": len(callers),
        }
        relation = relations.get(cid, {})
        if relation.get("parent"):
            rows[cid]["declared_in"] = relation["parent"].replace("\\", "/")
        rows[cid]["type_used_by"] = sorted(relation.get("users", set()))[:12]
        rows[cid]["type_user_count"] = len(relation.get("users", set()))
        rows[cid]["usage_evidence"] = relation.get("usage", [])[:2]
    return rows


def partition_by_relationships(component_ids, components, outgoing, incoming, fits):
    """Greedy bounded neighborhoods; normalize hub influence, never drop nodes."""
    ordered = sorted(
        component_ids, key=lambda cid: (components[cid].relative_path.replace("\\", "/"), cid)
    )
    remaining = set(ordered)
    degree = {cid: len(outgoing[cid] | incoming[cid]) for cid in ordered}
    batches = []
    cursor = 0
    while remaining:
        while ordered[cursor] not in remaining:
            cursor += 1
        seed = ordered[cursor]
        batch = []
        scores = defaultdict(float)
        queue = [(0.0, seed)]
        while queue:
            negative, cid = heapq.heappop(queue)
            if cid not in remaining or (cid != seed and -negative != scores[cid]):
                continue
            if not fits(batch + [cid]):
                if not batch:
                    raise BusinessClusteringError(
                        f"Planning evidence exceeds input budget for {cid}"
                    )
                continue
            batch.append(cid)
            remaining.remove(cid)
            for neighbor in sorted((outgoing[cid] | incoming[cid]) & remaining):
                scores[neighbor] += 1.0 / math.sqrt(max(1, degree[cid]) * max(1, degree[neighbor]))
                heapq.heappush(queue, (-scores[neighbor], neighbor))
        if batches and fits(batches[-1] + batch):
            batches[-1].extend(batch)
        else:
            batches.append(batch)
    return batches


BUSINESS_PLANNING_RULES = """Plan a business-knowledge wiki from source evidence, not a directory index.
Group by business capabilities, workflows and rules. Technical-layer names such as dto,
req or rsp alone do not establish a business topic. Keep data models and converters with
what they support. Build/deployment and genuinely shared technical capabilities may have
explicit topics. Static graph links are clues, not proof; missing edges do not imply unrelated
businesses. Excerpts are for planning, not completed implementation analysis.
The batch is an analysis budget boundary, NOT a page boundary. Reuse an existing topic's
EXACT name whenever its purpose matches; do not invent synonymous or numbered supplement
pages. Create a new topic only for a distinct responsibility supported by the evidence.
Each supplied C reference needs exactly ONE primary topic. This does not restrict reading,
explaining or referencing shared source from other topics. Never omit an ambiguous item:
use its source and relationships to decide. A reference with no business behaviour of its
own — a request/response DTO, a message payload, an inner class, a converter — is not an
exception: place it with the topic whose components use it, because its callers and the
types it carries are the evidence for that topic. Never create a topic that describes
this analysis instead of the business ("evidence insufficient", "unclassified", "to be
confirmed"): every reference belongs to a topic about the business. Return ONLY
<GROUPED_COMPONENTS> containing
compact JSON </GROUPED_COMPONENTS>, with this shape:
{"business topic": {"purpose": "evidence-based responsibility", "components": ["C0"]}}.
Use only C references from INPUT_EVIDENCE in components. No ranges, unknown references,
directory coverage placeholders or prose outside the envelope.
"""


def _catalog(tree):
    return [{"topic": name, "purpose": info["purpose"][:300]} for name, info in tree.items()]


def _prompt(ids, rows, tree, language, *, consolidation=False, refs=None):
    labels = refs or {}
    inputs = [{"ref": labels.get(cid, f"C{i}"), **rows[cid]} for i, cid in enumerate(ids)]
    instruction = (
        "Reconcile candidate topics across analysis batches. Each C reference is an entire candidate "
        "topic, not a source component. Merge synonyms and technical fragments of the same business "
        "capability, but preserve genuinely distinct responsibilities. Assign every C exactly once.\n"
        if consolidation
        else ""
    )
    return (
        format_module_naming_directive(language)
        + "\n"
        + BUSINESS_PLANNING_RULES
        + instruction
        + "\n<EXISTING_TOPICS>\n"
        + json.dumps(_catalog(tree), ensure_ascii=False)
        + "\n</EXISTING_TOPICS>\n<INPUT_EVIDENCE>\n"
        + json.dumps(inputs, ensure_ascii=False)
        + "\n</INPUT_EVIDENCE>"
    )


def salvage_partial_json(content):
    """Recover complete topic objects from a truncated planning response.

    Real planning responses look like ``{"topic": {"purpose": ..., "components":
    [...]}, "topic2": {...}}`` — a single top-level object. When the model stops
    mid-value, ``json.loads`` fails but the complete ``"topic": {...}`` pairs
    before the cut are still usable, so each one is decoded on its own.
    """
    if not isinstance(content, str) or not content.strip():
        return None
    decoder = json.JSONDecoder()
    recovered = {}
    # Pass 1: the whole thing parses (caller only reaches here on failure, so
    # this mostly helps when a trailing fence breaks json.loads).
    try:
        whole, _ = decoder.raw_decode(content, content.index("{"))
        if isinstance(whole, dict):
            return whole
    except ValueError:
        pass
    # Pass 2: decode each "name": {...} pair independently.
    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"\s*:\s*\{', content):
        try:
            obj, _ = decoder.raw_decode(content, match.end() - 1)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj:
            recovered[match.group(1)] = obj
    return recovered or None


def _parse_envelope(response):
    """Parse the planning envelope, salvaging complete topics when truncated."""
    try:
        return _parse_cluster_response(response)
    except (ValueError, SyntaxError, TypeError, RecursionError):
        match = (
            re.search(r"<GROUPED_COMPONENTS>\s*(.*?)\s*</GROUPED_COMPONENTS>", response, re.S)
            if isinstance(response, str)
            else None
        )
        body = match.group(1) if match else (response if isinstance(response, str) else "")
        salvaged = salvage_partial_json(body)
        if salvaged:
            return salvaged
        raise


def _read_assignments(response, ids, catalog=None, refs=None):
    """Parse one planning response, salvaging what is usable.

    Returns ``(assignments, problems)``. Only a response whose JSON shape is
    unusable raises. A topic entry is dropped — not the whole response — when it
    is a directory placeholder, a meta-topic about the analysis itself, or when it
    is a NEW topic without a purpose.
    ``purpose`` may be omitted for a topic that already exists in the catalog,
    which is how a model naturally extends a known topic.
    """
    result = _parse_envelope(response)
    labels = refs or {}
    known_refs = {labels.get(cid, f"C{i}"): cid for i, cid in enumerate(ids)}
    known = set(catalog or ())
    seen = set()
    assignments = {}
    problems = []
    for name, info in result.items():
        if not isinstance(info, dict):
            problems.append(f"topic {name}: value is not an object")
            continue
        if re.search(r"_(?:coverage|补充分析)(?:_\d+)?$", name):
            problems.append(f"directory coverage placeholder rejected: {name}")
            continue
        if _is_meta_topic(name):
            problems.append(f"analysis-about-analysis topic rejected: {name}")
            continue
        purpose = info.get("purpose")
        if purpose is not None and (not isinstance(purpose, str) or not purpose.strip()):
            problems.append(f"topic {name}: purpose must be a non-empty string")
            continue
        if isinstance(purpose, str):
            purpose = purpose.strip()
        if purpose is None and name not in known:
            problems.append(f"new topic {name} needs an evidence-based purpose")
            continue
        resolved = []
        for ref in info.get("components") or []:
            if ref not in known_refs:
                problems.append(f"topic {name}: unknown reference {ref}")
                continue
            if ref in seen:
                problems.append(f"topic {name}: duplicate reference {ref}")
                continue
            seen.add(ref)
            resolved.append(known_refs[ref])
        if resolved:
            assignments[name] = {"purpose": purpose, "components": resolved}
    return assignments, problems


def _topic_key(name: str) -> str:
    """Whitespace-insensitive key for topic identity.

    Models sometimes split one responsibility into two near-identical topics
    that differ only in spacing ("跨业务线接口转发" vs "跨业务线接口 转发").
    Sanitizing turns the space into an underscore and produces two overlapping
    pages, so identity is compared without whitespace and the second name is
    merged into the first.
    """
    return re.sub(r"\s+", "", name)


def _is_meta_topic(name: str) -> bool:
    """Reject standalone planning refusals, not business names containing state words."""
    text = re.sub(r"[\s,，、:：_\-。.]", "", name).lower()
    return bool(
        re.fullmatch(
            r"(?:(?:证据不足|无法归类|无法分类|未归类|未分类|待归类)(?:组件|条目|分析)?)+"
            r"|(?:insufficientevidence|unclassified|uncategorized)(?:components|items)?",
            text,
        )
    )


def canonical_topic_name(tree: dict[str, Any], name: str) -> str:
    """Return the existing topic whose whitespace-free key equals ``name``'s."""
    key = _topic_key(name)
    for existing in tree:
        if existing != name and _topic_key(existing) == key:
            return existing
    return name


def _ownership_relations(ids, components) -> dict[str, dict[str, Any]]:
    by_file = defaultdict(list)
    for cid in ids:
        by_file[components[cid].relative_path.replace("\\", "/")].append(cid)
    anchors = {cid: _entry_anchors(cid, by_file, components) for cid in components}
    parents = {}
    users = {cid: set() for cid in ids}
    usage = {cid: [] for cid in ids}
    for cid in ids:
        node = components[cid]
        candidates = [
            key
            for key in by_file[node.relative_path.replace("\\", "/")]
            if key != cid
            and node.class_name
            and components[key].name == node.class_name
            and components[key].start_line > 0
            and components[key].start_line <= node.start_line
            and components[key].end_line >= node.end_line
        ]
        if len(candidates) == 1:
            parents[cid] = candidates[0]
    for cid, node in sorted(components.items()):
        for dep in sorted(node.depends_on):
            for target in anchors.get(dep, []):
                for entry in anchors[cid]:
                    if entry == target:
                        continue
                    usage[target].append(
                        {
                            "id": cid,
                            "entry": entry,
                            "relation": "type_use" if dep in node.type_depends_on else "dependency",
                            "source_excerpt": _usage_excerpt(node, components[target]),
                            "start_line": node.start_line,
                        }
                    )
                    if dep in node.type_depends_on:
                        users[target].add(entry)
    executable = {
        entry
        for key, node in components.items()
        if node.component_type in ("method", "function")
        for entry in anchors[key]
    }
    return {
        cid: {
            "parent": parents.get(cid),
            "users": users[cid],
            "usage": usage[cid],
            "support_only": components[cid].component_type in STRUCTURAL_TYPES
            and cid not in executable,
        }
        for cid in ids
    }


def _repair_evidence(cid, row, tree, components, relations):
    owned = {key: name for name, info in reversed(list(tree.items())) for key in info["components"]}
    expanded = {**row, "source_excerpt": components[cid].source_code or ""}
    relation = relations.get(cid, {})
    usages = [{**item, "topic": owned.get(item["entry"])} for item in relation.get("usage", [])]
    usages.sort(key=lambda item: (item["topic"] or "", item["id"]))
    by_topic = defaultdict(list)
    for item in usages:
        by_topic[item["topic"]].append(item)
    evidence = []
    while by_topic and len(evidence) < 12:
        for topic in list(by_topic):
            evidence.append(by_topic[topic].pop(0))
            if not by_topic[topic]:
                del by_topic[topic]
            if len(evidence) == 12:
                break
    expanded["usage_evidence"] = evidence
    expanded["usage_count"] = len(usages)
    expanded["candidate_topics"] = sorted({item["topic"] for item in usages if item["topic"]})
    if relation.get("parent"):
        parent = relation["parent"]
        expanded["declared_in"] = {"id": parent, "topic": owned.get(parent)}
    return expanded


def _recover_ownership(pending, tree, relations):
    """Inherit only a confirmed container or unanimous, fully planned type users."""
    owned = {cid: name for name, info in reversed(list(tree.items())) for cid in info["components"]}
    recovered = set()
    while True:
        additions = {}
        for cid in pending:
            if cid in recovered:
                continue
            relation = relations.get(cid, {})
            parent = relation.get("parent")
            users = relation.get("users", set())
            topic = owned.get(parent)
            if (
                topic is None
                and relation.get("support_only")
                and users
                and all(user in owned for user in users)
            ):
                topics = {owned[user] for user in users}
                if len(topics) == 1:
                    topic = topics.pop()
            if topic is not None:
                additions[cid] = topic
        if not additions:
            break
        for cid, topic in additions.items():
            tree[topic]["components"].append(cid)
            owned[cid] = topic
        recovered.update(additions)
    if recovered:
        logger.info(
            "业务主题证据恢复：%d 个入口依据包含关系或一致的类型使用方归属。", len(recovered)
        )
    return recovered


def _repair_feedback(ids, refs, problem=""):
    return (
        "\nYou left references unassigned or unusable: " + ", ".join(refs[cid] for cid in ids) + "."
        "\nAssign EVERY listed reference exactly once, retaining its original number. "
        "Use usage_evidence and declared_in to identify what each declaration supports. "
        "Multiple candidate_topics mean shared use, not permission to guess by majority. "
        "Reuse an existing topic's EXACT name and omit purpose then; give an evidence-based "
        "purpose for a genuinely new responsibility. Never create a missing-evidence or "
        "unclassified topic. Return only the <GROUPED_COMPONENTS> envelope."
        + ("\nPrevious response problem: " + problem[:300] if problem else "")
    )


def _assign_batch(
    ids,
    rows,
    tree,
    config,
    completer,
    *,
    consolidation=False,
    components=None,
    relations=None,
    refs=None,
    attempts=3,
    defer_unresolved=False,
    repair=False,
):
    pending = list(ids)
    refs = refs or {cid: f"C{i}" for i, cid in enumerate(ids)}
    last_problem = ""
    stalled = False
    for attempt in range(attempts):
        if not pending:
            break
        before = len(pending)
        request_rows = dict(rows)
        if (repair or attempt) and components is not None and not consolidation:
            for cid in pending:
                request_rows[cid] = _repair_evidence(
                    cid, rows[cid], tree, components, relations or {}
                )
        size = max(1, len(pending) // 2) if stalled and attempt else len(pending)
        queue = [pending[i : i + size] for i in range(0, len(pending), size)]
        assigned = set()
        while queue:
            group = queue.pop(0)
            prompt_tree = {} if consolidation else tree
            feedback = _repair_feedback(group, refs, last_problem) if repair or attempt else ""
            prompt = (
                _prompt(
                    group,
                    request_rows,
                    prompt_tree,
                    config.language,
                    consolidation=consolidation,
                    refs=refs,
                )
                + feedback
            )
            if count_tokens(prompt) > config.max_token_per_module:
                if len(group) > 1:
                    half = max(1, len(group) // 2)
                    queue[:0] = [group[:half], group[half:]]
                    continue
                cid = group[0]
                reduced = dict(request_rows[cid])
                usages = reduced.get("usage_evidence", [])
                if reduced.get("source_excerpt") != rows[cid].get("source_excerpt"):
                    reduced["source_excerpt"] = rows[cid].get("source_excerpt", "")
                elif usages:
                    reduced["usage_evidence"] = usages[:-1]
                else:
                    raise BusinessClusteringError(
                        f"Topic catalog plus evidence cannot fit for {cid}"
                    )
                request_rows[cid] = reduced
                queue.insert(0, group)
                continue
            try:
                response = completer(prompt)
                assignments, problems = _read_assignments(response, group, prompt_tree, refs=refs)
            except (ValueError, SyntaxError, TypeError, RecursionError) as exc:
                last_problem = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "业务主题规划响应无效（%d/%d）：%s", attempt + 1, attempts, last_problem
                )
                continue
            for name, info in assignments.items():
                name = canonical_topic_name(tree, name)
                existing = tree.setdefault(
                    name,
                    {"purpose": "", "components": [], "children": {}, "analysis_mode": "business"},
                )
                if info["purpose"]:
                    existing["purpose"] = info["purpose"]
                existing["components"].extend(info["components"])
                assigned.update(info["components"])
            if problems:
                last_problem = "; ".join(problems[:5])
                logger.warning("业务主题规划部分条目被拒绝：%s", last_problem)
        pending = [cid for cid in pending if cid not in assigned]
        if not consolidation and relations:
            recovered = _recover_ownership(pending, tree, relations)
            pending = [cid for cid in pending if cid not in recovered]
        stalled = len(pending) == before
        if pending:
            logger.warning(
                "业务主题归属尚有缺口：%d 个，将补使用方证据%s。",
                len(pending),
                "并缩小修复批次" if stalled else "",
            )
    if pending and not defer_unresolved:
        label = "unresolved topics" if consolidation else "unresolved repository IDs"
        raise BusinessClusteringError(
            f"Business topic ownership incomplete after {attempts} attempts; {label}: "
            + "; ".join(pending)
            + f"; last response problem: {last_problem or 'model omitted the listed references'}"
        )
    return pending, last_problem


def _fit_requests(
    batches,
    rows,
    tree,
    config,
    completer,
    *,
    consolidation=False,
    components=None,
    relations=None,
    refs=None,
    attempts=3,
    defer_unresolved=False,
    repair=False,
):
    unresolved = []
    last_problem = ""
    for batch in batches:
        pending = list(batch)
        while pending:
            size = len(pending)
            prompt_tree = {} if consolidation else tree
            while (
                count_tokens(
                    _prompt(
                        pending[:size],
                        rows,
                        prompt_tree,
                        config.language,
                        consolidation=consolidation,
                    )
                )
                + 256
                > config.max_token_per_module
                or (not consolidation and size > config.max_leaf_nodes_per_cluster)
                or count_tokens(json.dumps([f"C{i}" for i in range(size)])) * 2 > config.max_tokens
            ):
                if size == 1:
                    raise BusinessClusteringError(
                        f"Topic catalog plus evidence cannot fit for {pending[0]}"
                    )
                size = max(1, size // 2)
            missing, problem = _assign_batch(
                pending[:size],
                rows,
                tree,
                config,
                completer,
                consolidation=consolidation,
                components=components,
                relations=relations,
                refs=refs,
                attempts=attempts,
                defer_unresolved=defer_unresolved,
                repair=repair,
            )
            unresolved.extend(missing)
            last_problem = problem or last_problem
            pending = pending[size:]
    return unresolved, last_problem


def resolve_primary_ownership(tree, expected_ids):
    """Give every component exactly one primary explanation, and record the rest.

    A component claimed by several topics is normal (shared models and
    converters legitimately serve several business capabilities), so it is not
    a planning error. The first topic that claimed it owns the full
    explanation; every other topic keeps it as a reference that must link
    back instead of explaining it again.
    """
    primary_owner = {}
    references = defaultdict(list)
    for name, info in tree.items():
        owned, shared = [], []
        for cid in info.get("components", []):
            if cid in primary_owner:
                shared.append(cid)
                references[primary_owner[cid]].append((name, cid))
            else:
                primary_owner[cid] = name
                owned.append(cid)
        info["components"] = owned
        if shared:
            info["shared_components"] = sorted(set(shared))
    for name, pairs in references.items():
        tree[name]["components_shared_with"] = sorted(
            {(cid, topic) for topic, cid in pairs}, key=lambda item: (item[0], item[1])
        )
    owned = [cid for info in tree.values() for cid in info.get("components", [])]
    if len(owned) != len(set(owned)) or set(owned) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(owned))
        raise BusinessClusteringError(
            "Business planning lost primary source ownership"
            + (f"; missing repository IDs: {'; '.join(missing)}" if missing else "")
        )
    logger.info(
        "业务主题归属完成：%d 个主题，%d/%d 个入口有唯一主说明，%d 个主题作为共享引用方。",
        len(tree),
        len(owned),
        len(expected_ids),
        sum(1 for info in tree.values() if info.get("shared_components")),
    )
    return tree


def cluster_business_modules(component_ids, components, config, completer):
    ids = resolve_component_ids(component_ids, components, strict=True)
    if not ids:
        return {}
    if (
        sum(count_tokens(components[cid].source_code or "") for cid in ids)
        <= config.max_token_per_module
    ):
        logger.info("业务源码在单模块预算内，保留完整仓库 Agent 分析模式。")
        return {}
    outgoing, incoming = dependency_neighborhoods(ids, components)
    relations = _ownership_relations(ids, components)
    rows = planning_evidence(ids, components, outgoing, incoming, relations=relations)
    costs = {cid: count_tokens(json.dumps(rows[cid], ensure_ascii=False)) for cid in ids}
    overhead = count_tokens(_prompt([], rows, {}, config.language))
    limit = getattr(config, "max_planning_refs_per_call", DEFAULT_MAX_PLANNING_REFS)

    def fits(batch):
        return (
            len(batch) <= min(limit, config.max_leaf_nodes_per_cluster)
            and overhead + sum(costs[cid] + 12 for cid in batch) <= config.max_token_per_module
        )

    batches = partition_by_relationships(ids, components, outgoing, incoming, fits)
    logger.info(
        "业务主题规划：%d 个分析入口，%d 个关联批次（每批最多 %d 个引用）；批次不直接成为页面。",
        len(ids),
        len(batches),
        limit,
    )
    tree = {}
    refs = {cid: f"C{i}" for i, cid in enumerate(ids)}
    pending, last_problem = _fit_requests(
        batches,
        rows,
        tree,
        config,
        completer,
        components=components,
        relations=relations,
        refs=refs,
        attempts=1,
        defer_unresolved=True,
    )
    for repair_round in range(2):
        owned = {cid for info in tree.values() for cid in info["components"]}
        pending = [cid for cid in pending if cid not in owned]
        recovered = _recover_ownership(pending, tree, relations)
        pending = [cid for cid in pending if cid not in recovered]
        if not pending:
            break
        cap = max(
            1, min(limit, config.max_leaf_nodes_per_cluster, len(pending)) // (2**repair_round)
        )
        groups = [pending[i : i + cap] for i in range(0, len(pending), cap)]
        pending, problem = _fit_requests(
            groups,
            rows,
            tree,
            config,
            completer,
            components=components,
            relations=relations,
            refs=refs,
            attempts=1,
            defer_unresolved=True,
            repair=True,
        )
        last_problem = problem or last_problem
    if pending:
        recovered = _recover_ownership(pending, tree, relations)
        pending = [cid for cid in pending if cid not in recovered]
    if pending:
        raise BusinessClusteringError(
            "Business topic ownership incomplete after discovery and 2 evidence-repair rounds; "
            "unresolved repository IDs: "
            + "; ".join(pending)
            + f"; last response problem: {last_problem or 'model omitted the listed references'}"
        )
    if len(batches) > 1 and len(tree) > 1:
        candidate_tree = tree
        topic_rows = {
            name: {
                "topic": name,
                "purpose": info["purpose"],
                "representative_evidence": [rows[cid] for cid in info["components"][:2]],
                "component_count": len(info["components"]),
            }
            for name, info in candidate_tree.items()
        }
        topic_ids = list(candidate_tree)
        reconciled = {}
        while topic_ids:
            budget = max(1, config.max_token_per_module // 3)
            group = []
            for name in topic_ids:
                proposal = group + [name]
                if (
                    group
                    and count_tokens(
                        _prompt(proposal, topic_rows, {}, config.language, consolidation=True)
                    )
                    > budget
                ):
                    break
                group = proposal
            candidates = {}
            _fit_requests(
                [group],
                topic_rows,
                candidates,
                config,
                completer,
                consolidation=True,
                components=components,
            )
            consumed = set()
            for name, info in candidates.items():
                target = reconciled.setdefault(
                    name,
                    {
                        "purpose": info["purpose"] or candidate_tree[name]["purpose"],
                        "components": [],
                        "children": {},
                        "analysis_mode": "business",
                    },
                )
                if info["purpose"]:
                    target["purpose"] = info["purpose"]
                for candidate in info["components"]:
                    if candidate not in candidate_tree or candidate in consumed:
                        raise BusinessClusteringError(
                            f"Invalid candidate topic in reconciliation: {candidate}"
                        )
                    consumed.add(candidate)
                    target["components"].extend(candidate_tree[candidate]["components"])
            if not consumed:
                raise BusinessClusteringError("Topic reconciliation made no progress")
            topic_ids = [name for name in topic_ids if name not in consumed]
        tree = reconciled
    tree = resolve_primary_ownership(tree, ids)
    owned = {cid: name for name, info in tree.items() for cid in info["components"]}
    for cid, relation in relations.items():
        primary = owned[cid]
        for user in sorted(relation["users"]):
            topic = owned.get(user)
            if topic and topic != primary:
                shared = tree[topic].setdefault("shared_components", [])
                if cid not in shared:
                    shared.append(cid)
                references = tree[primary].setdefault("components_shared_with", [])
                if (cid, topic) not in references:
                    references.append((cid, topic))
    return tree
