"""Per-run coordination for API requests, module names and resumable pages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import BoundedSemaphore
from uuid import uuid4

from codewiki.src.be.doc_layout import config_layout, module_doc_file
from codewiki.src.config import MODULE_TREE_FILENAME, OVERVIEW_FILENAME

logger = logging.getLogger(__name__)

_REQUESTS: ContextVar[GenerationRuntime | None] = ContextVar("codewiki_requests", default=None)


def generation_concurrency(config):
    if getattr(config, "provider", "openai-compatible") in {"claude-code", "codex"}:
        return 1
    return max(1, getattr(config, "concurrency", 2))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class MissingDocumentationError(RuntimeError):
    """An agent stopped before saving its required non-empty page.

    ``str(exc)`` keeps the full per-ID list because the continuation prompt to
    the model is built from it; the human-facing log uses the structured
    id sets below via :func:`format_evidence_gap_summary` instead of dumping
    hundreds of IDs onto the terminal.
    """

    def __init__(self, message, *, missing_ids=(), unresolved_ids=()):
        super().__init__(message)
        self.missing_ids = tuple(missing_ids)
        self.unresolved_ids = tuple(unresolved_ids)


def collect_required_sources(components, module_tree, module_path, *, business_analysis=False):
    """Repository IDs a page's completion must have read evidence for.

    Backend-agnostic: derived from the module tree node, not from a runtime
    instance. Business mode additionally requires the topic's own scope plus
    same-file method dependencies, so trimmed prompts cannot hide unread code.
    """
    node = module_tree
    for index, name in enumerate(module_path):
        node = node[name]
        if index < len(module_path) - 1:
            node = node.get("children", {})
    core = node.get("components", []) if module_path else []
    required = {
        dep
        for cid in core
        if cid in components
        for dep in components[cid].depends_on
        if dep in components and dep not in core
    }
    if business_analysis:
        required.update(core if module_path else components)
        files = {components[cid].relative_path for cid in core if cid in components}
        required.update(
            dep
            for item in components.values()
            if item.relative_path in files
            for dep in item.depends_on
            if dep in components
        )
    return required


def source_is_confirmed_missing(deps, path):
    """Only an inspected, absent path with no graph components is outside source scope."""

    def normalize(value):
        return os.path.normpath(value.replace("\\", "/")).replace("\\", "/")

    path = normalize(path)
    absent = {normalize(item) for item in getattr(deps, "source_files_missing", set())}
    return path in absent and not any(
        path in (normalize(node.relative_path), normalize(cid.partition("::")[0]))
        for cid, node in deps.components.items()
    )


def validate_source_reads(deps, *, extra_required=None):
    """Raise unless every required source has read evidence (component IDs or full files)."""
    components = deps.components
    required = collect_required_sources(
        components,
        deps.module_tree,
        deps.path_to_current_module,
        business_analysis=getattr(deps, "business_analysis", False),
    )
    required.update(extra_required or ())
    reads = getattr(deps, "source_reads", set())
    files = getattr(deps, "source_files_read", set())
    missing = [
        cid
        for cid in sorted(required)
        if cid not in reads and components[cid].relative_path.replace("\\", "/") not in files
    ]
    unresolved = {
        cid
        for cid in getattr(deps, "unresolved_components", set())
        if not source_is_confirmed_missing(deps, cid.partition("::")[0])
    }
    if missing or unresolved:
        reasons = []
        if missing:
            reasons.append(
                "Unread exact repository IDs; use read_code_components: " + "; ".join(missing)
            )
        if unresolved:
            reasons.append(
                "Unresolved read requests (NOT verified repository IDs): "
                + "; ".join(sorted(unresolved))
                + ". Resolve to actual graph IDs or inspect the exact file with "
                "str_replace_editor working_dir='repo', command='view'"
            )
        raise MissingDocumentationError(
            "Source analysis incomplete. " + ". ".join(reasons),
            missing_ids=missing,
            unresolved_ids=sorted(unresolved),
        )


def _short_repo_path(component_id: str) -> str:
    """Last two path segments of an ID's file part, e.g. feign/MmearsFeignApi.java."""
    path = component_id.replace("\\", "/").partition("::")[0] or component_id
    segments = path.split("/")
    return "/".join(segments[-2:]) if len(segments) > 1 else path


def _group_ids_by_file(ids) -> list[tuple[str, int]]:
    """Collapse component IDs to (file, count), widest files first."""
    counts: dict[str, int] = {}
    for cid in ids:
        path = cid.replace("\\", "/").partition("::")[0] or cid
        counts[path] = counts.get(path, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def format_evidence_gap_summary(exc, *, max_files: int = 5) -> str:
    """Compact, human-facing summary of an evidence-gap error.

    The continuation warning used to print every unread component ID (one real
    case was ~100 IDs from 7 files). Operators only need the scale and which
    files are involved; the model still receives the full list via str(exc).
    """
    lines = ["未读源码按文件补读即可（read_code_components 传文件路径会一次返回并登记整文件）："]
    found = False

    def add(title: str, ids, hint: str) -> None:
        nonlocal found
        if not ids:
            return
        found = True
        groups = _group_ids_by_file(ids)
        lines.append(f"{title}：{len(ids)} 个组件 / {len(groups)} 个文件。{hint}")
        for path, count in groups[:max_files]:
            lines.append(f"  - {_short_repo_path(path)}（{count} 个组件）")
        if len(groups) > max_files:
            lines.append(f"    …另有 {len(groups) - max_files} 个文件")

    add("未读源码证据", getattr(exc, "missing_ids", ()), "这是预算截断后的正常补读，不是失败")
    add(
        "未解析的读取请求", getattr(exc, "unresolved_ids", ()), "需先解析为真实图 ID 或查看确切文件"
    )
    if not found:
        return f"原因：{exc}"
    return "\n" + "\n".join(lines)


def validate_document_complete(document, *, config, working_dir, module_tree, pending=None):
    """Page non-empty plus complete child pages; overview guards when tracked.

    ``pending`` is the runtime's resumable-page set. Backends without page
    tracking (subscription mode) pass ``None`` and only get the on-disk checks;
    the completion definition itself stays identical across backends.
    """
    working_dir = Path(working_dir)
    layout = config_layout(config)
    if not document.is_file() or not document.read_text(encoding="utf-8").strip():
        relative = document.relative_to(working_dir).as_posix()
        raise MissingDocumentationError(f"Module documentation missing or empty: {relative}")

    def doc_for(name, module_path):
        return working_dir / module_doc_file(name, module_path, layout)

    def check_children(tree, prefix):
        for name, info in tree.items():
            module_path = prefix + [name]
            if doc_for(name, module_path) == document:

                def check(node, node_path):
                    for child_name, child in node.get("children", {}).items():
                        child_path = node_path + [child_name]
                        child_doc = doc_for(child_name, child_path)
                        child_pending = (
                            pending is not None
                            and child_doc.relative_to(working_dir).as_posix() in pending
                        )
                        # Existence + pending only, matching the historical
                        # runtime behavior; content encoding varies by writer.
                        if child_pending or not child_doc.is_file():
                            raise RuntimeError(f"Child documentation incomplete: {child_name}")
                        check(child, child_path)

                check(info, module_path)
            check_children(info.get("children", {}), module_path)

    check_children(module_tree, [])
    repo_name = os.path.basename(os.path.normpath(config.repo_path))
    if document.name == OVERVIEW_FILENAME or document == doc_for(repo_name, []):
        if pending is not None:
            incomplete = pending - {document.relative_to(working_dir).as_posix()}
            if incomplete:
                raise RuntimeError("Cannot complete overview while module pages are unfinished")
        from codewiki.src.be.module_naming import find_missing_module_docs

        if find_missing_module_docs(module_tree, str(working_dir), overview_required=False):
            raise RuntimeError("Cannot complete overview while module pages are missing")


class GenerationRuntime:
    def __init__(self, config, working_dir):
        self.config = config
        self.business_analysis: bool = False
        self.directory = Path(working_dir)
        self.tree_path = self.directory / MODULE_TREE_FILENAME
        self.tree = json.loads(self.tree_path.read_text(encoding="utf-8"))
        self.limit = generation_concurrency(config)
        self.requests = BoundedSemaphore(self.limit)
        self.delegation_locks = {}
        self.state_path = self.directory / "temp" / "incomplete_pages.json"
        self.pending = (
            set(json.loads(self.state_path.read_text(encoding="utf-8")))
            if self.state_path.exists()
            else set()
        )
        self.summary_state_path = self.directory / "temp" / "incomplete_summaries.json"
        self.pending_summaries = (
            set(json.loads(self.summary_state_path.read_text(encoding="utf-8")))
            if self.summary_state_path.exists()
            else set()
        )
        self.evidence_path = self.directory / "temp" / "source_evidence.json"
        self.evidence = (
            json.loads(self.evidence_path.read_text(encoding="utf-8"))
            if self.evidence_path.exists()
            else {}
        )

    def checkpoint(self):
        atomic_json(self.tree_path, self.tree)

    def doc_path(self, name, path):
        return self.directory / module_doc_file(name, path, config_layout(self.config))

    def invalidate_summaries(self, module_path):
        targets = {"overview.md"}
        for depth in range(1, len(module_path)):
            path = module_path[:depth]
            targets.add(self.doc_path(path[-1], path).relative_to(self.directory).as_posix())
        self.pending.update(targets)
        self.pending_summaries.update(targets)
        atomic_json(self.state_path, sorted(self.pending))
        atomic_json(self.summary_state_path, sorted(self.pending_summaries))

    def mark_pages_for_rewrite(self, relatives):
        """Force these pages to be regenerated by an agent.

        Used when a page was previously written as an index of sub-pages and now
        has to carry the material itself: its own evidence hash is still valid,
        so without this it would be skipped as up to date.
        """
        self.pending.update(relatives)
        atomic_json(self.state_path, sorted(self.pending))

    def is_pending(self, path):
        return path.relative_to(self.directory).as_posix() in self.pending

    def needs_agent_resume(self, path):
        relative = path.relative_to(self.directory).as_posix()
        return relative in self.pending and relative not in self.pending_summaries

    def begin(self, path, *, summary=False):
        relative = path.relative_to(self.directory).as_posix()
        if summary:
            self.pending_summaries.add(relative)
        else:
            self.pending_summaries.discard(relative)
        atomic_json(self.summary_state_path, sorted(self.pending_summaries))
        if path.exists():
            backup = self.directory / "temp" / "incomplete" / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                backup = backup.with_name(backup.stem + "." + uuid4().hex + backup.suffix)
            # COPY, never move: the previous version must stay readable at its
            # real path while the agent rewrites the page (a move made the md
            # "not found" in the IDE for the whole rewrite window), and a
            # crash/interruption must leave the previous version in place
            # instead of a missing file. str_replace_editor `create` is
            # allowed to overwrite exactly this whitelisted target.
            shutil.copy2(path, backup)
        self.pending.add(relative)
        atomic_json(self.state_path, sorted(self.pending))

    def validate_page(self, path):
        validate_document_complete(
            path,
            config=self.config,
            working_dir=self.directory,
            module_tree=self.tree,
            pending=self.pending,
        )

    def finish(self, path):
        self.validate_page(path)
        self.checkpoint()
        relative = path.relative_to(self.directory).as_posix()
        self.pending.discard(relative)
        atomic_json(self.state_path, sorted(self.pending))
        self.pending_summaries.discard(relative)
        atomic_json(self.summary_state_path, sorted(self.pending_summaries))

    def required_sources(self, components, module_path):
        return collect_required_sources(
            components, self.tree, module_path, business_analysis=self.business_analysis
        )

    def evidence_current(self, document, components, module_path):
        required = self.required_sources(components, module_path)
        if not required:
            return True
        record = self.evidence.get(document.relative_to(self.directory).as_posix(), {})
        sources = {
            cid: hashlib.sha256((components[cid].source_code or "").encode()).hexdigest()
            for cid in sorted(required)
        }
        return (
            record.get("sources") == sources
            and document.is_file()
            and record.get("page") == hashlib.sha256(document.read_bytes()).hexdigest()
        )

    def validate_source_evidence(self, deps):
        validate_source_reads(deps, extra_required=getattr(deps, "required_source_ids", set()))

    async def run_page_agent(self, agent, prompt, deps, document):
        from pydantic_ai.usage import RunUsage, UsageLimits

        usage = RunUsage()
        history = None
        result = None
        for attempt in range(self.config.agent_retries + 1):
            result = await agent.run(
                prompt,
                deps=deps,
                message_history=history,
                usage=usage,
                usage_limits=UsageLimits(request_limit=self.config.request_limit),
            )
            try:
                self.validate_page(document)
                self.validate_source_evidence(deps)
                required = self.required_sources(deps.components, deps.path_to_current_module)
                required.update(getattr(deps, "required_source_ids", set()))
                self.evidence[document.relative_to(self.directory).as_posix()] = {
                    "sources": {
                        cid: hashlib.sha256(
                            (deps.components[cid].source_code or "").encode()
                        ).hexdigest()
                        for cid in sorted(required)
                    },
                    "page": hashlib.sha256(document.read_bytes()).hexdigest(),
                }
                atomic_json(self.evidence_path, self.evidence)
                return result
            except MissingDocumentationError as exc:
                if attempt == self.config.agent_retries:
                    raise
                relative = document.relative_to(self.directory).as_posix()
                logger.warning(
                    "模块页或源码分析未完成，继续原 Agent 会话 %s（%d/%d）；请求预算不重置。%s",
                    relative,
                    attempt + 1,
                    self.config.agent_retries,
                    format_evidence_gap_summary(exc),
                )
                logger.debug("源码证据缺口完整清单 %s: %s", relative, exc)
                try:
                    from codewiki.src.be.trace import record_evidence_retry

                    record_evidence_retry(relative, attempt + 1)
                except Exception:  # noqa: BLE001 — telemetry must never break a run
                    pass
                history = result.all_messages()
                prompt = (
                    f"The required documentation page `{relative}` is incomplete: {exc} "
                    "Resolve each concrete issue above using the existing conversation and source evidence. "
                    "Do not repeat an unresolved identifier as if it were an exact graph ID. "
                    "Read efficiently: pass each repository-relative FILE PATH to read_code_components in "
                    "parallel — one file-path call returns and records every parsed component of that file, "
                    "so never read methods of the same file one by one. "
                    "Read the missing implementations, then save or update the complete page with str_replace_editor, "
                    f"working_dir='docs', path='{relative}'. This path is relative to the docs root, "
                    "not to the current module directory. Keep the required depth, source references "
                    "and diagrams; do not create a placeholder or just claim completion. "
                    "Read the target if it exists, then create or edit it as needed."
                )
        return result

    @asynccontextmanager
    async def activate(self):
        token = _REQUESTS.set(self)
        try:
            yield
        finally:
            _REQUESTS.reset(token)


@asynccontextmanager
async def request_slot():
    runtime = _REQUESTS.get()
    if runtime is None:
        yield
    else:
        while not runtime.requests.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            runtime.requests.release()


def sync_request(call, **kwargs):
    runtime = _REQUESTS.get()
    if runtime is None:
        return call(**kwargs)
    with runtime.requests:
        return call(**kwargs)


async def run_all(coroutines):
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise errors[0]
    return results
