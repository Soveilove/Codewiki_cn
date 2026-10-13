"""PydanticAIBackend — the existing API-key based path.

This backend is a thin adapter over :func:`call_llm` and the pydantic-ai
``Agent`` machinery.  Behaviour is preserved exactly; this file only
repackages it behind the :class:`LLMBackend` interface so the rest of
CodeWiki can be backend-agnostic.
"""

from __future__ import annotations

import logging
import os
import time
import traceback
from contextvars import ContextVar
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.agent_tools.generate_sub_module_documentations import (
    generate_sub_module_documentation_tool,
)
from codewiki.src.be.agent_tools.read_code_components import read_code_components_tool
from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor_tool
from codewiki.src.be.backend import AgentReply, LLMBackend, usage_to_dict
from codewiki.src.be.cluster_modules import format_potential_core_components
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.llm_services import call_llm, create_fallback_models, pop_last_usage
from codewiki.src.be.prompt_template import (
    format_leaf_system_prompt,
    format_system_prompt,
    format_user_prompt,
)
from codewiki.src.be.utils import count_tokens, is_complex_module
from codewiki.src.be.doc_layout import config_layout, find_doc
from codewiki.src.be.trace import mark_skipped, traced
from codewiki.src.be.generation_runtime import GenerationRuntime
from codewiki.src.config import OVERVIEW_FILENAME, Config

logger = logging.getLogger(__name__)


def _run_usage(result: Any) -> dict[str, Any] | None:
    """Token usage of a pydantic-ai run; ``usage`` is a property in pydantic-ai
    2.x and a method in earlier releases."""
    try:
        usage = getattr(result, "usage", None)
        if callable(usage):
            usage = usage()
        return usage_to_dict(usage)
    except Exception:  # noqa: BLE001 — usage is optional telemetry
        return None


class PydanticAIBackend(LLMBackend):
    """API-key based backend using pydantic-ai + openai/litellm clients."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._fallback_models = create_fallback_models(config)
        self._custom_instructions = config.get_prompt_addition()
        self._usage: ContextVar[dict | None] = ContextVar("backend_usage", default=None)

    @property
    def last_usage(self):
        return self._usage.get()

    @last_usage.setter
    def last_usage(self, value):
        self._usage.set(value)

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> str:
        pop_last_usage()
        result = call_llm(prompt, self._config, model=model, system_prompt=system_prompt)
        self.last_usage = pop_last_usage()
        return result or ""

    async def run_update_agent(
        self,
        system_prompt: str,
        user_prompt: str,
        deps: CodeWikiDeps,
    ) -> AgentReply:
        agent = Agent(
            self._fallback_models,
            name=f"update:{deps.current_module_name}",
            deps_type=CodeWikiDeps,
            tools=[read_code_components_tool, str_replace_editor_tool],
            system_prompt=system_prompt,
            retries=self._config.agent_retries,
        )
        started = time.time()
        result = await agent.run(
            user_prompt,
            deps=deps,
            usage_limits=UsageLimits(request_limit=self._config.request_limit),
        )
        seconds = time.time() - started
        usage = _run_usage(result)
        self.last_usage = usage
        text = str(result.output)
        return AgentReply(text=text, usage=usage, seconds=seconds)

    @traced("module_agent", "module", fields=("module_name", "module_path"))
    async def run_module_agent(
        self,
        module_name: str,
        components: dict[str, Node],
        core_component_ids: list[str],
        module_path: list[str],
        working_dir: str,
    ) -> dict[str, Any]:
        config = self._config
        runtime = getattr(self, "generation_runtime", None)
        if runtime is None:
            runtime = GenerationRuntime(config, working_dir)
            # Init binds a business-aware runtime before any agent runs; the
            # update path reaches here without one, so inherit the backend
            # flag rather than silently producing old-style pages.
            runtime.business_analysis = getattr(self, "business_analysis", False)
        module_tree = runtime.tree
        expected_doc = runtime.doc_path(module_name, module_path)

        # overview.md is the root module's own doc (renamed from
        # {repo_name}.md), so its presence only proves the root is done —
        # nested modules must still be checked against their own doc file,
        # or a resume after a partial run silently skips every missing one.
        if not module_path:
            overview_docs_path = os.path.join(working_dir, OVERVIEW_FILENAME)
            if os.path.exists(overview_docs_path) and not runtime.is_pending(expected_doc):
                logger.info("✓ Overview docs already exists at %s", overview_docs_path)
                mark_skipped("已有文档")
                return module_tree
        docs_path = find_doc(working_dir, module_name, module_tree) if module_path else None
        if docs_path is None:
            docs_path = os.path.join(working_dir, f"{module_name}.md")
        if os.path.exists(docs_path) and not runtime.is_pending(expected_doc):
            if not runtime.evidence_current(expected_doc, components, module_path):
                logger.warning("旧页面缺少有效源码分析证据，将备份并补分析：%s", module_name)
                runtime.invalidate_summaries(module_path)
                runtime.begin(expected_doc)
            else:
                logger.info("✓ Module docs already exists at %s", docs_path)
                mark_skipped("已有文档")
                return module_tree

        # Sub-module delegation is only possible when this run can still go one
        # level deeper. The tool must be removed from the agent's tool list as
        # well as from the prompt: pydantic-ai sends every registered tool's
        # schema to the model regardless of what the prompt says, so leaving it
        # registered lets the model call it and spawn a whole extra agent
        # session even though the depth guard then only downgrades it.
        # The gate matches CawBackend so both subscription and API paths
        # decompose on the same conditions.
        _, potential_code = format_potential_core_components(core_component_ids, components)
        can_delegate = (
            is_complex_module(components, core_component_ids)
            and max(1, len(module_path)) < config.max_depth
            and count_tokens(potential_code) >= config.max_token_per_leaf_module
        )
        deps = CodeWikiDeps(
            absolute_docs_path=working_dir,
            absolute_repo_path=str(os.path.abspath(config.repo_path)),
            registry={},
            components=components,
            path_to_current_module=list(module_path),
            current_module_name=module_name,
            module_tree=module_tree,
            max_depth=config.max_depth,
            current_depth=max(1, len(module_path)),
            config=config,
            custom_instructions=self._custom_instructions,
            generation_runtime=runtime,
            allowed_write_paths={str(expected_doc.resolve())},
            business_analysis=runtime.business_analysis,
        )

        # The user prompt is built first because it decides whether any source
        # still has to be read: only after it do we know whether the agent needs
        # a source-exploration step at all. Getting this order wrong was
        # measured as ten requests and 30k tokens of history for a module that
        # needed one write, because the default workflow told the model to
        # re-read source the prompt already contained in full.
        user_prompt = format_user_prompt(
            module_name=module_name,
            core_component_ids=core_component_ids,
            components=components,
            module_tree=deps.module_tree,
            module_path=module_path,
            layout=config_layout(config),
            business_analysis=deps.business_analysis,
            required_source_ids=deps.required_source_ids,
            prompt_read_ids=deps.source_reads,
            deferred_source_ids=deps.deferred_source_ids,
            can_delegate=can_delegate,
        )
        direct_write = (
            deps.business_analysis
            and bool(core_component_ids)
            and not deps.required_source_ids
            and not deps.deferred_source_ids
        )
        tools = [read_code_components_tool, str_replace_editor_tool]
        if can_delegate:
            tools.append(generate_sub_module_documentation_tool)
        if is_complex_module(components, core_component_ids):
            agent = Agent(
                self._fallback_models,
                name=module_name,
                deps_type=CodeWikiDeps,
                tools=tools,
                system_prompt=format_system_prompt(
                    module_name,
                    self._custom_instructions,
                    module_path,
                    config_layout(config),
                    can_delegate=can_delegate,
                    direct_write=direct_write,
                ),
                retries=config.agent_retries,
            )
        else:
            agent = Agent(
                self._fallback_models,
                name=module_name,
                deps_type=CodeWikiDeps,
                tools=[read_code_components_tool, str_replace_editor_tool],
                system_prompt=format_leaf_system_prompt(
                    module_name,
                    self._custom_instructions,
                    module_path,
                    config_layout(config),
                    direct_write=direct_write,
                ),
                retries=config.agent_retries,
            )

        try:
            runtime.begin(expected_doc)
            async with runtime.activate():
                result = await runtime.run_page_agent(
                    agent,
                    user_prompt,
                    deps,
                    expected_doc,
                )
            self.last_usage = _run_usage(result)
            runtime.finish(expected_doc)
            return deps.module_tree
        except Exception as e:
            logger.error("Error processing module %s: %s", module_name, e)
            logger.error("Traceback: %s", traceback.format_exc())
            raise
