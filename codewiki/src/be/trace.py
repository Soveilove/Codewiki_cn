"""Best-effort CLI timing traces without prompts, source code or credentials."""

from __future__ import annotations

import inspect
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any

import click

_ACTIVE: ContextVar[GenerationTrace | None] = ContextVar("codewiki_trace", default=None)
_PARENT: ContextVar[dict | None] = ContextVar("codewiki_trace_parent", default=None)
_LABELS = {
    "generation": "文档生成",
    "dependency_analysis": "依赖分析",
    "clustering": "模块聚类",
    "super_grouping": "上层分组",
    "module_generation": "模块生成",
    "module_agent": "模块",
    "sub_module": "子模块",
    "parent_overview": "父页/总览",
    "organize_docs": "目录与链接整理",
    "metadata": "生成元信息",
    "validation": "完整性校验",
    "wiki_doctor": "文档体检",
    "html_generation": "HTML 生成",
    "finalization": "收尾",
    "incremental_update": "增量更新",
}


def _safe_label(value):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value))


class GenerationTrace:
    def __init__(self):
        self.started = time.perf_counter()
        self.enabled = True
        self.stages = {}
        self.slowest_modules = []
        self.models = {}
        # Per-request context composition, aggregated for the summary. Lets us
        # see where input tokens actually go (initial prompt vs re-sent history
        # vs tool results) instead of guessing from totals. prompt_tree and
        # prompt_code are subsets of "prompt", never added into the total.
        self.composition = {"system": 0, "prompt": 0, "user_extra": 0, "assistant": 0, "tool": 0}
        self.prompt_sections = {"prompt_tree": 0, "prompt_code": 0}
        # Pages whose source-evidence check failed and had to continue the same
        # agent session: page -> number of continuation rounds.
        self.evidence_retries: dict[str, int] = {}
        self.tool_chars = {}
        self.tool_calls = {}
        self.module_chars = {}

    def print(self, message):
        if self.enabled:
            try:
                click.echo(
                    f"[trace +{time.perf_counter() - self.started:.1f}s] {message}", err=True
                )
            except (OSError, UnicodeError, ValueError):
                self.enabled = False

    def write(self, event):
        label = _LABELS.get(event["name"]) or event["name"]
        path = "/".join(event.get("module_path", []))
        if event["kind"] == "module":
            label += " " + (path or event.get("module_name", "仓库总览"))
        elif event["kind"] == "request":
            label = f"模型请求 {event['model']}"
            if path:
                label += f" | {path}"
        label = _safe_label(label)
        if event["event"] == "start":
            self.print(f"开始 {label}")
            return
        status = {"ok": "完成", "error": "失败", "skipped": "跳过"}[event["status"]]
        detail = ""
        if event.get("error_type"):
            detail += f" | {event['error_type']}"
        if "http_status" in event:
            detail += f" HTTP {event['http_status']}"
        if event.get("reason"):
            detail += " | " + event["reason"]
        usage = event.get("usage", {})
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        if input_tokens is not None:
            detail += f" | 输入 {input_tokens} tokens"
        if output_tokens is not None:
            detail += f" | 输出 {output_tokens} tokens"
        self.print(f"{status} {label} | {event['duration_seconds']:.2f}s{detail}")
        kind = event["kind"]
        if kind == "stage":
            name = event["name"]
            self.stages[name] = self.stages.get(name, 0.0) + event["duration_seconds"]
        elif kind == "module" and event["status"] != "skipped":
            self.slowest_modules.append((label, event["duration_seconds"], status))
            self.slowest_modules.sort(key=lambda item: item[1], reverse=True)
            del self.slowest_modules[5:]
        elif kind == "request":
            totals = self.models.setdefault(
                event["model"], {"requests": 0, "errors": 0, "seconds": 0.0}
            )
            totals["requests"] += 1
            totals["errors"] += event["status"] == "error"
            totals["seconds"] += event["duration_seconds"]
            composition = event.get("composition")
            if composition:
                for key in self.composition:
                    self.composition[key] += composition.get(key, 0)
                for key in self.prompt_sections:
                    self.prompt_sections[key] += composition.get(key, 0)
                for name, chars in (composition.get("tool_chars") or {}).items():
                    self.tool_chars[name] = self.tool_chars.get(name, 0) + chars
                for name, count in (composition.get("tool_calls") or {}).items():
                    self.tool_calls[name] = self.tool_calls.get(name, 0) + count
                module = "/".join(event.get("module_path", [])) or "(根/非模块)"
                # Only top-level buckets: prompt_tree/prompt_code are subsets of
                # prompt and would double-count the request.
                self.module_chars[module] = self.module_chars.get(module, 0) + sum(
                    composition.get(key, 0) for key in self.composition
                )

    def summary(self, status):
        self.print(f"耗时汇总 | {status} | 总耗时 {time.perf_counter() - self.started:.2f}s")
        for name, seconds in sorted(self.stages.items(), key=lambda item: item[1], reverse=True):
            self.print(f"阶段 {_LABELS.get(name, name)} | {seconds:.2f}s")
        for model, totals in self.models.items():
            self.print(
                f"模型 {_safe_label(model)} | 请求 {totals['requests']} 次"
                f" | 失败 {totals['errors']} 次 | 累计 {totals['seconds']:.2f}s"
            )
        for label, seconds, result in self.slowest_modules:
            self.print(f"最慢任务 {label} | {seconds:.2f}s | {result}")
        self.print("阶段/父子任务耗时含内部工作，不可相加；请求次数不含 SDK 内部重试。")
        self._summary_composition()

    def _summary_composition(self):
        """Where the input payload actually goes, in characters."""
        if not any(self.composition.values()):
            return
        total = sum(self.composition.values())

        def share(key):
            value = self.composition[key]
            return f"{value / 1000:.0f}k ({value * 100 // total}%)" if total else "0"

        self.print(
            "上下文构成 | 系统提示 "
            + share("system")
            + " | 首条提示(含内联源码) "
            + share("prompt")
            + " | 助手历史 "
            + share("assistant")
            + " | 工具返回 "
            + share("tool")
            + f" | 合计 {total / 1000:.0f}k 字符"
        )
        prompt_total = self.composition["prompt"]
        if prompt_total:
            tree = self.prompt_sections["prompt_tree"]
            code = self.prompt_sections["prompt_code"]
            other = max(0, prompt_total - tree - code)

            def part(value):
                return f"{value / 1000:.0f}k ({value * 100 // prompt_total}%)"

            self.print(
                "首条提示构成 | 模块树 "
                + part(tree)
                + " | 内联源码 "
                + part(code)
                + " | 模板/规则/说明 "
                + part(other)
            )
        if self.evidence_retries:
            retry_total = sum(self.evidence_retries.values())
            detail = " | ".join(
                f"{name} {rounds} 轮"
                for name, rounds in sorted(self.evidence_retries.items(), key=lambda kv: -kv[1])[:5]
            )
            self.print(
                f"源码补读续跑 | {len(self.evidence_retries)} 个页/{retry_total} 轮 | {detail}"
            )
        if self.tool_chars:
            detail = " | ".join(
                f"{name} {chars / 1000:.0f}k 字符/{self.tool_calls.get(name, 0)} 次"
                for name, chars in sorted(self.tool_chars.items(), key=lambda kv: -kv[1])
            )
            self.print("工具返回构成 | " + detail)
        if self.module_chars:
            top = sorted(self.module_chars.items(), key=lambda kv: -kv[1])[:5]
            detail = " | ".join(f"{name} {chars / 1000:.0f}k" for name, chars in top)
            self.print("上下文最重的模块 | " + detail)


@contextmanager
def trace_span(name, kind="stage", **fields):
    trace = _ACTIVE.get()
    if trace is None:
        yield {}
        return
    parent = _PARENT.get()
    span = {"name": name, "kind": kind}
    if parent and "module_path" in parent:
        span["module_path"] = parent["module_path"]
    span.update(fields)
    outcome: dict[str, Any] = {"status": "ok"}
    token = _PARENT.set({**span, "outcome": outcome})
    started = time.perf_counter()
    trace.write({"event": "start", **span})
    try:
        yield outcome
    except BaseException as exc:
        outcome.update(status="error", error_type=type(exc).__name__)
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            outcome["http_status"] = status_code
        raise
    finally:
        _PARENT.reset(token)
        trace.write(
            {
                "event": "finish",
                **span,
                **outcome,
                "duration_seconds": time.perf_counter() - started,
            }
        )


def trace_active() -> bool:
    """Whether a trace is currently collecting (cheap guard for instrumentation)."""
    return _ACTIVE.get() is not None


def record_evidence_retry(page: str, attempt: int) -> None:
    """Record a source-evidence continuation round for a page.

    A non-zero round count means the page was written before all required
    source had evidence and had to continue the same agent session — each
    round re-sends the whole prompt, so this tracks the cost the inline
    prefetch is meant to remove.
    """
    trace = _ACTIVE.get()
    if trace is not None:
        trace.evidence_retries[page] = max(trace.evidence_retries.get(page, 0), attempt)


def trace_enabled() -> bool:
    """Whether stage timing should be printed for this run.

    ``--no-trace`` and ``codewiki config --no-trace`` turn it off; the
    ``CODEWIKI_TRACE`` environment variable still wins for CI and debugging.
    """
    override = os.environ.get("CODEWIKI_TRACE")
    if override is not None:
        return override.strip().lower() not in {"0", "false", "off", "no"}
    from codewiki.src.config import is_cli_context, trace_setting

    if not is_cli_context():
        return False
    return trace_setting()


@contextmanager
def generation_trace():
    from codewiki.src.config import is_cli_context

    if _ACTIVE.get() is not None or not is_cli_context() or not trace_enabled():
        yield
        return
    trace = GenerationTrace()
    token = _ACTIVE.set(trace)
    parent_token = _PARENT.set(None)
    status = "失败/中断"
    try:
        with trace_span("generation", "run"):
            yield
        status = "完成"
    finally:
        try:
            trace.summary(status)
        finally:
            _PARENT.reset(parent_token)
            _ACTIVE.reset(token)


def trace_generation(function):
    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def wrapped_async(*args, **kwargs):
            with generation_trace():
                return await function(*args, **kwargs)

        return wrapped_async

    @wraps(function)
    def wrapped_sync(*args, **kwargs):
        with generation_trace():
            return function(*args, **kwargs)

    return wrapped_sync


def traced(name, kind="stage", fields=()):
    def decorate(function):
        signature = inspect.signature(function)

        def span_fields(args, kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            values = {key: bound.arguments[key] for key in fields}
            if "module_path" in values:
                values["module_path"] = list(values["module_path"])
            return values

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def wrapped_async(*args, **kwargs):
                if _ACTIVE.get() is None:
                    return await function(*args, **kwargs)
                with trace_span(name, kind, **span_fields(args, kwargs)):
                    return await function(*args, **kwargs)

            return wrapped_async

        @wraps(function)
        def wrapped_sync(*args, **kwargs):
            if _ACTIVE.get() is None:
                return function(*args, **kwargs)
            with trace_span(name, kind, **span_fields(args, kwargs)):
                return function(*args, **kwargs)

        return wrapped_sync

    return decorate


def mark_skipped(reason):
    parent = _PARENT.get()
    if parent:
        parent["outcome"].update(status="skipped", reason=reason)


def record_response(outcome, response):
    if _ACTIVE.get() is None:
        return
    try:
        usage = getattr(response, "usage", None)
        if usage is not None:
            from codewiki.src.be.backend import usage_to_dict

            numbers = usage_to_dict(usage)
            if numbers:
                outcome["usage"] = numbers
    except Exception:  # noqa: BLE001 — optional telemetry must not break generation
        pass


def trace_request(call, /, **kwargs):
    with trace_span("completion", "request", model=kwargs.get("model", "unknown")) as outcome:
        from codewiki.src.be.generation_runtime import sync_request

        response = sync_request(call, **kwargs)
        record_response(outcome, response)
        return response
