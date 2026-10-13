import asyncio
import json
import logging
import os
import re
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from codewiki import __version__
from codewiki.src.be.backend import LLMBackend, get_backend
from codewiki.src.be.cluster_modules import (
    collect_module_tree_component_ids,
    get_clustering_input_token_count,
)
from codewiki.src.be.business_clustering import (
    BusinessClusteringError,
    cluster_business_modules,
    select_business_components,
)
from codewiki.src.be.dependency_analyzer import DependencyGraphBuilder
from codewiki.src.be.dependency_analyzer.analyzers.artifact import render_artifact_index
from codewiki.src.be.doc_layout import (
    config_layout,
    list_doc_files,
    module_doc_relpath,
    organize_docs,
    relative_link,
)
from codewiki.src.be.module_naming import (
    dedupe_module_tree_names,
    find_missing_module_docs,
    resolve_module_doc_path,
)
from codewiki.src.be.prompt_template import (
    MODULE_OVERVIEW_PROMPT,
    REPO_OVERVIEW_ARTIFACT_ADDENDUM,
    REPO_OVERVIEW_PROMPT,
    format_overview_system_prompt,
)
from codewiki.src.be.wiki_doctor import doctor_wiki
from codewiki.src.config import (
    FIRST_MODULE_TREE_FILENAME,
    MODULE_TREE_FILENAME,
    OVERVIEW_FILENAME,
    Config,
)
from codewiki.src.utils import file_manager
from codewiki.src.be.trace import mark_skipped, trace_generation, trace_span, traced
from codewiki.src.be.generation_runtime import GenerationRuntime, generation_concurrency, run_all

# Configure logging and monitoring
logger = logging.getLogger(__name__)
_PROMPT_HASH_UNSET = object()


def _parse_overview_response(response: str, module_name: str) -> str:
    content = response.strip()
    envelope = re.search(r"<OVERVIEW>\s*(.*?)\s*</OVERVIEW>", content, re.DOTALL)
    if envelope:
        content = envelope.group(1).strip()
    elif "<OVERVIEW>" in content or "</OVERVIEW>" in content:
        raise RuntimeError(
            f"Incomplete <OVERVIEW> wrapper for {module_name}; "
            "response may be truncated, documentation was not saved."
        )

    fence = re.fullmatch(
        r"(?P<fence>`{3,}|~{3,})(?:markdown|md)?[ \t]*\n(?P<body>.*)\n(?P=fence)[ \t]*",
        content,
        re.DOTALL | re.IGNORECASE,
    )
    if fence:
        content = fence.group("body").strip()
    if not content:
        raise RuntimeError(f"LLM returned empty content for {module_name} overview")
    if not envelope:
        if re.match(r"#{1,6}\s+\S", content):
            logger.debug("Overview for %s returned Markdown without transport wrapper", module_name)
        else:
            logger.warning(
                "Overview response for %s has no wrapper or leading Markdown heading; "
                "saving non-empty response, review the generated page.",
                module_name,
            )
    return content


class IncompleteDocumentationError(Exception):
    """Raised when generation finishes but some modules have no doc file on disk."""

    def __init__(self, missing_modules: list[str]):
        self.missing_modules = missing_modules
        super().__init__(
            f"Documentation generation finished but {len(missing_modules)} module doc(s) "
            f"are missing: {', '.join(missing_modules)}"
        )


def _tree_depth(tree):
    """Deepest module level in the tree (1 for a flat tree)."""
    depth = 0
    for info in (tree or {}).values():
        depth = max(depth, 1 + _tree_depth(info.get("children") or {}))
    return depth


def _tree_doc_paths(tree, layout):
    """Docs-relative page path of every module currently in the tree."""
    paths = set()

    def walk(node, path):
        for name, info in (node or {}).items():
            here = path + [name]
            paths.add(module_doc_relpath(here, layout))
            walk(info.get("children") or {}, here)

    walk(tree, [])
    return paths


def _all_descendant_components(children):
    """Every component id below ``children``, regardless of level."""
    collected: list[str] = []
    stack = [children]
    while stack:
        current = stack.pop()
        for info in current.values():
            collected.extend(info.get("components") or [])
            kids = info.get("children") or {}
            if kids:
                stack.append(kids)
    return collected


def _collapse_tree(tree, max_depth, layout):
    """Merge every branch deeper than ``max_depth`` into its parent.

    The parent gains the descendants' components so its single page can carry
    that material itself, and loses ``children`` so the branch is not walked
    again. Returns the doc paths of the pages that must be rewritten.
    """
    rewritten: set[str] = set()

    def walk(node, depth, parent_path):
        for name, info in node.items():
            path = parent_path + [name]
            children = info.get("children") or {}
            if not children:
                continue
            if depth >= max_depth:
                merged = list(info.get("components") or [])
                seen = set(merged)
                for cid in _all_descendant_components(children):
                    if cid not in seen:
                        seen.add(cid)
                        merged.append(cid)
                info["components"] = merged
                info["children"] = {}
                rewritten.add(module_doc_relpath(path, layout))
            else:
                walk(children, depth + 1, path)

    walk(tree, 1, [])
    return rewritten


class DocumentationGenerator:
    """Main documentation generation orchestrator."""

    def __init__(
        self, config: Config, commit_id: str | None = None, backend: LLMBackend | None = None
    ):
        self.config = config
        self.commit_id = commit_id
        self.graph_builder = DependencyGraphBuilder(config)
        self.backend: LLMBackend = backend or get_backend(config)
        self.business_analysis = False
        # Declared up front: set later by run()/​_generate_parallel_modules
        # once analysis and the runtime exist.
        self.analysis_components: dict[str, Any] = {}
        self.analysis_leaf_nodes: list[str] = []
        self.generation_runtime: GenerationRuntime | None = None

    def create_documentation_metadata(
        self,
        working_dir: str,
        components: dict[str, Any],
        num_leaf_nodes: int,
        prompt_asset_hash: str | None | object = _PROMPT_HASH_UNSET,
    ):
        """Create a metadata file with documentation generation information."""
        from datetime import UTC, datetime

        from codewiki.src.prompt_assets import (
            FINGERPRINT_KEY,
            prompt_asset_fingerprint,
        )

        if prompt_asset_hash is _PROMPT_HASH_UNSET:
            prompt_asset_hash = prompt_asset_fingerprint(self.config.repo_path)

        metadata: dict[str, Any] = {
            "generation_info": {
                "timestamp": datetime.now(UTC).isoformat(),
                "main_model": self.config.main_model,
                "generator_version": __version__,
                "repo_path": self.config.repo_path,
                "commit_id": self.commit_id,
                # Read back by `--update` so updated pages keep this language
                "language": getattr(self.config, "language", None),
                # Read back by `--update` and the viewers to locate pages
                "layout": config_layout(self.config),
                # Fingerprint of <repo>/.codewiki/prompts.md; `--update`
                # compares it to detect team-convention changes. An explicit
                # value wins (the update path keeps the stored fingerprint so
                # the warning persists until a full rebuild).
                FINGERPRINT_KEY: prompt_asset_hash,
            },
            "statistics": {
                "total_components": len(components),
                "leaf_nodes": num_leaf_nodes,
                "max_depth": self.config.max_depth,
            },
            "files_generated": ["overview.md", "module_tree.json", "first_module_tree.json"],
        }

        # Add generated markdown files (docs-relative paths) to the metadata
        try:
            for file_path in sorted(list_doc_files(working_dir).values()):
                if file_path not in metadata["files_generated"]:
                    metadata["files_generated"].append(file_path)
        except Exception as e:  # noqa: BLE001 — metadata listing is best-effort
            logger.warning(f"Could not list generated files: {e}")

        metadata_path = os.path.join(working_dir, "metadata.json")
        file_manager.save_json(metadata, metadata_path)

    @staticmethod
    def get_processing_order(
        module_tree: dict[str, Any], parent_path: list[str] | None = None
    ) -> list[tuple[list[str], str]]:
        """Get the processing order using topological sort (leaf modules first)."""
        parent_path = parent_path or []
        processing_order = []

        def collect_modules(tree: dict[str, Any], path: list[str]):
            for module_name, module_info in tree.items():
                current_path = path + [module_name]

                # If this module has children, process them first
                if (
                    module_info.get("children")
                    and isinstance(module_info["children"], dict)
                    and module_info["children"]
                ):
                    collect_modules(module_info["children"], current_path)
                    # Add this parent module after its children
                    processing_order.append((current_path, module_name))
                else:
                    # This is a leaf module, add it immediately
                    processing_order.append((current_path, module_name))

        collect_modules(module_tree, parent_path)
        return processing_order

    @staticmethod
    def is_leaf_module(module_info: dict[str, Any]) -> bool:
        """Check if a module is a leaf module (has no children or empty children)."""
        children = module_info.get("children", {})
        return not children or (isinstance(children, dict) and len(children) == 0)

    def build_overview_structure(
        self, module_tree: dict[str, Any], module_path: list[str], working_dir: str
    ) -> dict[str, Any]:
        """Build structure for overview generation with 1-depth children doc paths and target indicator.

        Children docs are referenced by absolute file path (``docs_path``)
        rather than inlined, and ``components`` lists are stripped: inlining
        the full tree plus docs blew past provider input caps (codex rejects
        turns over 1,048,576 chars) on large repos. The overview agent reads
        the referenced files itself.
        """

        processed_module_tree = deepcopy(module_tree)
        self._strip_components(processed_module_tree)
        module_info = processed_module_tree
        for path_part in module_path:
            module_info = module_info[path_part]
            if path_part != module_path[-1]:
                module_info = module_info.get("children", {})
            else:
                module_info["is_target_for_overview_generation"] = True

        if "children" in module_info:
            module_info = module_info["children"]

        layout = config_layout(self.config)
        page_rel = module_doc_relpath(module_path, layout)
        for child_name, child_info in module_info.items():
            child_rel = module_doc_relpath(module_path + [child_name], layout)
            child_docs_path = self._resolve_child_docs_path(working_dir, child_name, module_tree)
            if child_docs_path is not None:
                child_info["docs_path"] = child_docs_path
                child_info["link"] = relative_link(page_rel, child_rel)
            else:
                logger.warning(f"Module docs not found at {os.path.join(working_dir, child_rel)}")
                child_info["docs_path"] = None

        return processed_module_tree

    @classmethod
    def _strip_components(cls, tree: dict[str, Any]) -> None:
        """Recursively drop ``components`` lists — they dominate the tree's
        serialized size and add nothing to an overview prompt."""
        for module_info in tree.values():
            if not isinstance(module_info, dict):
                continue
            module_info.pop("components", None)
            children = module_info.get("children")
            if isinstance(children, dict):
                cls._strip_components(children)

    @staticmethod
    def _resolve_child_docs_path(
        working_dir: str, child_name: str, module_tree: dict[str, Any] | None = None
    ) -> str | None:
        """Resolve the on-disk path for a child module's .md doc.

        Sub-agents sometimes save files under a sanitized variant of the
        module name (spaces → underscores, lowercased, etc.) rather than the
        exact key in the module tree. Try a small set of common variants
        before giving up so the overview prompt still gets the children's
        content as context.
        """
        return resolve_module_doc_path(working_dir, child_name, module_tree)

    def validate_generated_docs(self, working_dir: str) -> list[str]:
        """Check the final module tree against the docs on disk.

        Returns the names of modules whose .md file is missing (plus
        "overview" if overview.md was never written).
        """
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        if not os.path.exists(module_tree_path):
            return []
        module_tree = file_manager.load_json(module_tree_path)
        return find_missing_module_docs(module_tree or {}, working_dir)

    @traced("module_generation")
    async def generate_module_documentation(
        self, components: dict[str, Any], leaf_nodes: list[str]
    ) -> str:
        """Generate documentation for all modules using dynamic programming approach."""
        # Prepare output directory
        working_dir = os.path.abspath(self.config.docs_dir)
        file_manager.ensure_directory(working_dir)

        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        module_tree = file_manager.load_json(module_tree_path) or {}
        if getattr(self, "business_analysis", False):
            missing = (
                set(leaf_nodes) - collect_module_tree_component_ids(module_tree)
                if module_tree
                else set()
            )
            if missing:
                raise BusinessClusteringError(
                    "Cached module tree lacks business ownership. Clean generated artifacts and rerun init. "
                    "Missing repository IDs: " + "; ".join(sorted(missing))
                )
        # Subscription backends generate sequentially (no runtime); the shared
        # completion validators read the mode from the backend itself.
        self.backend.business_analysis = getattr(self, "business_analysis", False)

        # Get processing order (leaf modules first) from module_tree.json, not
        # first_module_tree.json. The first tree only holds the initial
        # clustering result (and stays {} in whole-repository mode), while
        # module_tree.json also carries the sub-modules agents inserted via
        # generate_sub_module_documentation_tool. --update/--compare-to
        # invalidates docs against module_tree.json, so ordering from the
        # first tree would skip any invalidated agent-inserted module and the
        # run would end in IncompleteDocumentationError. Iterating the full
        # tree is safe on resume: every module whose .md already exists
        # short-circuits in run_module_agent/generate_parent_module_docs, so
        # unaffected modules cost no LLM calls. In whole-repository mode a
        # regenerated overview.md is rebuilt from the module docs via
        # generate_parent_module_docs([]) rather than by the whole-repo agent.
        processing_order = self.get_processing_order(module_tree)
        from codewiki.src.be.pydantic_ai_backend import PydanticAIBackend

        if (
            getattr(self, "business_analysis", False)
            or generation_concurrency(self.config) > 1
            or isinstance(self.backend, PydanticAIBackend)
        ):
            await self._generate_parallel_modules(components, leaf_nodes, working_dir)
            with trace_span("organize_docs"):
                organize_docs(working_dir, config_layout(self.config))
            return working_dir

        # Process modules in dependency order
        final_module_tree = module_tree
        processed_modules = set()

        if len(module_tree) > 0:
            for module_path, module_name in processing_order:
                # Bound outside try: the except handler logs it, and a KeyError
                # inside the traversal must not turn into a NameError while
                # reporting the original failure.
                module_key = "/".join(module_path)
                try:
                    # Reload module tree to get latest hierarchical structure from sub-agent modifications
                    module_tree = file_manager.load_json(module_tree_path) or {}

                    # Get the module info from the tree
                    module_info = module_tree
                    for path_part in module_path:
                        module_info = module_info[path_part]
                        if path_part != module_path[-1]:  # Not the last part
                            module_info = module_info.get("children", {})

                    # Skip if already processed
                    if module_key in processed_modules:
                        continue

                    # Process the module
                    if self.is_leaf_module(module_info):
                        logger.info(f"📄 Processing leaf module: {module_key}")
                        final_module_tree = await self.backend.run_module_agent(
                            module_name=module_name,
                            components=components,
                            core_component_ids=module_info["components"],
                            module_path=module_path,
                            working_dir=working_dir,
                        )
                    else:
                        logger.info(f"📁 Processing parent module: {module_key}")
                        final_module_tree = await self.generate_parent_module_docs(
                            module_path, working_dir
                        )

                    processed_modules.add(module_key)

                except Exception as e:  # noqa: BLE001 — one failed module must not abort the run
                    logger.error(f"Failed to process module {module_key}: {e!s}")
                    logger.error(f"Traceback: {traceback.format_exc()}")
                    continue

            # Generate repo overview
            logger.info("📚 Generating repository overview")
            final_module_tree = await self.generate_parent_module_docs(
                [], working_dir, components=components
            )
        else:
            logger.info("Processing whole repo because repo can fit in the context window")
            repo_name = os.path.basename(os.path.normpath(self.config.repo_path))
            final_module_tree = await self.backend.run_module_agent(
                module_name=repo_name,
                components=components,
                core_component_ids=leaf_nodes,
                module_path=[],
                working_dir=working_dir,
            )

            # save final_module_tree to module_tree.json
            file_manager.save_json(
                final_module_tree, os.path.join(working_dir, MODULE_TREE_FILENAME)
            )

            # rename repo_name.md to overview.md
            repo_overview_path = os.path.join(working_dir, f"{repo_name}.md")
            if os.path.exists(repo_overview_path):
                os.rename(repo_overview_path, os.path.join(working_dir, OVERVIEW_FILENAME))

        # Move pages an agent saved in the wrong folder and fix links between pages
        with trace_span("organize_docs"):
            organize_docs(working_dir, config_layout(self.config))

        return working_dir

    async def _generate_parallel_modules(self, components, leaf_nodes, working_dir):
        runtime = GenerationRuntime(self.config, working_dir)
        runtime.business_analysis = getattr(self, "business_analysis", False)
        self.generation_runtime = runtime
        self.backend.generation_runtime = runtime
        self._honour_depth_budget(runtime, working_dir)
        workers = asyncio.Semaphore(runtime.limit)

        async def generate_branch(path, info):
            children = list(info.get("children", {}).items())
            if children:
                await run_all(generate_branch(path + [name], child) for name, child in children)
                async with workers:
                    document = runtime.doc_path(path[-1], path)
                    if runtime.needs_agent_resume(document) or not runtime.evidence_current(
                        document, components, path
                    ):
                        await self.backend.run_module_agent(
                            module_name=path[-1],
                            components=components,
                            core_component_ids=info["components"],
                            module_path=path,
                            working_dir=working_dir,
                        )
                    else:
                        await self.generate_parent_module_docs(path, working_dir)
            else:
                async with workers:
                    await self.backend.run_module_agent(
                        module_name=path[-1],
                        components=components,
                        core_component_ids=info["components"],
                        module_path=path,
                        working_dir=working_dir,
                    )
            incomplete = []

            def check_subtree(node_path, node):
                document = resolve_module_doc_path(working_dir, node_path[-1], runtime.tree)
                if document is None or runtime.is_pending(
                    runtime.doc_path(node_path[-1], node_path)
                ):
                    incomplete.append(node_path[-1])
                for name, child in node.get("children", {}).items():
                    check_subtree(node_path + [name], child)

            check_subtree(path, info)
            if incomplete:
                raise IncompleteDocumentationError(incomplete)

        async with runtime.activate():
            repo_name = os.path.basename(os.path.normpath(self.config.repo_path))
            root_pending = runtime.is_pending(runtime.doc_path(repo_name, []))
            if runtime.tree:
                await run_all(
                    generate_branch([name], info) for name, info in list(runtime.tree.items())
                )
            if runtime.tree and (
                not root_pending or not runtime.needs_agent_resume(runtime.doc_path(repo_name, []))
            ):
                await self.generate_parent_module_docs([], working_dir, components=components)
            else:
                await self.backend.run_module_agent(
                    module_name=repo_name,
                    components=components,
                    core_component_ids=leaf_nodes,
                    module_path=[],
                    working_dir=working_dir,
                )
                source = os.path.join(working_dir, f"{repo_name}.md")
                if os.path.exists(source):
                    os.replace(source, os.path.join(working_dir, OVERVIEW_FILENAME))

    def _honour_depth_budget(self, runtime, working_dir):
        """Collapse branches deeper than the configured ``max_depth``.

        A module tree produced under a deeper budget is reused as-is when it is
        loaded from disk, so a run with ``max_depth=1`` would otherwise keep
        generating every nested page: each one is a separate agent session that
        inlines its own copy of the source, and the parent pages hold only an
        index. Collapsing moves the descendants' components up into the parent,
        marks that page for a full rewrite, and archives the now-orphaned child
        pages so nothing is silently dropped.
        """
        max_depth = getattr(self.config, "max_depth", None)
        if not runtime.tree or not max_depth:
            return
        working_dir = Path(working_dir)
        before = _tree_doc_paths(runtime.tree, config_layout(self.config))
        if max_depth >= _tree_depth(runtime.tree):
            return
        rewritten = _collapse_tree(runtime.tree, max_depth, config_layout(self.config))
        runtime.mark_pages_for_rewrite(rewritten)
        runtime.invalidate_summaries([])
        runtime.checkpoint()
        orphans = sorted(before - _tree_doc_paths(runtime.tree, config_layout(self.config)))
        archived = 0
        for relative in orphans:
            source = working_dir / relative
            if not source.exists():
                continue
            backup = working_dir / "temp" / "incomplete" / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                backup = backup.with_name(backup.stem + "." + uuid4().hex + backup.suffix)
            os.replace(source, backup)
            archived += 1
        logger.info(
            "max_depth=%d 生效：已把 %d 个页面压平为 %d 个，"
            "并归档 %d 个嵌套页面（备份于 temp/incomplete）",
            max_depth,
            len(before),
            len(rewritten),
            archived,
        )

    @traced("parent_overview", "module", fields=("module_path",))
    async def generate_parent_module_docs(
        self,
        module_path: list[str],
        working_dir: str,
        components: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generate documentation for a parent module based on its children's documentation.

        For the repository overview (``module_path == []``) pass ``components``
        so the artifact index can be appended to the prompt.
        """
        module_name = (
            module_path[-1]
            if len(module_path) >= 1
            else os.path.basename(os.path.normpath(self.config.repo_path))
        )

        logger.info(f"Generating parent documentation for: {module_name}")

        # Load module tree
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        module_tree = file_manager.load_json(module_tree_path) or {}

        # check if parent docs already exists (for the root module the doc
        # path below resolves to overview.md, so a blanket "overview exists →
        # skip" check is not needed here and would mask missing parent docs
        # on resume)
        existing_docs_path = (
            resolve_module_doc_path(working_dir, module_name, module_tree) if module_path else None
        )
        runtime = getattr(self, "generation_runtime", None)
        expected_doc = (
            runtime.directory / module_doc_relpath(module_path, config_layout(self.config))
            if runtime
            else None
        )
        pending = runtime.is_pending(expected_doc) if runtime else False
        if existing_docs_path is not None and not pending:
            logger.info(f"✓ Parent docs already exists at {existing_docs_path}")
            mark_skipped("已有文档")
            return module_tree
        parent_docs_path = os.path.join(
            working_dir, module_doc_relpath(module_path, config_layout(self.config))
        )
        if os.path.exists(parent_docs_path) and not pending:
            logger.info(f"✓ Parent docs already exists at {parent_docs_path}")
            mark_skipped("已有文档")
            return module_tree

        # Create repo structure with 1-depth children doc paths and target indicator
        repo_structure = self.build_overview_structure(module_tree, module_path, working_dir)

        prompt = (
            MODULE_OVERVIEW_PROMPT.format(
                module_name=module_name, repo_structure=json.dumps(repo_structure, indent=2)
            )
            if len(module_path) >= 1
            else REPO_OVERVIEW_PROMPT.format(
                repo_name=module_name, repo_structure=json.dumps(repo_structure, indent=2)
            )
        )
        if len(module_path) == 0 and components:
            artifact_index = render_artifact_index(components)
            if artifact_index:
                prompt += "\n\n" + REPO_OVERVIEW_ARTIFACT_ADDENDUM.format(
                    artifact_index=artifact_index
                )
        logger.debug(f"Overview prompt for {module_name}: {len(prompt)} chars")

        try:
            if runtime is not None:
                runtime.begin(expected_doc, summary=True)
            complete_args = {
                "system_prompt": format_overview_system_prompt(self.config.get_prompt_addition())
            }
            if generation_concurrency(self.config) > 1:
                parent_docs = await asyncio.to_thread(
                    self.backend.complete, prompt, **complete_args
                )
            else:
                parent_docs = self.backend.complete(prompt, **complete_args)
            if not parent_docs:
                raise RuntimeError(
                    f"LLM returned empty content for {module_name} overview "
                    f"(possible output truncation at max_tokens)"
                )

            parent_content = _parse_overview_response(parent_docs, module_name)
            os.makedirs(os.path.dirname(parent_docs_path), exist_ok=True)
            file_manager.save_text(parent_content, parent_docs_path)
            if runtime is not None:
                runtime.finish(expected_doc)

            logger.debug(f"Successfully generated parent documentation for: {module_name}")
            return module_tree

        except Exception as e:
            logger.error(f"Error generating parent documentation for {module_name}: {e!s}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

    @trace_generation
    async def run(self, *, write_metadata: bool = True) -> None:
        """Run generation; MCP publishes metadata with its completed graph snapshot."""
        try:
            # Build dependency graph
            with trace_span("dependency_analysis") as span:
                components, leaf_nodes = self.graph_builder.build_dependency_graph()
                span.update(component_count=len(components), leaf_count=len(leaf_nodes))

            leaf_nodes = select_business_components(leaf_nodes, components)
            self.analysis_components = components
            self.analysis_leaf_nodes = leaf_nodes
            self.business_analysis = True
            logger.debug(f"Found {len(leaf_nodes)} business analysis entry points")
            # logger.debug(f"Leaf nodes:\n{'\n'.join(sorted(leaf_nodes)[:200])}")
            # exit()

            # Cluster modules
            working_dir = os.path.abspath(self.config.docs_dir)
            file_manager.ensure_directory(working_dir)
            first_module_tree_path = os.path.join(working_dir, FIRST_MODULE_TREE_FILENAME)
            module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)

            # Check if module tree exists
            if os.path.exists(first_module_tree_path):
                logger.debug(f"Module tree found at {first_module_tree_path}")
                with trace_span("clustering"):
                    mark_skipped("复用已有模块树")
                    module_tree = file_manager.load_json(first_module_tree_path)
                # Never clobber an existing module_tree.json with the cached
                # first tree: it carries sub-modules inserted by agents in
                # earlier (resumed) runs, and those modules short-circuit on
                # their existing .md without re-inserting their children.
                if not os.path.exists(module_tree_path):
                    file_manager.save_json(module_tree, module_tree_path)
            else:
                logger.debug(f"Module tree not found at {module_tree_path}, clustering modules")
                clustering_tokens = get_clustering_input_token_count(leaf_nodes, components)
                logger.info(
                    "Preparing %d leaf nodes for module clustering (%d tokens, threshold %d)",
                    len(leaf_nodes),
                    clustering_tokens,
                    self.config.max_token_per_module,
                )
                # Bind cluster_model into the completer so the backend uses the
                # configured clustering model (separate from main_model) when
                # one is set.  Caw mode's cluster_model is typically empty —
                # complete() falls back to its own _model in that case.
                cluster_model = self.config.cluster_model or None
                with trace_span("clustering"):
                    module_tree = cluster_business_modules(
                        leaf_nodes,
                        components,
                        self.config,
                        completer=lambda p: self.backend.complete(p, model=cluster_model),
                    )
                # Only freshly clustered trees are deduped: renaming a cached
                # key whose .md already exists would orphan the doc.
                module_tree = dedupe_module_tree_names(module_tree)
                file_manager.save_json(module_tree, first_module_tree_path)
                file_manager.save_json(module_tree, module_tree_path)

            module_tree = file_manager.load_json(module_tree_path) or {}

            if len(module_tree) == 0:
                logger.info(
                    "Module clustering produced no top-level modules; continuing in "
                    "whole-repository documentation mode"
                )
            else:
                logger.info(
                    "Grouped components into %d top-level modules",
                    len(module_tree),
                )

            # Generate module documentation using dynamic programming approach
            # This processes leaf modules first, then parent modules
            working_dir = await self.generate_module_documentation(components, leaf_nodes)

            # Create documentation metadata
            if write_metadata:
                with trace_span("metadata"):
                    self.create_documentation_metadata(working_dir, components, len(leaf_nodes))

            # Reconcile the final module tree against the docs on disk so
            # name collisions or failed sub-agents can't pass silently (issue #76)
            with trace_span("validation"):
                missing_docs = self.validate_generated_docs(working_dir)
                if missing_docs:
                    for module_name in missing_docs:
                        logger.error(f"Module doc missing after generation: {module_name}.md")
                    raise IncompleteDocumentationError(missing_docs)

            # Deterministic, cache-independent page health check: repair bad
            # encoding / unbalanced fences / Windows-style links and report
            # pages a preview would still fail to render. Runs even when every
            # page came from cache, so stale previews never ship silently.
            with trace_span("wiki_doctor"):
                report = doctor_wiki(working_dir, file_manager.load_json(module_tree_path))
                logger.info(report.summary_line())
                for rel, issue in report.warnings:
                    logger.error("文档体检未通过：%s — %s", rel, issue)

            logger.debug(
                "Documentation generation completed successfully using dynamic programming!"
            )
            logger.debug("Processing order: leaf modules → parent modules → repository overview")
            logger.debug(f"Documentation saved to: {working_dir}")

        except Exception as e:
            logger.error(f"Documentation generation failed: {e!s}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise
