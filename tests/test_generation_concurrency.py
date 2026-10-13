# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Concurrency preserves coverage, task isolation, dependencies and resumability."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from codewiki.cli.commands.generate import generate_command
from codewiki.src.be import cluster_modules as clustering
from codewiki.src.be import pydantic_ai_backend as api
from codewiki.src.be.agent_tools import generate_sub_module_documentations as delegation
from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.documentation_generator import DocumentationGenerator
from codewiki.src.be.generation_runtime import (
    GenerationRuntime,
    generation_concurrency,
    request_slot,
)
from codewiki.src.be import llm_services
from codewiki.src.config import Config


def config(tmp_path, concurrency=2, layout="hierarchical", max_depth=1):
    return Config.from_cli(
        repo_path=str(tmp_path / "repo"),
        output_dir=str(tmp_path),
        llm_base_url="http://localhost:1/v1",
        llm_api_key="test",
        main_model="main",
        cluster_model="cluster",
        concurrency=concurrency,
        layout=layout,
        agent_instructions={"language": "zh"},
        max_depth=max_depth,
    )


def write_tree(tmp_path, tree):
    (tmp_path / "module_tree.json").write_text(
        json.dumps(tree, ensure_ascii=False), encoding="utf-8"
    )


def backend(tmp_path, monkeypatch, Agent, concurrency=2, layout="hierarchical", max_depth=1):
    monkeypatch.setattr(api, "Agent", Agent)
    monkeypatch.setattr(api, "create_fallback_models", lambda config: None)
    monkeypatch.setattr(delegation, "Agent", Agent)
    monkeypatch.setattr(delegation, "create_fallback_models", lambda config: None)
    cfg = config(tmp_path, concurrency, layout, max_depth)
    return cfg, api.PydanticAIBackend(cfg)


def test_real_pydantic_agent_and_editor_create_owned_pages(tmp_path, monkeypatch):
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

    tree = {name: {"components": [], "children": {}} for name in ("认证", "上传")}
    write_tree(tmp_path, tree)

    def respond(messages, info):
        if any(isinstance(message, ModelResponse) for message in messages):
            return ModelResponse(parts=[TextPart("done")])
        system = "\n".join(
            part.content
            for message in messages
            for part in message.parts
            if getattr(part, "part_kind", None) == "system-prompt"
        )
        name = "认证" if "认证" in system else "上传"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "str_replace_editor",
                    {
                        "command": "create",
                        "working_dir": "docs",
                        "path": f"{name}.md",
                        "file_text": f"# {name}\n完整文档",
                    },
                )
            ]
        )

    monkeypatch.setattr(api, "create_fallback_models", lambda config: FunctionModel(respond))
    cfg = config(tmp_path, layout="flat")
    be = api.PydanticAIBackend(cfg)
    be.complete = lambda *args, **kwargs: "<OVERVIEW># 总览</OVERVIEW>"
    asyncio.run(DocumentationGenerator(cfg, backend=be).generate_module_documentation({}, []))
    assert (tmp_path / "认证.md").read_text(encoding="utf-8") == "# 认证\n完整文档"
    assert (tmp_path / "上传.md").read_text(encoding="utf-8") == "# 上传\n完整文档"
    assert (tmp_path / "overview.md").exists()
    assert json.loads((tmp_path / "temp" / "incomplete_pages.json").read_text()) == []


def test_real_sub_agent_recovers_missing_nested_page_with_same_history(tmp_path, monkeypatch):
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai import Agent

    tree = {"客户运营": {"components": [], "children": {}}}
    write_tree(tmp_path, tree)
    cfg = config(tmp_path)
    runtime = GenerationRuntime(cfg, str(tmp_path))
    calls = []

    def respond(messages, info):
        calls.append(len(messages))
        if len(calls) == 1:
            return ModelResponse(parts=[TextPart("# 工作台订单管理\n只返回文档但没写文件")])
        if len(calls) == 2:
            assert any(isinstance(message, ModelResponse) for message in messages)
            assert any("relative to the docs root" in str(message) for message in messages)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "str_replace_editor",
                        {
                            "command": "create",
                            "working_dir": "docs",
                            "path": "客户运营/工作台订单管理.md",
                            "file_text": "# 工作台订单管理\n真实落盘文档",
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    monkeypatch.setattr(delegation, "create_fallback_models", lambda config: FunctionModel(respond))
    monkeypatch.setattr(delegation, "Agent", Agent)
    deps = CodeWikiDeps(
        str(tmp_path),
        str(tmp_path),
        {},
        {},
        ["客户运营"],
        "客户运营",
        runtime.tree,
        2,
        1,
        cfg,
        generation_runtime=runtime,
    )

    async def run():
        async with runtime.activate():
            return await delegation.generate_sub_module_documentation(
                SimpleNamespace(deps=deps),
                {"工作台订单管理": []},
            )

    asyncio.run(run())
    assert len(calls) == 3
    assert (tmp_path / "客户运营" / "工作台订单管理.md").read_text(
        encoding="utf-8"
    ) == "# 工作台订单管理\n真实落盘文档"
    assert runtime.pending == set()


def test_missing_page_repair_never_resets_request_budget(tmp_path):
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.exceptions import UsageLimitExceeded

    write_tree(tmp_path, {})
    cfg = config(tmp_path)
    cfg.request_limit = 2
    cfg.agent_retries = 3
    runtime = GenerationRuntime(cfg, str(tmp_path))
    page = tmp_path / "missing.md"
    runtime.begin(page)
    calls = []

    def respond(messages, info):
        calls.append(len(messages))
        return ModelResponse(parts=[TextPart("done without writing")])

    with pytest.raises(UsageLimitExceeded):
        asyncio.run(runtime.run_page_agent(Agent(FunctionModel(respond)), "write page", None, page))
    assert len(calls) == 2
    assert runtime.is_pending(page)
    assert not page.exists()


def test_missing_page_repair_exhaustion_still_fails(tmp_path):
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, TextPart
    from codewiki.src.be.generation_runtime import MissingDocumentationError

    write_tree(tmp_path, {})
    cfg = config(tmp_path)
    cfg.agent_retries = 1
    runtime = GenerationRuntime(cfg, str(tmp_path))
    page = tmp_path / "missing.md"
    runtime.begin(page)
    calls = []

    def respond(messages, info):
        calls.append(1)
        return ModelResponse(parts=[TextPart("done without writing")])

    with pytest.raises(MissingDocumentationError, match="missing.md"):
        asyncio.run(runtime.run_page_agent(Agent(FunctionModel(respond)), "write page", None, page))
    assert len(calls) == 2
    assert runtime.is_pending(page)
    assert not page.exists()


def test_write_denial_reports_exact_owned_page_path(tmp_path):
    from codewiki.src.be.agent_tools.str_replace_editor import check_write_allowed

    expected = tmp_path / "客户运营" / "工作台订单管理.md"
    error = check_write_allowed(str(tmp_path / "工作台订单管理.md"), {str(expected.resolve())})
    assert str(expected.resolve()) in error
    assert "including all parent directories" in error
    assert check_write_allowed(str(expected), {str(expected.resolve())}) is None


@pytest.mark.parametrize("limit", [1, 2])
def test_real_nested_agents_correct_denied_paths_without_cross_writes(
    tmp_path, monkeypatch, caplog, limit
):
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

    tree = {"客户运营": {"components": [], "children": {}}}
    write_tree(tmp_path, tree)
    cfg = config(tmp_path, concurrency=limit)
    runtime = GenerationRuntime(cfg, str(tmp_path))
    seen = []

    def respond(messages, info):
        text = "\n".join(
            str(part.content)
            for message in messages
            for part in message.parts
            if getattr(part, "part_kind", None) == "system-prompt"
        )
        name = "订单管理" if "订单管理" in text else "个人中心"
        count = sum(isinstance(message, ModelResponse) for message in messages)
        seen.append((name, count))
        if count == 0:
            path = f"{name}.md"
        elif count == 1:
            assert "Exact docs-relative target paths" in str(messages[-1])
            path = f"客户运营/{name}.md"
        else:
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "str_replace_editor",
                    {
                        "working_dir": "docs",
                        "command": "create",
                        "path": path,
                        "file_text": f"# {name}\n```mermaid\ngraph TD; A-->B\n```",
                    },
                )
            ]
        )

    monkeypatch.setattr(delegation, "Agent", Agent)
    monkeypatch.setattr(delegation, "create_fallback_models", lambda config: FunctionModel(respond))
    deps = CodeWikiDeps(
        str(tmp_path),
        str(tmp_path),
        {},
        {},
        ["客户运营"],
        "客户运营",
        runtime.tree,
        2,
        1,
        cfg,
        generation_runtime=runtime,
    )

    async def run():
        async with runtime.activate():
            await delegation.generate_sub_module_documentation(
                SimpleNamespace(deps=deps), {"订单管理": [], "个人中心": []}
            )

    asyncio.run(run())
    assert len(seen) == 6
    assert "文档写入路径被拒绝" in caplog.text
    assert runtime.pending == set()
    for name in ("订单管理", "个人中心"):
        assert not (tmp_path / f"{name}.md").exists()
        assert (
            (tmp_path / "客户运营" / f"{name}.md")
            .read_text(encoding="utf-8")
            .startswith("# " + name)
        )
    saved = json.loads((tmp_path / "module_tree.json").read_text(encoding="utf-8"))
    assert set(saved["客户运营"]["children"]) == {"订单管理", "个人中心"}


def test_pending_summary_resumes_summary_not_agent(tmp_path, monkeypatch):
    tree = {"概要": {"components": [], "children": {"子": {"components": [], "children": {}}}}}
    write_tree(tmp_path, tree)
    # A two-level tree only exists when the depth budget allows it.
    cfg = config(tmp_path, layout="flat", max_depth=2)
    runtime = GenerationRuntime(cfg, str(tmp_path))
    runtime.begin(tmp_path / "概要.md", summary=True)
    (tmp_path / "概要.md").write_text("# unfinished", encoding="utf-8")
    (tmp_path / "子.md").write_text("# 完整子页", encoding="utf-8")
    calls = []
    monkeypatch.setattr(api, "create_fallback_models", lambda config: None)
    be = api.PydanticAIBackend(cfg)

    def complete(prompt, **kwargs):
        calls.append(prompt)
        return "<OVERVIEW># 完整概要</OVERVIEW>"

    monkeypatch.setattr(be, "complete", complete)
    generator = DocumentationGenerator(cfg, backend=be)
    asyncio.run(generator.generate_module_documentation({}, []))
    assert len(calls) == 2
    assert (tmp_path / "概要.md").read_text(encoding="utf-8") == "# 完整概要"
    assert generator.generation_runtime.pending == set()
    assert generator.generation_runtime.pending_summaries == set()


def test_checkpoint_error_does_not_clear_pending(tmp_path, monkeypatch):
    write_tree(tmp_path, {"模块": {"components": [], "children": {}}})
    runtime = GenerationRuntime(config(tmp_path), str(tmp_path))
    page = tmp_path / "模块.md"
    runtime.begin(page)
    page.write_text("# content", encoding="utf-8")

    def fail():
        raise PermissionError("checkpoint failed")

    monkeypatch.setattr(runtime, "checkpoint", fail)
    with pytest.raises(PermissionError):
        runtime.finish(page)
    reloaded = GenerationRuntime(config(tmp_path), str(tmp_path))
    assert runtime.is_pending(page)
    assert reloaded.is_pending(page)


def test_default_and_cli_limit(tmp_path):
    assert config(tmp_path).concurrency == 2
    assert generation_concurrency(SimpleNamespace(provider="codex", concurrency=9)) == 1
    result = CliRunner().invoke(generate_command, ["--help"])
    assert result.exit_code == 0
    assert "--concurrency" in result.output
    assert "default: 2" in result.output
    assert CliRunner().invoke(generate_command, ["--concurrency", "0"]).exit_code == 2


@pytest.mark.parametrize("limit", [1, 2, 4])
def test_actual_agent_requests_obey_limit_and_release_on_error(tmp_path, monkeypatch, limit):
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from pydantic_ai.models import ModelRequestParameters

    write_tree(tmp_path, {})
    runtime = GenerationRuntime(config(tmp_path, limit), str(tmp_path))
    active = peak = 0

    async def fake_create(self, *args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return SimpleNamespace(usage=None)
        finally:
            active -= 1

    monkeypatch.setattr(OpenAIChatModel, "_completions_create", fake_create)
    model = llm_services.CachingOpenAIModel(
        "main", prompt_caching=False, provider=OpenAIProvider(api_key="test")
    )

    async def run():
        async with runtime.activate():
            await asyncio.gather(
                *(
                    model._completions_create([], False, {}, ModelRequestParameters())
                    for _ in range(8)
                )
            )
            with pytest.raises(RuntimeError):
                async with request_slot():
                    raise RuntimeError("test")
            async with request_slot():
                pass

    asyncio.run(run())
    assert peak == limit
    assert active == 0


@pytest.mark.parametrize("layout", ["flat", "hierarchical"])
def test_recursive_siblings_overlap_without_mutating_parent(tmp_path, monkeypatch, layout):
    tree = {"父模块": {"components": [], "children": {}}}
    write_tree(tmp_path, tree)
    cfg = config(tmp_path, layout=layout)
    runtime = GenerationRuntime(cfg, str(tmp_path))
    active = peak = 0
    seen = []

    class Agent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt, deps, **kwargs):
            nonlocal active, peak
            async with request_slot():
                active += 1
                peak = max(peak, active)
                seen.append(
                    (
                        deps.current_module_name,
                        list(deps.path_to_current_module),
                        deps.current_depth,
                    )
                )
                await asyncio.sleep(0.02)
                active -= 1
            document = runtime.doc_path(deps.current_module_name, deps.path_to_current_module)
            assert deps.allowed_write_paths == {str(document.resolve())}
            document.parent.mkdir(parents=True, exist_ok=True)
            document.write_text("# " + deps.current_module_name, encoding="utf-8")
            return SimpleNamespace(output="ok")

    monkeypatch.setattr(delegation, "Agent", Agent)
    monkeypatch.setattr(delegation, "create_fallback_models", lambda config: None)
    deps = CodeWikiDeps(
        str(tmp_path),
        str(tmp_path),
        {},
        {},
        ["父模块"],
        "父模块",
        runtime.tree,
        2,
        1,
        cfg,
        generation_runtime=runtime,
    )

    async def run():
        async with runtime.activate():
            return await delegation.generate_sub_module_documentation(
                SimpleNamespace(deps=deps), {"会话": [], "上传": [], "班级": []}
            )

    report = asyncio.run(run())
    assert peak == 2
    assert len(seen) == 3
    assert deps.current_module_name == "父模块"
    assert deps.path_to_current_module == ["父模块"]
    assert deps.current_depth == 1
    assert all(depth == 2 for _, _, depth in seen)
    assert "MISSING" not in report
    assert runtime.pending == set()
    assert (
        len(
            json.loads((tmp_path / "module_tree.json").read_text(encoding="utf-8"))["父模块"][
                "children"
            ]
        )
        == 3
    )


def test_two_roots_delegating_same_name_keep_both_branches(tmp_path, monkeypatch):
    write_tree(tmp_path, {name: {"components": [], "children": {}} for name in ("甲", "乙")})

    class Agent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt, deps, **kwargs):
            if deps.current_depth == 1:
                await delegation.generate_sub_module_documentation(
                    SimpleNamespace(deps=deps), {"会话": []}
                )
            async with request_slot():
                await asyncio.sleep(0.01)
            page = deps.generation_runtime.doc_path(
                deps.current_module_name, deps.path_to_current_module
            )
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("# " + deps.current_module_name, encoding="utf-8")
            return SimpleNamespace(output="ok")

    cfg, be = backend(tmp_path, monkeypatch, Agent)
    runtime = GenerationRuntime(cfg, str(tmp_path))
    be.generation_runtime = runtime

    async def run():
        await asyncio.wait_for(
            asyncio.gather(
                *(be.run_module_agent(name, {}, [], [name], str(tmp_path)) for name in ("甲", "乙"))
            ),
            2,
        )

    asyncio.run(run())
    saved = json.loads((tmp_path / "module_tree.json").read_text(encoding="utf-8"))
    assert list(saved["甲"]["children"]) == ["会话"]
    assert list(saved["乙"]["children"]) == ["乙_会话"]
    assert (tmp_path / "甲" / "会话.md").exists()
    assert (tmp_path / "乙" / "乙_会话.md").exists()
    assert runtime.pending == set()


def test_failure_does_not_mark_partial_page_complete_and_resume_retries(tmp_path, monkeypatch):
    write_tree(tmp_path, {"认证": {"components": [], "children": {}}})
    fail = True
    calls = 0

    class Agent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt, deps, **kwargs):
            nonlocal calls
            calls += 1
            page = deps.generation_runtime.doc_path(
                deps.current_module_name, deps.path_to_current_module
            )
            # First attempt: nothing exists yet. Resume attempt: the failed
            # partial version stayed in place (copy-aside backup, no move).
            if calls == 1:
                assert not page.exists()
            else:
                assert page.read_text(encoding="utf-8") == "partial"
            page.write_text("partial" if fail else "# 完整认证", encoding="utf-8")
            if fail:
                raise RuntimeError("failed after write")
            return SimpleNamespace(output="ok")

    cfg, be = backend(tmp_path, monkeypatch, Agent)
    with pytest.raises(RuntimeError, match="failed after write"):
        asyncio.run(be.run_module_agent("认证", {}, [], ["认证"], str(tmp_path)))
    assert json.loads(
        (tmp_path / "temp" / "incomplete_pages.json").read_text(encoding="utf-8")
    ) == ["认证.md"]
    fail = False
    asyncio.run(be.run_module_agent("认证", {}, [], ["认证"], str(tmp_path)))
    asyncio.run(be.run_module_agent("认证", {}, [], ["认证"], str(tmp_path)))
    assert calls == 2
    assert (tmp_path / "认证.md").read_text(encoding="utf-8") == "# 完整认证"
    assert (tmp_path / "temp" / "incomplete" / "认证.md").read_text() == "partial"


def test_whole_repo_interruption_with_inserted_tree_resumes_root(tmp_path, monkeypatch):
    write_tree(tmp_path, {"子模块": {"components": [], "children": {}}})
    cfg = config(tmp_path, layout="flat")
    runtime = GenerationRuntime(cfg, str(tmp_path))
    runtime.begin(runtime.doc_path("repo", []))
    (tmp_path / "repo.md").write_text("# partial", encoding="utf-8")
    calls = []

    class Agent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt, deps, **kwargs):
            calls.append(deps.current_module_name)
            page = deps.generation_runtime.doc_path(
                deps.current_module_name, deps.path_to_current_module
            )
            page.write_text("# " + deps.current_module_name, encoding="utf-8")
            return SimpleNamespace(output="ok")

    cfg, be = backend(tmp_path, monkeypatch, Agent, layout="flat")
    asyncio.run(DocumentationGenerator(cfg, backend=be).generate_module_documentation({}, []))
    assert calls == ["子模块", "repo"]
    assert (tmp_path / "overview.md").read_text(encoding="utf-8") == "# repo"
    assert be.generation_runtime.pending == set()
    assert not (tmp_path / "repo.md").exists()


def test_pending_detailed_parent_resumes_agent_not_summary(tmp_path, monkeypatch):
    tree = {"详细父页": {"components": [], "children": {"子": {"components": [], "children": {}}}}}
    write_tree(tmp_path, tree)
    cfg = config(tmp_path, layout="flat")
    runtime = GenerationRuntime(cfg, str(tmp_path))
    runtime.begin(tmp_path / "详细父页.md")
    (tmp_path / "详细父页.md").write_text("# unfinished", encoding="utf-8")
    (tmp_path / "子.md").write_text("# 已完成子页", encoding="utf-8")
    calls = []

    class Agent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt, deps, **kwargs):
            calls.append(deps.current_module_name)
            page = deps.generation_runtime.doc_path(
                deps.current_module_name, deps.path_to_current_module
            )
            # The previous unfinished version stays readable until replaced.
            assert page.read_text(encoding="utf-8") == "# unfinished"
            page.write_text("# 详细父页\n保留源码分析深度", encoding="utf-8")
            return SimpleNamespace(output="ok")

    cfg, be = backend(tmp_path, monkeypatch, Agent, layout="flat", max_depth=2)
    be.complete = lambda *args, **kwargs: "<OVERVIEW># 总览</OVERVIEW>"
    asyncio.run(DocumentationGenerator(cfg, backend=be).generate_module_documentation({}, []))
    assert calls == ["详细父页"]
    assert "保留源码分析深度" in (tmp_path / "详细父页.md").read_text(encoding="utf-8")
    assert be.generation_runtime.pending == set()


@pytest.mark.parametrize("layout", ["flat", "hierarchical"])
def test_sanitized_name_collisions_keep_distinct_sources(tmp_path, monkeypatch, layout):
    from codewiki.src.be.dependency_analyzer.models.core import Node

    ids = ["src/a.ts::a", "src/b.ts::b"]
    nodes = {
        cid: Node(
            id=cid,
            name=cid[-1],
            component_type="function",
            file_path=cid.split("::")[0],
            relative_path=cid.split("::")[0],
            source_code="code",
        )
        for cid in ids
    }
    write_tree(tmp_path, {"父": {"components": ids, "children": {}}})
    cfg = config(tmp_path, layout=layout)
    runtime = GenerationRuntime(cfg, str(tmp_path))
    calls = []

    class Agent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt, deps, **kwargs):
            calls.append(deps.current_module_name)
            await asyncio.sleep(0.01)
            page = runtime.doc_path(deps.current_module_name, deps.path_to_current_module)
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("# " + deps.current_module_name, encoding="utf-8")
            return SimpleNamespace(output="ok")

    monkeypatch.setattr(delegation, "Agent", Agent)
    monkeypatch.setattr(delegation, "create_fallback_models", lambda config: None)
    deps = CodeWikiDeps(
        str(tmp_path),
        str(tmp_path),
        {},
        nodes,
        ["父"],
        "父",
        runtime.tree,
        2,
        1,
        cfg,
        generation_runtime=runtime,
    )

    async def run():
        async with runtime.activate():
            await delegation.generate_sub_module_documentation(
                SimpleNamespace(deps=deps), {"a/b": [ids[0]]}
            )
            await delegation.generate_sub_module_documentation(
                SimpleNamespace(deps=deps), {"a/b": [ids[0]], "a?b": [ids[1]]}
            )
            await delegation.generate_sub_module_documentation(
                SimpleNamespace(deps=deps), {"a/b": [ids[0]], "a?b": [ids[1]]}
            )

    asyncio.run(run())
    assert calls == ["a_b", "父_a_b"]
    assert runtime.tree["父"]["children"]["a_b"]["components"] == [ids[0]]
    assert runtime.tree["父"]["children"]["父_a_b"]["components"] == [ids[1]]
    assert runtime.pending == set()


def test_name_collision_with_other_branch_does_not_skip_sources(tmp_path, monkeypatch):
    from codewiki.src.be.dependency_analyzer.models.core import Node

    cid = "src/a.ts::a"
    nodes = {
        cid: Node(
            id=cid,
            name="a",
            component_type="function",
            file_path="src/a.ts",
            relative_path="src/a.ts",
            source_code="code",
        )
    }
    tree = {
        "父": {"components": [], "children": {}},
        "会话": {"components": [], "children": {}},
        "父_会话": {"components": [], "children": {}},
    }
    write_tree(tmp_path, tree)
    cfg = config(tmp_path)
    runtime = GenerationRuntime(cfg, str(tmp_path))
    calls = []

    class Agent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt, deps, **kwargs):
            calls.append(deps.current_module_name)
            page = runtime.doc_path(deps.current_module_name, deps.path_to_current_module)
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("# unique", encoding="utf-8")
            return SimpleNamespace(output="ok")

    monkeypatch.setattr(delegation, "Agent", Agent)
    monkeypatch.setattr(delegation, "create_fallback_models", lambda config: None)
    deps = CodeWikiDeps(
        str(tmp_path),
        str(tmp_path),
        {},
        nodes,
        ["父"],
        "父",
        runtime.tree,
        2,
        1,
        cfg,
        generation_runtime=runtime,
    )

    async def run():
        async with runtime.activate():
            await delegation.generate_sub_module_documentation(
                SimpleNamespace(deps=deps), {"会话": [cid]}
            )

    asyncio.run(run())
    assert calls == ["父_会话_2"]
    assert runtime.tree["父"]["children"]["父_会话_2"]["components"] == [cid]


def test_windows_case_collisions_allocate_distinct_names(monkeypatch):
    from codewiki.src.be import module_naming

    monkeypatch.setattr(module_naming.os.path, "normcase", lambda name: name.lower())
    assert module_naming.resolve_unique_name("Api", "父", {"API", "父_api"}) == "父_Api_2"


@pytest.mark.parametrize("command", ["create", "str_replace", "insert", "undo_edit"])
def test_disk_write_errors_never_report_success(tmp_path, monkeypatch, command):
    from pathlib import Path
    from codewiki.src.be.agent_tools import str_replace_editor as editor

    target = tmp_path / "page.md"
    if command != "create":
        target.write_text("original\n", encoding="utf-8")

    def fail_write(self, *args, **kwargs):
        raise PermissionError("write denied")

    monkeypatch.setattr(Path, "write_text", fail_write)
    tool = editor.EditTool({}, str(tmp_path))
    if command == "undo_edit":
        monkeypatch.setattr(
            editor.EditTool, "_file_history", property(lambda self: {target: ["old content"]})
        )
    tool(
        command=command,
        path=str(target),
        file_text="new",
        old_str="original",
        new_str="new",
        insert_line=0,
    )
    logs = "\n".join(tool.logs)
    assert "write denied" in logs
    assert "successfully" not in logs
    assert "has been edited" not in logs
    if command == "create":
        assert not target.exists()
    else:
        assert target.read_text(encoding="utf-8") == "original\n"


def test_begin_keeps_previous_page_readable_and_backed_up(tmp_path):
    # Rewriting a page must NOT move it away: the IDE/viewer would report the
    # md as missing for the whole rewrite window, and an interruption would
    # leave no file at all. The previous version stays in place and is copied
    # aside as a backup.
    write_tree(tmp_path, {})
    runtime = GenerationRuntime(config(tmp_path), str(tmp_path))
    page = tmp_path / "学员档案.md"
    page.write_text("# 学员档案\n旧版内容", encoding="utf-8")
    runtime.begin(page)
    assert page.read_text(encoding="utf-8") == "# 学员档案\n旧版内容"
    backup = tmp_path / "temp" / "incomplete" / "学员档案.md"
    assert backup.read_text(encoding="utf-8") == "# 学员档案\n旧版内容"
    assert runtime.is_pending(page)


def test_create_overwrites_only_the_whitelisted_rewrite_target(tmp_path):
    from codewiki.src.be.agent_tools import str_replace_editor as editor

    target = tmp_path / "page.md"
    sibling = tmp_path / "other.md"
    target.write_text("old target\n", encoding="utf-8")
    sibling.write_text("old sibling\n", encoding="utf-8")

    allowed = editor.EditTool({}, str(tmp_path), rewrite_paths={str(target)})
    allowed(command="create", path=str(target), file_text="fresh page\n")
    assert target.read_text(encoding="utf-8") == "fresh page\n"
    assert "replaced successfully" in "\n".join(allowed.logs)

    # The same create must not overwrite any other existing page.
    blocked = editor.EditTool({}, str(tmp_path), rewrite_paths={str(target)})
    blocked(command="create", path=str(sibling), file_text="hijack\n")
    assert sibling.read_text(encoding="utf-8") == "old sibling\n"
    assert "Cannot overwrite" in "\n".join(blocked.logs)

    # Without a rewrite allowlist (MCP/manual flows) the strict rule stays.
    strict = editor.EditTool({}, str(tmp_path))
    strict(command="create", path=str(target), file_text="again\n")
    assert target.read_text(encoding="utf-8") == "fresh page\n"
    assert "Cannot overwrite" in "\n".join(strict.logs)


def test_parent_cannot_complete_with_failed_child(tmp_path):
    tree = {"父": {"components": [], "children": {"子": {"components": [], "children": {}}}}}
    write_tree(tmp_path, tree)
    runtime = GenerationRuntime(config(tmp_path), str(tmp_path))
    page = runtime.doc_path("父", ["父"])
    runtime.begin(page)
    page.write_text("# partial parent")
    with pytest.raises(RuntimeError, match="Child documentation incomplete"):
        runtime.finish(page)
    assert runtime.is_pending(page)


def test_independent_failure_preserves_success_and_blocks_parent(tmp_path):
    tree = {
        "父": {
            "components": [],
            "children": {
                "成功": {"components": [], "children": {}},
                "失败": {"components": [], "children": {}},
            },
        }
    }
    write_tree(tmp_path, tree)
    cfg = config(tmp_path, layout="flat", max_depth=2)
    calls = []

    class Backend:
        async def run_module_agent(self, module_name, **kwargs):
            if module_name == "失败":
                raise RuntimeError("failed child")
            await asyncio.sleep(0.01)
            (tmp_path / f"{module_name}.md").write_text("# successful")
            calls.append(module_name)

        def complete(self, *args, **kwargs):
            calls.append("overview")
            return "<OVERVIEW># overview</OVERVIEW>"

    generator = DocumentationGenerator(cfg, backend=Backend())
    with pytest.raises(RuntimeError, match="failed child"):
        asyncio.run(generator.generate_module_documentation({}, []))
    assert calls == ["成功"]
    assert not (tmp_path / "父.md").exists()
    assert not (tmp_path / "overview.md").exists()


def test_parent_waits_and_complete_moves_off_event_loop(tmp_path):
    tree = {
        "父": {
            "components": [],
            "children": {
                "甲": {"components": [], "children": {}},
                "乙": {"components": [], "children": {}},
            },
        }
    }
    write_tree(tmp_path, tree)
    cfg = config(tmp_path, layout="flat", max_depth=2)
    loop_thread = threading.get_ident()
    calls = []

    class Backend:
        async def run_module_agent(self, module_name, **kwargs):
            await asyncio.sleep(0.01)
            (tmp_path / f"{module_name}.md").write_text("# " + module_name)
            calls.append(module_name)

        def complete(self, *args, **kwargs):
            assert threading.get_ident() != loop_thread
            assert {"甲", "乙"} <= set(calls)
            calls.append("overview")
            return "<OVERVIEW># overview</OVERVIEW>"

    asyncio.run(
        DocumentationGenerator(cfg, backend=Backend()).generate_module_documentation({}, [])
    )
    assert calls[:2] == ["甲", "乙"]
    assert calls[2:] == ["overview", "overview"]


def test_clustering_parallel_merges_in_original_order(tmp_path, monkeypatch):
    nodes = {
        str(i): SimpleNamespace(
            relative_path=f"src/{i}.py", source_code="x", component_type="function"
        )
        for i in range(3)
    }
    monkeypatch.setattr(
        clustering,
        "format_potential_core_components",
        lambda ids, *args: ("ids", "code" if len(ids) > 1 else "small"),
    )
    monkeypatch.setattr(clustering, "count_tokens", lambda value: 999999 if value == "code" else 1)
    monkeypatch.setattr(
        clustering, "partition_leaf_nodes_by_structure", lambda *args: [[str(i)] for i in range(3)]
    )
    active = peak = 0
    lock = threading.Lock()

    def batch(ids, *args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03 if ids == ["0"] else 0.01)
        with lock:
            active -= 1
        return {f"模块{ids[0]}": {"components": ids, "path": "src"}}

    monkeypatch.setattr(clustering, "_cluster_via_llm", batch)
    result = clustering.cluster_modules(list(nodes), nodes, config(tmp_path))
    assert peak == 2
    assert list(result) == ["模块0", "模块1", "模块2"]
    assert {cid for info in result.values() for cid in info["components"]} == set(nodes)


def test_recursive_retry_reuses_success_and_reserved_names(tmp_path, monkeypatch):
    write_tree(tmp_path, {"父": {"components": [], "children": {}}})
    cfg = config(tmp_path)
    runtime = GenerationRuntime(cfg, str(tmp_path))
    fail = True
    calls = []

    class Agent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt, deps, **kwargs):
            calls.append(deps.current_module_name)
            page = runtime.doc_path(deps.current_module_name, deps.path_to_current_module)
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("# content", encoding="utf-8")
            if fail and deps.current_module_name == "失败子页":
                raise RuntimeError("child failed")
            return SimpleNamespace(output="ok")

    monkeypatch.setattr(delegation, "Agent", Agent)
    monkeypatch.setattr(delegation, "create_fallback_models", lambda config: None)
    deps = CodeWikiDeps(
        str(tmp_path),
        str(tmp_path),
        {},
        {},
        ["父"],
        "父",
        runtime.tree,
        2,
        1,
        cfg,
        generation_runtime=runtime,
    )

    async def run():
        async with runtime.activate():
            return await delegation.generate_sub_module_documentation(
                SimpleNamespace(deps=deps), {"成功子页": [], "失败子页": []}
            )

    with pytest.raises(RuntimeError, match="child failed"):
        asyncio.run(run())
    fail = False
    asyncio.run(run())
    assert calls.count("成功子页") == 1
    assert calls.count("失败子页") == 2
    assert list(runtime.tree["父"]["children"]) == ["成功子页", "失败子页"]
    assert runtime.pending == set()


def test_same_parent_repeated_delegation_does_not_duplicate(tmp_path, monkeypatch):
    write_tree(tmp_path, {"父": {"components": [], "children": {}}})
    cfg = config(tmp_path)
    runtime = GenerationRuntime(cfg, str(tmp_path))
    calls = []

    class Agent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt, deps, **kwargs):
            calls.append(deps.current_module_name)
            await asyncio.sleep(0.01)
            page = runtime.doc_path(deps.current_module_name, deps.path_to_current_module)
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("# successful", encoding="utf-8")
            return SimpleNamespace(output="ok")

    monkeypatch.setattr(delegation, "Agent", Agent)
    monkeypatch.setattr(delegation, "create_fallback_models", lambda config: None)
    deps = CodeWikiDeps(
        str(tmp_path),
        str(tmp_path),
        {},
        {},
        ["父"],
        "父",
        runtime.tree,
        2,
        1,
        cfg,
        generation_runtime=runtime,
    )

    async def run():
        async with runtime.activate():
            await asyncio.gather(
                *(
                    delegation.generate_sub_module_documentation(
                        SimpleNamespace(deps=deps), {"会话": []}
                    )
                    for _ in range(2)
                )
            )

    asyncio.run(run())
    assert calls == ["会话"]
    assert list(runtime.tree["父"]["children"]) == ["会话"]


def test_mixed_sync_and_async_requests_share_limit(tmp_path):
    from codewiki.src.be.generation_runtime import sync_request

    write_tree(tmp_path, {})
    runtime = GenerationRuntime(config(tmp_path), str(tmp_path))
    lock = threading.Lock()
    active = peak = 0

    def enter():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)

    def leave():
        nonlocal active
        with lock:
            active -= 1

    def blocking():
        enter()
        time.sleep(0.03)
        leave()

    async def asynchronous():
        async with request_slot():
            enter()
            await asyncio.sleep(0.03)
            leave()

    async def run():
        async with runtime.activate():
            await asyncio.gather(
                *(asynchronous() for _ in range(3)),
                *(asyncio.to_thread(sync_request, blocking) for _ in range(3)),
            )

    asyncio.run(run())
    assert peak == 2
    assert active == 0


def test_cancelled_module_is_not_reused_as_success(tmp_path, monkeypatch):
    write_tree(tmp_path, {"认证": {"components": [], "children": {}}})
    started = None

    class Agent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt, deps, **kwargs):
            page = deps.generation_runtime.doc_path(
                deps.current_module_name, deps.path_to_current_module
            )
            page.write_text("# unfinished", encoding="utf-8")
            started.set()
            await asyncio.sleep(10)

    cfg, be = backend(tmp_path, monkeypatch, Agent)

    async def run():
        nonlocal started
        started = asyncio.Event()
        task = asyncio.create_task(be.run_module_agent("认证", {}, [], ["认证"], str(tmp_path)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    runtime = GenerationRuntime(cfg, str(tmp_path))
    assert runtime.is_pending(tmp_path / "认证.md")


def test_serial_and_parallel_preserve_pages_and_calls(tmp_path, monkeypatch):
    tree = {name: {"components": [], "children": {}} for name in ("甲", "乙", "丙")}
    results = []
    for limit in (1, 2):
        directory = tmp_path / str(limit)
        directory.mkdir()
        write_tree(directory, tree)
        calls = []

        class Agent:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, prompt, deps, **kwargs):
                calls.append(deps.current_module_name)
                async with request_slot():
                    await asyncio.sleep(0.01)
                page = deps.generation_runtime.doc_path(
                    deps.current_module_name, deps.path_to_current_module
                )
                page.write_text("# " + deps.current_module_name, encoding="utf-8")
                return SimpleNamespace(output="ok")

        cfg, be = backend(directory, monkeypatch, Agent, concurrency=limit, layout="flat")
        be.complete = lambda *args, **kwargs: "<OVERVIEW># 总览</OVERVIEW>"
        asyncio.run(DocumentationGenerator(cfg, backend=be).generate_module_documentation({}, []))
        results.append(({p.name: p.read_bytes() for p in directory.glob("*.md")}, sorted(calls)))
    assert results[0] == results[1]
    assert results[0][1] == ["丙", "乙", "甲"]


def test_existing_page_without_dependency_evidence_is_not_complete(tmp_path):
    from codewiki.src.be.dependency_analyzer.models.core import Node
    from codewiki.src.be.agent_tools.read_code_components import read_code_components
    from codewiki.src.be.generation_runtime import MissingDocumentationError

    controller = "api/Controller.java::Controller"
    service = "business/Service.java::Service"
    components = {
        cid: Node(
            id=cid,
            name=cid.split("::")[-1],
            component_type="class",
            file_path=cid.split("::")[0],
            relative_path=cid.split("::")[0],
            source_code="class Example {}",
            depends_on=[service] if cid == controller else [],
        )
        for cid in (controller, service)
    }
    write_tree(tmp_path, {"订单": {"components": [controller], "children": {}}})
    runtime = GenerationRuntime(config(tmp_path), str(tmp_path))
    document = tmp_path / "订单.md"
    document.write_text("# 订单\n实现未读取", encoding="utf-8")
    deps = CodeWikiDeps(
        str(tmp_path),
        str(tmp_path),
        {},
        components,
        ["订单"],
        "订单",
        runtime.tree,
        2,
        1,
        runtime.config,
    )
    calls = []

    class Agent:
        async def run(self, prompt, **kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                document.write_text("# 订单\n实现未读取", encoding="utf-8")
            if len(calls) == 2:
                await read_code_components(SimpleNamespace(deps=deps), [service])
                document.write_text("# 订单\n已读取 Service 实现", encoding="utf-8")
            return SimpleNamespace(all_messages=lambda: [])

    runtime.begin(document)
    asyncio.run(runtime.run_page_agent(Agent(), "initial", deps, document))
    assert len(calls) == 2
    assert service in calls[1]
    assert runtime.is_pending(document)
    assert runtime.evidence_current(document, components, ["订单"])
    runtime.finish(document)
    assert not runtime.is_pending(document)
    assert (tmp_path / "temp/incomplete/订单.md").read_text(
        encoding="utf-8"
    ) == "# 订单\n实现未读取"
    reloaded = GenerationRuntime(runtime.config, str(tmp_path))
    assert reloaded.evidence_current(document, components, ["订单"])
    document.write_text("# 修改后的页面", encoding="utf-8")
    assert not reloaded.evidence_current(document, components, ["订单"])
    deps.source_reads.clear()
    runtime.begin(document)

    class IncompleteAgent:
        async def run(self, prompt, **kwargs):
            return SimpleNamespace(all_messages=lambda: [])

    with pytest.raises(MissingDocumentationError):
        asyncio.run(runtime.run_page_agent(IncompleteAgent(), "initial", deps, document))
    assert runtime.is_pending(document)


def test_init_rejects_incomplete_cached_ownership_without_creating_supplements(tmp_path):
    from codewiki.src.be.dependency_analyzer.models.core import Node
    from codewiki.src.be.business_clustering import BusinessClusteringError

    cfg = config(tmp_path, layout="flat")
    cfg.docs_dir = str(tmp_path)
    keys = ["api/a.py::A", "store/b.py::B"]
    components = {
        cid: Node(
            id=cid,
            name=cid.split("::")[-1],
            component_type="class",
            file_path=cid.split("::")[0],
            relative_path=cid.split("::")[0],
            source_code="class Example: pass",
        )
        for cid in keys
    }
    tree = {"原模块": {"components": [keys[0]], "children": {}}}
    write_tree(tmp_path, tree)
    (tmp_path / "first_module_tree.json").write_text(
        json.dumps(tree, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "原模块.md").write_text("# 原模块", encoding="utf-8")
    (tmp_path / "overview.md").write_text("# 旧总览", encoding="utf-8")

    class Backend:
        generation_runtime = None

        async def run_module_agent(self, module_name, module_path, **kwargs):
            runtime = self.generation_runtime
            document = runtime.doc_path(module_name, module_path)
            if not document.exists():
                runtime.begin(document)
                document.write_text(f"# {module_name}", encoding="utf-8")
                runtime.finish(document)
            return runtime.tree

        def complete(self, prompt, **kwargs):
            return "<OVERVIEW># 更新总览</OVERVIEW>"

    generator = DocumentationGenerator(cfg, backend=Backend())
    generator.graph_builder = SimpleNamespace(build_dependency_graph=lambda: (components, keys))
    with pytest.raises(BusinessClusteringError, match="Clean generated artifacts"):
        asyncio.run(generator.run())
    assert json.loads((tmp_path / "module_tree.json").read_text(encoding="utf-8")) == tree
    assert (tmp_path / "原模块.md").read_text(encoding="utf-8") == "# 原模块"
    assert (tmp_path / "overview.md").read_text(encoding="utf-8") == "# 旧总览"
    assert not any("补充分析" in path.name for path in tmp_path.iterdir())
