# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""CLI timing is observational: no extra requests or changes to generated pages."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from codewiki.src import config as config_module
from codewiki.src.be import documentation_generator as generation
from codewiki.src.be import llm_services
from codewiki.src.be.trace import (
    GenerationTrace,
    _ACTIVE,
    _PARENT,
    mark_skipped,
    trace_generation,
    trace_request,
    trace_span,
    traced,
)
from codewiki.src.config import Config


@pytest.fixture(autouse=True)
def cli_context(monkeypatch):
    monkeypatch.setattr(config_module, "_CLI_CONTEXT", True)
    monkeypatch.setenv("CODEWIKI_TRACE", "1")


class Runner:
    @trace_generation
    async def run(self, fail=False):
        with trace_span("clustering"):
            mark_skipped("复用已有模块树")
        await self.module(["身份认证"])
        if fail:
            raise RuntimeError("SECRET not for terminal")
        return "unchanged"

    @traced("module_agent", "module", fields=("module_path",))
    async def module(self, module_path):
        response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))
        assert trace_request(lambda **kwargs: response, model="main", messages="SECRET") is response
        with trace_span("sub_module", "module", module_path=module_path + ["会话"]):
            with trace_span("agent_completion", "request", model="fallback"):
                await asyncio.sleep(0)


def test_enabled_terminal_output_and_context_cleanup(capsys):
    assert asyncio.run(Runner().run()) == "unchanged"
    output = capsys.readouterr()
    assert output.out == ""
    assert "开始 模块 身份认证" in output.err
    assert "模型请求 fallback | 身份认证/会话" in output.err
    assert "输入 10 tokens | 输出 5 tokens" in output.err
    assert "跳过 模块聚类" in output.err
    assert "耗时汇总 | 完成" in output.err
    assert "最慢任务" in output.err
    assert "SECRET" not in output.err
    assert _ACTIVE.get() is None
    assert _PARENT.get() is None


@pytest.mark.parametrize("disabled", ["0", "false", "off"])
def test_can_disable_without_changing_result(monkeypatch, capsys, disabled):
    monkeypatch.setenv("CODEWIKI_TRACE", disabled)
    assert asyncio.run(Runner().run()) == "unchanged"
    assert capsys.readouterr().err == ""


def test_trace_is_on_by_default(monkeypatch, capsys):
    monkeypatch.delenv("CODEWIKI_TRACE", raising=False)
    assert asyncio.run(Runner().run()) == "unchanged"
    assert "耗时汇总 | 完成" in capsys.readouterr().err


def test_non_cli_does_not_pollute_mcp(monkeypatch, capsys):
    monkeypatch.setattr(config_module, "_CLI_CONTEXT", False)
    assert asyncio.run(Runner().run()) == "unchanged"
    assert capsys.readouterr() == ("", "")


def test_failure_preserved_and_summary_printed(capsys):
    with pytest.raises(RuntimeError, match="SECRET"):
        asyncio.run(Runner().run(fail=True))
    output = capsys.readouterr().err
    assert "失败 文档生成" in output
    assert "RuntimeError" in output
    assert "耗时汇总 | 失败/中断" in output
    assert "SECRET" not in output
    assert _ACTIVE.get() is None
    assert _PARENT.get() is None


def test_broken_terminal_does_not_abort(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("closed terminal")

    monkeypatch.setattr("codewiki.src.be.trace.click.echo", broken)
    assert asyncio.run(Runner().run()) == "unchanged"


def test_invalid_usage_does_not_change_response(capsys):
    class Response:
        @property
        def usage(self):
            raise RuntimeError("SECRET invalid telemetry")

    response = Response()
    token = _ACTIVE.set(GenerationTrace())
    try:
        assert trace_request(lambda **kwargs: response, model="main") is response
    finally:
        _ACTIVE.reset(token)
    assert "SECRET" not in capsys.readouterr().err


def test_async_module_paths_are_isolated():
    events = []
    trace = GenerationTrace()
    trace.write = events.append

    async def run():
        token = _ACTIVE.set(trace)
        try:
            await asyncio.gather(Runner().module(["甲"]), Runner().module(["乙"]))
        finally:
            _ACTIVE.reset(token)

    asyncio.run(run())
    requests = [event for event in events if event["kind"] == "request"]
    assert {tuple(event["module_path"]) for event in requests} == {
        ("甲",),
        ("甲", "会话"),
        ("乙",),
        ("乙", "会话"),
    }
    assert _PARENT.get() is None


def test_agent_request_error_and_usage_are_traced(monkeypatch, capsys):
    model = llm_services.CachingOpenAIModel(
        "main",
        prompt_caching=False,
        provider=OpenAIProvider(base_url="http://localhost:1/v1", api_key="SECRET"),
    )
    calls = []
    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3))

    async def fake_create(self, *args):
        calls.append(self.model_name)
        if len(calls) == 1:
            raise ModelHTTPError(429, self.model_name, "SECRET response")
        return response

    monkeypatch.setattr(OpenAIChatModel, "_completions_create", fake_create)

    async def run():
        token = _ACTIVE.set(GenerationTrace())
        try:
            with pytest.raises(ModelHTTPError):
                await model._completions_create([], False, {}, ModelRequestParameters())
            result = await model._completions_create([], False, {}, ModelRequestParameters())
            assert result is response
        finally:
            _ACTIVE.reset(token)

    asyncio.run(run())
    output = capsys.readouterr().err
    assert calls == ["main", "main"]
    assert "HTTP 429" in output
    assert "输入 7 tokens | 输出 3 tokens" in output
    assert "SECRET" not in output


def test_sync_completion_keeps_arguments_and_result(monkeypatch, capsys):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="wiki"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
    )
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm_services, "create_openai_client", lambda config: client)
    config = SimpleNamespace(
        main_model="main", provider="openai-compatible", llm_base_url="", max_tokens=42
    )
    token = _ACTIVE.set(GenerationTrace())
    try:
        assert llm_services.call_llm("SECRET prompt", config, model="cluster") == "wiki"
    finally:
        _ACTIVE.reset(token)
    assert calls == [
        {
            "model": "cluster",
            "messages": [{"role": "user", "content": "SECRET prompt"}],
            "max_tokens": 42,
        }
    ]
    output = capsys.readouterr().err
    assert "模型请求 cluster" in output
    assert "SECRET" not in output


def test_generation_stages_and_resume_do_not_change_pages(tmp_path, monkeypatch, capsys):
    config = Config.from_cli(
        repo_path=str(tmp_path),
        output_dir=str(tmp_path / "output"),
        llm_base_url="http://localhost:1/v1",
        llm_api_key="test",
        main_model="main",
        cluster_model="cluster",
        agent_instructions={"language": "zh"},
    )
    docs = tmp_path / "output"
    docs.mkdir()
    tree = {"身份认证": {"components": [], "children": {}}}
    monkeypatch.setattr(generation, "cluster_business_modules", lambda *args, **kwargs: tree)
    calls = []

    class Backend:
        async def run_module_agent(self, **kwargs):
            calls.append("module")
            (docs / "身份认证.md").write_text("# 身份认证\n保持原有内容", encoding="utf-8")
            return tree

        def complete(self, *args, **kwargs):
            calls.append("overview")
            return "<OVERVIEW># 总览\n保持原有质量</OVERVIEW>"

    generator = generation.DocumentationGenerator(config, backend=Backend())
    monkeypatch.setattr(generator.graph_builder, "build_dependency_graph", lambda: ({}, []))
    asyncio.run(generator.run())
    output = capsys.readouterr().err
    for label in ("依赖分析", "模块聚类", "模块生成", "父页/总览", "完整性校验"):
        assert f"开始 {label}" in output
    pages = {p.relative_to(docs): p.read_bytes() for p in docs.rglob("*.md")}
    assert "保持原有质量" in (docs / "overview.md").read_text(encoding="utf-8")
    assert not list(docs.rglob("*.jsonl"))
    assert json.loads((docs / "module_tree.json").read_text(encoding="utf-8")) == tree
    asyncio.run(generator.run())
    assert "跳过 模块聚类" in capsys.readouterr().err
    assert calls.count("overview") == 1
    assert {p.relative_to(docs): p.read_bytes() for p in docs.rglob("*.md")} == pages


def test_real_cli_pipeline_prints_without_verbose(tmp_path, monkeypatch, capsys):
    from codewiki.cli.adapters.doc_generator import CLIDocumentationGenerator
    from codewiki.src.be import business_clustering as clustering

    output_dir = tmp_path / "wiki"
    tree = {"身份认证": {"components": [], "children": {}}}
    calls = []

    class Backend:
        @traced("module_agent", "module", fields=("module_name", "module_path"))
        async def run_module_agent(
            self, module_name, components, core_component_ids, module_path, working_dir
        ):
            calls.append("module")
            (output_dir / "身份认证.md").write_text("# 身份认证", encoding="utf-8")
            return tree

        def complete(self, prompt, model=None, system_prompt=None):
            calls.append(model or "main")
            response = trace_request(
                lambda **kwargs: SimpleNamespace(usage=None, content="<OVERVIEW># 总览</OVERVIEW>"),
                model=model or "main",
            )
            return response.content

    def cluster(*args, completer, **kwargs):
        completer("SECRET prompt")
        return tree

    monkeypatch.setattr(generation, "get_backend", lambda config: Backend())
    monkeypatch.setattr(
        generation.DependencyGraphBuilder, "build_dependency_graph", lambda self: ({}, [])
    )
    monkeypatch.setattr(clustering, "cluster_business_modules", cluster)
    adapter = CLIDocumentationGenerator(
        tmp_path,
        output_dir,
        {
            "base_url": "http://localhost:1/v1",
            "api_key": "SECRET",
            "main_model": "main",
            "cluster_model": "cluster",
        },
        verbose=False,
    )
    job = adapter.generate()
    output = capsys.readouterr().err
    assert calls == ["cluster", "module", "main"]
    assert output.count("开始 文档生成") == 1
    assert "开始 依赖分析" in output
    assert "开始 模块聚类" in output
    assert "开始 模块 身份认证" in output
    assert "模型请求 cluster" in output
    assert "模型请求 main" in output
    assert "完整性校验" in output
    assert "耗时汇总 | 完成" in output
    assert "SECRET" not in output
    assert "overview.md" in job.files_generated
    assert not list(output_dir.rglob("*.jsonl"))
