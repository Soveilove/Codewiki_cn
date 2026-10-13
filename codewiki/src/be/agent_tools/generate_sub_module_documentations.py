import asyncio
from copy import copy
from pathlib import Path

from pydantic_ai import RunContext, Tool, Agent, ModelRetry
from pydantic_ai.usage import UsageLimits

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.doc_layout import config_layout, module_doc_file
from codewiki.src.be.module_naming import (
    RESERVED_STEMS,
    SubModulePlan,
    collect_module_tree_names,
    resolve_unique_name,
    skipped_report,
    plan_sub_module_specs,
    sub_module_report,
    sanitize_module_name,
    strip_parent_prefix,
    subtopic_name_variants,
)
from codewiki.src.be.agent_tools.read_code_components import read_code_components_tool
from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor_tool
from codewiki.src.be.llm_services import create_fallback_models
from codewiki.src.be.trace import trace_span
from codewiki.src.be.generation_runtime import generation_concurrency, run_all
from codewiki.src.be.prompt_template import (
    format_leaf_system_prompt,
    format_system_prompt,
    format_user_prompt,
)
from codewiki.src.be.utils import is_complex_module, count_tokens
from codewiki.src.be.cluster_modules import (
    format_potential_core_components,
    resolve_component_ids,
)

import logging

logger = logging.getLogger(__name__)


async def generate_sub_module_documentation(
    ctx: RunContext[CodeWikiDeps], sub_module_specs: dict[str, list[str]]
) -> str:
    """Generate complete sub-module pages with separate agents and wait for their files.

    Args:
        sub_module_specs: Map each descriptive sub-module name to a list of exact
            component IDs from the supplied source graph, e.g.
            {"authentication": ["src/auth.ts::login", "src/auth.ts::logout"]}.
            Do not invent IDs or pass prose descriptions. Names are reserved across
            the wiki; collisions are renamed and completed pages are reused. The
            result reports the actual page paths for parent references.
    """
    runtime = getattr(ctx.deps, "generation_runtime", None)
    if runtime is None:
        return await _generate_sub_module_documentation(ctx, sub_module_specs)
    key = tuple(ctx.deps.path_to_current_module)
    lock = runtime.delegation_locks.setdefault(key, asyncio.Lock())
    async with lock:
        return await _generate_sub_module_documentation(ctx, sub_module_specs)


def _resolve_sub_topic_ownership(children: dict, specs: dict[str, list[str]]) -> None:
    """Give each component one primary sub-topic and mark the rest as references.

    A parent agent splitting its scope may hand the same model or converter to
    several sub-topics; that is a legitimate statement about shared use, not a
    reason for every sub-page to explain it in full. The first sub-topic keeps
    it, the rest record it under ``shared_components`` and are told to link
    back instead of repeating the explanation.
    """
    owner: dict[str, str] = {}
    also_used_by: dict[str, set[str]] = {}
    for name in specs:
        node = children.get(name)
        if node is None:
            continue
        owned, shared = [], []
        for cid in specs[name]:
            if cid in owner:
                shared.append(cid)
                also_used_by.setdefault(owner[cid], set()).add(name)
            else:
                owner[cid] = name
                owned.append(cid)
        node["components"] = owned
        if shared:
            node["shared_components"] = sorted(set(shared))
    for name, users in also_used_by.items():
        node = children.get(name)
        if node is None:
            continue
        pairs = set()
        for other in users:
            for cid in specs.get(other, []):
                if owner.get(cid) == name:
                    pairs.add((cid, other))
        node["components_shared_with"] = sorted(pairs)
    moved = sum(len(children[n].get("shared_components", [])) for n in specs if n in children)
    if moved:
        logger.info(
            "递归拆分：%d 个共享组件只保留在第一个子主题中，其余子主题改为引用。",
            moved,
        )


async def _generate_sub_module_documentation(
    ctx: RunContext[CodeWikiDeps], sub_module_specs: dict[str, list[str]]
) -> str:
    """Delegate documentation generation of sub-modules to sub-agents. Each sub-module will be documented separately.

    Args:
        sub_module_specs: A dictionary mapping sub-module names to their core component IDs.
            Example: {"authentication": ["auth_handler.py::AuthHandler", "auth_middleware.py::verify_token"], "database": ["db_client.py::DBClient", "models.py::UserModel"]}
            Each key is a descriptive sub-module name, and the value is a list of component IDs from the current module's core components that belong to that sub-module.
            Sub-module names must be unique across the whole wiki. A name already used by another module is
            prefixed with the current module name; a name that is already documented (plain or prefixed)
            is SKIPPED, not regenerated. The tool result reports the final file names actually saved and
            every skipped request.
    """

    deps = ctx.deps
    previous_module_name = deps.current_module_name
    try:
        canonical_specs = {
            name: resolve_component_ids(ids, deps.components, strict=True)
            for name, ids in sub_module_specs.items()
        }
    except ValueError as exc:
        raise ModelRetry(
            str(exc) + ". Use exact file_path::symbol IDs from the supplied components; "
            "do not create a sub-module with unresolved components."
        ) from exc

    # Create fallback models from config
    fallback_models = create_fallback_models(deps.config)

    # Resolve name collisions against the module tree and files already on disk
    # before touching the tree (issue #76): names are unique across the wiki.
    # A request that is already documented (plain or parent-prefixed name) is
    # skipped rather than renamed x_2, x_3, ... (issue #113).
    runtime = getattr(deps, "generation_runtime", None)
    plan = (
        SubModulePlan()
        if runtime is not None
        else plan_sub_module_specs(
            sub_module_specs,
            previous_module_name,
            deps.module_tree,
            deps.absolute_docs_path,
        )
    )
    name_map = plan.name_map
    layout = config_layout(deps.config)
    parent_path = list(deps.path_to_current_module)
    parent_doc = module_doc_file(previous_module_name, parent_path, layout)
    value = deps.module_tree
    for key in parent_path:
        value = value[key]["children"]
    if runtime is not None:
        from codewiki.src.be.doc_layout import list_doc_files

        taken = (
            collect_module_tree_names(deps.module_tree)
            | set(list_doc_files(deps.absolute_docs_path))
            | RESERVED_STEMS
        )
        name_map.clear()
        plan.skipped.clear()
        for requested_name, canonical in canonical_specs.items():
            # Recognize the same sub-topic whether the model named it plain or
            # parent-prefixed, across runs. Exact component-set equality is the
            # strongest match; a unique same-name child still reuses the node
            # (its components are reconciled below by ownership resolution), so
            # a slightly different re-grouping cannot create duplicate pages.
            variants = subtopic_name_variants(requested_name, previous_module_name, list(value))
            existing = next(
                (
                    key
                    for key in variants
                    if key in value and set(value[key].get("components", [])) == set(canonical)
                ),
                None,
            )
            if existing is None:
                # Cross-run / naming-style reuse: one same-named child whose
                # scope substantially overlaps the new allocation. Without the
                # overlap gate, a DIFFERENT topic that merely sanitizes to the
                # same stem would be swallowed into the old page.
                same_name = [key for key in variants if key in value]
                if len(same_name) == 1:
                    existing_components = set(value[same_name[0]].get("components") or [])
                    canonical_set = set(canonical)
                    if canonical_set and existing_components:
                        smaller = min(len(canonical_set), len(existing_components))
                        overlap = len(canonical_set & existing_components) / smaller
                        if overlap >= 0.5:
                            existing = same_name[0]
            if existing is not None:
                document = runtime.doc_path(existing, parent_path + [existing])
                if (
                    document.is_file()
                    and document.read_text(encoding="utf-8").strip()
                    and not runtime.is_pending(document)
                    and runtime.evidence_current(
                        document, deps.components, parent_path + [existing]
                    )
                ):
                    plan.skipped[requested_name] = f"already documented as {existing}.md"
                else:
                    if document.is_file() and not runtime.is_pending(document):
                        runtime.invalidate_summaries(parent_path + [existing])
                        runtime.begin(document)
                    name_map[requested_name] = existing
            else:
                # Page stems are indexed wiki-wide (find_doc / list_doc_files /
                # the reference index), so a colliding sub-topic name must be
                # made globally unique — the parent prefix is load-bearing.
                base = strip_parent_prefix(
                    sanitize_module_name(requested_name), previous_module_name
                )
                final_name = resolve_unique_name(base, previous_module_name, taken)
                taken.add(final_name)
                name_map[requested_name] = final_name
                value[final_name] = {
                    "components": canonical,
                    "children": {},
                    "purpose": f"{previous_module_name} 的子主题：{requested_name}",
                }
    if not name_map:
        return skipped_report(plan.skipped, parent_doc)
    final_specs = {
        name_map[requested_name]: core_component_ids
        for requested_name, core_component_ids in canonical_specs.items()
        if requested_name in name_map
    }
    for sub_module_name, core_component_ids in final_specs.items():
        node = value.setdefault(
            sub_module_name,
            {
                "components": core_component_ids,
                "children": {},
                "purpose": f"{previous_module_name} 的子主题：{sub_module_name}",
            },
        )
        # A sub-topic created in an earlier run has no purpose; give it the
        # parent's context so its page is not written as a file listing.
        if not (node.get("purpose") or "").strip():
            node["purpose"] = f"{previous_module_name} 的子主题：{sub_module_name}"

    if runtime is not None:
        runtime.checkpoint()

    _resolve_sub_topic_ownership(value, final_specs)

    async def generate_one(sub_module_name, core_component_ids):
        # Create visual indentation for nested modules
        indent = "  " * deps.current_depth
        arrow = "└─" if deps.current_depth > 0 else "→"

        logger.info(f"{indent}{arrow} Generating documentation for sub-module: {sub_module_name}")

        num_tokens = count_tokens(
            format_potential_core_components(core_component_ids, ctx.deps.components)[-1]
        )

        child_path = parent_path + [sub_module_name]
        document = Path(deps.absolute_docs_path) / module_doc_file(
            sub_module_name, child_path, layout
        )
        child_deps = copy(deps)
        child_deps.current_module_name = sub_module_name
        child_deps.path_to_current_module = child_path
        child_deps.current_depth = deps.current_depth + 1
        child_deps.registry = {}
        child_deps.source_reads = set()
        child_deps.source_files_read = set()
        child_deps.source_files_missing = set()
        child_deps.unresolved_components = set()
        child_deps.required_source_ids = set()
        child_deps.deferred_source_ids = set()
        child_deps.allowed_write_paths = {str(document.resolve())} if runtime else None

        # Build the user prompt before the agent: its out-params (still-missing
        # assigned files, deferred dependencies) decide whether the system
        # prompt may promise direct write or must keep the explore workflow.
        prompt = format_user_prompt(
            module_name=sub_module_name,
            core_component_ids=core_component_ids,
            components=deps.components,
            module_tree=deps.module_tree,
            module_path=child_path,
            layout=layout,
            business_analysis=getattr(child_deps, "business_analysis", False),
            required_source_ids=child_deps.required_source_ids,
            prompt_read_ids=child_deps.source_reads,
            deferred_source_ids=child_deps.deferred_source_ids,
            # A sub-module sits at the deepest level the tree budget allows,
            # so it never splits further.
            can_delegate=False,
        )
        child_direct_write = (
            bool(getattr(child_deps, "business_analysis", False))
            and bool(core_component_ids)
            and not child_deps.required_source_ids
            and not child_deps.deferred_source_ids
        )

        if (
            is_complex_module(ctx.deps.components, core_component_ids)
            and ctx.deps.current_depth < ctx.deps.max_depth
            and num_tokens >= ctx.deps.config.max_token_per_leaf_module * ctx.deps.current_depth
        ):
            # A sub-module agent may only delegate further while depth remains.
            # The tool must be unregistered, not merely undocumented: pydantic-ai
            # sends every registered tool's schema to the model regardless of the
            # prompt, and a delegation call always spawns a fresh agent session.
            child_tools = [read_code_components_tool, str_replace_editor_tool]
            if ctx.deps.current_depth + 1 < ctx.deps.max_depth:
                child_tools.append(generate_sub_module_documentation_tool)
            sub_agent = Agent(
                model=fallback_models,
                name=sub_module_name,
                deps_type=CodeWikiDeps,
                system_prompt=format_system_prompt(
                    sub_module_name,
                    ctx.deps.custom_instructions,
                    parent_path + [sub_module_name],
                    layout,
                    can_delegate=len(child_tools) == 3,
                    direct_write=child_direct_write,
                ),
                retries=ctx.deps.config.agent_retries,
                tools=child_tools,
            )
        else:
            sub_agent = Agent(
                model=fallback_models,
                name=sub_module_name,
                deps_type=CodeWikiDeps,
                system_prompt=format_leaf_system_prompt(
                    sub_module_name,
                    ctx.deps.custom_instructions,
                    parent_path + [sub_module_name],
                    layout,
                    direct_write=child_direct_write,
                ),
                retries=ctx.deps.config.agent_retries,
                tools=[read_code_components_tool, str_replace_editor_tool],
            )

        if runtime is not None:
            runtime.begin(document)
        with trace_span(
            "sub_module", "module", module_name=sub_module_name, module_path=child_path
        ):
            if runtime is not None:
                await runtime.run_page_agent(sub_agent, prompt, child_deps, document)
                runtime.finish(document)
                deps.source_reads.update(child_deps.source_reads)
                deps.source_files_read.update(child_deps.source_files_read)
            else:
                await sub_agent.run(
                    prompt,
                    deps=child_deps,
                    usage_limits=UsageLimits(request_limit=deps.config.request_limit),
                )

    if runtime is not None and generation_concurrency(deps.config) > 1:
        await run_all(generate_one(name, ids) for name, ids in final_specs.items())
    else:
        for name, ids in final_specs.items():
            await generate_one(name, ids)

    return sub_module_report(
        name_map,
        plan.skipped,
        deps.absolute_docs_path,
        deps.module_tree,
        previous_module_name,
        parent_path,
        layout,
    )


generate_sub_module_documentation_tool = Tool(
    function=generate_sub_module_documentation,
    name="generate_sub_module_documentation",
    takes_ctx=True,
)
