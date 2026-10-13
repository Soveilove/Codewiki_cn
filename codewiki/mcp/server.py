"""
CodeWiki MCP Server.

Provides two sets of tools:

**Fine-grained tools (IDE-driven, zero LLM config):**
  - ``analyze_repo``      — Parse a repo and build a dependency graph (session-based)
  - ``read_code_components`` — Write component source code to workspace files
  - ``write_doc_file``    — Create a documentation .md file with Mermaid validation
  - ``edit_doc_file``     — Edit a documentation file (str_replace / insert / undo)
  - ``save_module_tree``  — Persist IDE agent's module clustering
  - ``get_processing_order`` — Get leaf-first documentation order
  - ``get_prompt``        — Retrieve CodeWiki's prompt templates
  - ``close_session``     — Clean up a session and workspace files

Large analysis results (component index, source code, processing order) are
written to workspace files on disk.  The IDE agent reads these files directly
instead of receiving large payloads through the MCP stdio channel.

**Legacy tools (require CodeWiki LLM config):**
  - ``generate_docs``     — Full documentation generation (black-box)
  - ``get_module_tree``   — Retrieve existing module clustering

Usage:
    python -m codewiki.mcp.server

    # Cursor / Claude Desktop config:
    {
        "mcpServers": {
            "codewiki": {
                "command": "python",
                "args": ["-m", "codewiki.mcp.server"]
            }
        }
    }
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from codewiki.mcp.session import SessionState, SessionStore
from codewiki.src.be.doc_layout import resolve_output_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global session store (lives for the lifetime of the MCP server process)
# ---------------------------------------------------------------------------
_store = SessionStore()

# ---------------------------------------------------------------------------
# MCP Server instance
# ---------------------------------------------------------------------------
server = Server("codewiki")


# ===================================================================
#  Tool definitions
# ===================================================================


def _generation_properties() -> dict[str, Any]:
    return {
        "language": {
            "type": "string",
            "description": "Output language; team conventions win on new builds. Updates preserve the stored language.",
        },
        "doc_type": {
            "type": "string",
            "description": "Documentation focus, including business knowledge.",
        },
        "regenerate": {
            "type": "boolean",
            "default": False,
            "description": "Explicitly rebuild all pages using current team conventions.",
        },
        "artifact_exclude": {
            "type": "string",
            "description": "Comma-separated artifact exclusion patterns.",
        },
        "artifacts_enabled": {"type": "boolean", "default": True},
        "artifact_token_budget": {"type": "integer", "minimum": 1, "default": 200000},
        "with_prose": {"type": "boolean", "default": False},
    }


def _fine_grained_tools() -> list[Tool]:
    """Return the zero-config, IDE-driven tool set."""
    return [
        Tool(
            name="analyze_repo",
            description=(
                "Analyze a code repository's structure, dependencies, and components "
                "using Tree-sitter AST parsing. No LLM required. "
                "Writes the full component index, leaf nodes, and language stats to "
                "workspace files on disk, and returns file paths plus a compact summary. "
                "Read the workspace files for complete data. "
                "This is the entry point for the wiki generation pipeline. "
                "After calling this, use get_prompt('cluster') to learn clustering rules, "
                "then save_module_tree to persist your grouping. "
                "INCREMENTAL UPDATE: If docs already exist in output_dir (metadata.json + "
                "module_tree.json), the response includes a 'changes' field showing which "
                "files changed and which modules need updating."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path to the repository to analyze",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Output directory for generated docs (default: <repo>/.codewiki)",
                    },
                    "include_patterns": {
                        "type": "string",
                        "description": "Comma-separated file patterns to include (e.g., '*.py,*.js')",
                    },
                    "exclude_patterns": {
                        "type": "string",
                        "description": "Comma-separated patterns to exclude (e.g., '*test*,*spec*')",
                    },
                    "use_gitignore": {
                        "type": "boolean",
                        "description": "Apply Git ignore rules before analysis (default: true)",
                        "default": True,
                    },
                    **_generation_properties(),
                },
                "required": ["repo_path"],
            },
        ),
        Tool(
            name="read_code_components",
            description=(
                "Write the source code for a list of component IDs to workspace files. "
                "Component IDs have the form 'file_path::ComponentName'. "
                "Each component's full source is written to an individual .src file "
                "in the session's sources/ directory. Returns file paths — no truncation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID from analyze_repo",
                    },
                    "component_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of component IDs to read",
                    },
                },
                "required": ["session_id", "component_ids"],
            },
        ),
        Tool(
            name="write_doc_file",
            description=(
                "Create a new markdown documentation file in the output directory. "
                "Automatically validates Mermaid diagrams after writing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID from analyze_repo",
                    },
                    "filename": {
                        "type": "string",
                        "description": (
                            "Path of the doc relative to the output dir: the module's "
                            "doc_path from processing_order.json (e.g., 'auth.md' or "
                            "'auth/login.md'; folders are created as needed)"
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Markdown content to write",
                    },
                },
                "required": ["session_id", "filename", "content"],
            },
        ),
        Tool(
            name="edit_doc_file",
            description=(
                "Edit an existing documentation file. Supports str_replace (find-and-replace), "
                "insert (add text at a line), delete (generated pages only), and undo (revert last edit or delete). "
                "Automatically validates Mermaid diagrams after editing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID from analyze_repo",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Filename of the doc to edit",
                    },
                    "command": {
                        "type": "string",
                        "enum": ["str_replace", "insert", "undo", "delete"],
                        "description": "Edit command to run",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "String to find (required for str_replace)",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement string (for str_replace/insert)",
                    },
                    "insert_line": {
                        "type": "integer",
                        "description": "Line number for insert (0-indexed)",
                    },
                },
                "required": ["session_id", "filename", "command"],
            },
        ),
        Tool(
            name="save_module_tree",
            description=(
                "Save the IDE agent's module clustering result. "
                "Accepts a JSON module tree and persists it to disk. "
                "Computes the leaf-first processing order and writes it to a workspace file. "
                "Returns the file path for the processing order. "
                "Stages the tree in the session until close_session completes. "
                "Rejects unknown IDs, unsafe names and incomplete business source ownership. "
                "Shared components get one primary explanation plus cross-topic references. "
                "An empty tree uses overview.md to document the complete repository."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID from analyze_repo",
                    },
                    "module_tree": {
                        "type": "object",
                        "description": (
                            "Module tree dict. Each key is a module name with value "
                            "{'components': [component_ids], 'children': {nested modules}}"
                        ),
                    },
                },
                "required": ["session_id", "module_tree"],
            },
        ),
        Tool(
            name="get_processing_order",
            description=(
                "Compute and write the leaf-first processing order to a workspace file. "
                "Returns the file path. Process leaf modules (is_leaf=true) before parent modules."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID from analyze_repo",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="get_prompt",
            description=(
                "Retrieve CodeWiki's prompt templates for each pipeline stage. "
                "Available types: cluster, system_complex, system_leaf, user, "
                "overview_module, overview_repo. Pass session_id to apply team conventions and business evidence. "
                "Large prompts return content_file rather than inline content. Standalone template queries remain supported."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt_type": {
                        "type": "string",
                        "enum": [
                            "cluster",
                            "system_complex",
                            "system_leaf",
                            "user",
                            "overview_module",
                            "overview_repo",
                        ],
                        "description": "Which prompt template to retrieve",
                    },
                    "variables": {
                        "type": "object",
                        "description": "Optional module_name/module_path and standalone template variables",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional analysis session; applies team conventions, language, evidence and layout automatically.",
                    },
                },
                "required": ["prompt_type"],
            },
        ),
        Tool(
            name="close_session",
            description="Publish a completed session and clean up. Incomplete pages return 'incomplete' and keep the session retryable; discard skips baseline publication.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID to close",
                    },
                    "discard": {
                        "type": "boolean",
                        "default": False,
                        "description": "Release an unfinished session without advancing generation metadata or the graph baseline.",
                    },
                },
                "required": ["session_id"],
            },
        ),
    ]


def _legacy_tools() -> list[Tool]:
    """Return the legacy tools that require CodeWiki LLM configuration."""
    return [
        Tool(
            name="generate_docs",
            description=(
                "[LEGACY — requires 'codewiki config set' first] "
                "Generate full documentation for a repository in one shot. "
                "For IDE-driven generation, use the fine-grained tools instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path to the repository to document",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Output directory for generated docs (default: <repo>/.codewiki)",
                    },
                    "doc_type": {
                        "type": "string",
                        "enum": ["api", "architecture", "user-guide", "developer"],
                        "description": "Type of documentation to generate",
                    },
                    "language": {
                        "type": "string",
                        "description": "Language of the generated docs, as a code or name "
                        "(e.g. 'ja', 'Japanese'; default: English)",
                    },
                    "include_patterns": {
                        "type": "string",
                        "description": "Comma-separated file patterns to include",
                    },
                    "exclude_patterns": {
                        "type": "string",
                        "description": "Comma-separated patterns to exclude",
                    },
                    "use_gitignore": {
                        "type": "boolean",
                        "description": "Apply Git ignore rules before analysis (default: true)",
                        "default": True,
                    },
                    **_generation_properties(),
                },
                "required": ["repo_path"],
            },
        ),
        Tool(
            name="get_module_tree",
            description="Get the existing module clustering tree for a repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path to the repository",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Directory containing generated docs (default: <repo>/.codewiki)",
                    },
                },
                "required": ["repo_path"],
            },
        ),
    ]


# ===================================================================
#  Tool dispatch
# ===================================================================


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available CodeWiki MCP tools."""
    return _fine_grained_tools() + _legacy_tools()


_analyze_lock = asyncio.Lock()


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Route tool calls to the appropriate handler."""
    try:
        # --- Fine-grained tools (no LLM config needed) ---
        # Synchronous handlers run via asyncio.to_thread() so they never
        # block the event loop (which would hang the MCP stdio server).
        if name == "analyze_repo":
            from codewiki.mcp.tools.analysis import handle_analyze_repo

            # Offloaded to a thread so the long-running analysis does not
            # block the stdio pump.  The lock serialises concurrent
            # analyze_repo calls: each analyzer owns its own Tree-sitter
            # Parser, but parsers are not safe to drive from several
            # threads at once and the job is heavy anyway.
            async with _analyze_lock:
                return [_text(await asyncio.to_thread(handle_analyze_repo, arguments, _store))]

        elif name == "read_code_components":
            from codewiki.mcp.tools.code_reader import handle_read_code_components

            return [_text(await asyncio.to_thread(handle_read_code_components, arguments, _store))]

        elif name == "write_doc_file":
            from codewiki.mcp.tools.doc_writer import handle_write_doc_file

            result = await handle_write_doc_file(arguments, _store)
            return [_text(result)]

        elif name == "edit_doc_file":
            from codewiki.mcp.tools.doc_writer import handle_edit_doc_file

            result = await handle_edit_doc_file(arguments, _store)
            return [_text(result)]

        elif name == "save_module_tree":
            from codewiki.mcp.tools.module_tree import handle_save_module_tree

            return [_text(await asyncio.to_thread(handle_save_module_tree, arguments, _store))]

        elif name == "get_processing_order":
            from codewiki.mcp.tools.module_tree import handle_get_processing_order

            return [_text(await asyncio.to_thread(handle_get_processing_order, arguments, _store))]

        elif name == "get_prompt":
            from codewiki.mcp.tools.prompt_server import handle_get_prompt

            return [_text(await asyncio.to_thread(handle_get_prompt, arguments, _store))]

        elif name == "close_session":
            sid = arguments["session_id"]
            session = _store.get(sid)
            closed: dict[str, Any] = {
                "status": "not_found",
                "session_id": sid,
                "metadata_saved": False,
            }
            if session:
                closed["status"] = "closed"
                if not arguments.get("discard", False) and session.docs_written:
                    if session.config is not None:
                        from codewiki.mcp.finalization import finalize_session

                        closed.update(await asyncio.to_thread(finalize_session, session))
                        if closed.get("status") == "incomplete":
                            return [_text(json.dumps(closed, ensure_ascii=False))]
                    else:
                        _write_generation_metadata(session)
                        closed["metadata_saved"] = True
                if session.workspace is not None:
                    session.workspace.cleanup()
                _store.remove(sid)
            return [_text(json.dumps(closed, ensure_ascii=False))]

        # --- Legacy tools (require CodeWiki LLM config) ---
        elif name == "generate_docs":
            return await _legacy_generate_docs(arguments)

        elif name == "get_module_tree":
            return await _legacy_get_module_tree(arguments)

        else:
            return [_text(json.dumps({"error": f"Unknown tool: {name}"}))]

    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return [_text(json.dumps({"error": str(e)}))]


# ===================================================================
#  Legacy tool handlers (require _load_config)
# ===================================================================


def _load_config():
    """Load CodeWiki configuration from ~/.codewiki/config.json + keyring."""
    from codewiki.cli.config_manager import ConfigManager

    manager = ConfigManager()
    if not manager.load():
        raise RuntimeError("CodeWiki not configured. Run 'codewiki config set' first.")
    return manager


async def _legacy_generate_docs(arguments: dict[str, Any]) -> list[TextContent]:
    """Legacy generate_docs — requires CodeWiki LLM configuration."""
    repo_path = Path(arguments["repo_path"]).expanduser().resolve()
    output_dir = resolve_output_dir(repo_path, arguments.get("output_dir"))

    if not repo_path.exists():
        return [_text(json.dumps({"error": f"Repository not found: {repo_path}"}))]

    manager = _load_config()
    config = manager.get_config()
    if config is None:
        raise RuntimeError("CodeWiki configuration is unavailable.")
    api_key = manager.get_api_key()

    from codewiki.src.be.backend import is_caw_provider

    caw_mode = bool(config) and is_caw_provider(getattr(config, "provider", ""))
    if not api_key and not caw_mode:
        return [
            _text(
                json.dumps(
                    {"error": "API key not configured. Run 'codewiki config set --api-key <key>'"}
                )
            )
        ]

    from codewiki.mcp.context import build_context
    from codewiki.src.be.doc_layout import list_doc_files, preserve_generated_docs
    from codewiki.src.prompt_assets import detect_prompt_change, prompt_asset_summary

    from codewiki.mcp.tools.analysis import _detect_changes

    change = detect_prompt_change(str(repo_path), str(output_dir))
    previous_changes = _detect_changes(repo_path, output_dir)
    if not arguments.get("regenerate", False) and (
        change.status == "changed" or (previous_changes and not previous_changes.get("no_changes"))
    ):
        return [
            _text(
                json.dumps(
                    {
                        "status": "needs_confirmation",
                        "output_dir": str(output_dir),
                        "reason": "Existing documentation needs updating. Confirm generate_docs(regenerate=true) for a full rebuild, or use IDE-driven tools for incremental changes.",
                    }
                )
            )
        ]
    saved = getattr(config, "agent_instructions", None)
    backend_config, asset = build_context(repo_path, arguments, saved.to_dict() if saved else None)
    backend_config.llm_base_url = config.base_url
    backend_config.llm_api_key = api_key or ""
    backend_config.main_model = config.main_model
    backend_config.cluster_model = config.cluster_model
    for key in (
        "fallback_model",
        "provider",
        "aws_region",
        "api_version",
        "azure_deployment",
        "max_tokens",
        "max_token_per_module",
        "max_token_per_leaf_module",
        "max_depth",
        "request_limit",
        "agent_retries",
        "concurrency",
        "prompt_caching",
        "trace",
    ):
        if hasattr(config, key):
            setattr(backend_config, key, getattr(config, key))
    previous_docs = None
    if arguments.get("regenerate", False) and output_dir.exists():
        previous_docs = str(preserve_generated_docs(repo_path, output_dir))

    from codewiki.cli.utils.repo_validator import get_git_commit_hash
    from codewiki.src.be.documentation_generator import DocumentationGenerator

    import uuid
    from datetime import UTC, datetime
    from codewiki.mcp.workspace import SessionWorkspace
    from codewiki.mcp.finalization import finalize_session
    from codewiki.src.be.doc_layout import load_module_tree

    workspace = SessionWorkspace(repo_path, uuid.uuid4().hex[:12])
    state = SessionState(workspace.root.name, str(repo_path), str(output_dir), {}, [])
    change.current = asset.sha256
    state.config, state.prompt_asset, state.prompt_change = backend_config, asset, change
    state.regenerate = bool(arguments.get("regenerate"))
    state.workspace = workspace
    state.analyzed_commit = get_git_commit_hash(repo_path) or None
    state.analyzed_at = datetime.now(UTC).isoformat()
    metadata_path = output_dir / "metadata.json"
    state.registry["metadata_snapshot"] = (
        metadata_path.read_bytes() if metadata_path.exists() else None
    )
    backend_config.output_dir = str(workspace.root / "temp")
    backend_config.dependency_graph_dir = str(workspace.root / "temp" / "dependency_graphs")
    try:
        doc_gen = DocumentationGenerator(backend_config, commit_id=state.analyzed_commit)
        await doc_gen.run(write_metadata=False)
        state.components = doc_gen.analysis_components
        state.leaf_nodes = doc_gen.analysis_leaf_nodes
        state.module_tree = load_module_tree(str(output_dir))
        state.written_paths = set(list_doc_files(str(output_dir)).values())
        state.docs_written = len(state.written_paths)
        finalized = finalize_session(state)
        if not finalized.get("metadata_saved"):
            raise RuntimeError(f"Documentation generation incomplete: {finalized}")
    finally:
        workspace.cleanup()

    generated_files = sorted(list_doc_files(str(output_dir)).values())
    for f in output_dir.iterdir():
        if f.suffix in (".json", ".html"):
            generated_files.append(f.name)

    result = {
        "status": "success",
        "output_dir": str(output_dir),
        "files_generated": sorted(generated_files),
        "file_count": len(generated_files),
        "team_prompt": prompt_asset_summary(asset),
        "previous_docs": previous_docs,
    }
    return [_text(json.dumps(result, indent=2))]


async def _legacy_get_module_tree(arguments: dict[str, Any]) -> list[TextContent]:
    """Legacy get_module_tree."""
    repo_path = Path(arguments["repo_path"]).expanduser().resolve()
    output_dir = resolve_output_dir(repo_path, arguments.get("output_dir"))

    module_tree_path = output_dir / "module_tree.json"
    if not module_tree_path.exists():
        return [
            _text(
                json.dumps(
                    {
                        "error": f"Module tree not found at {module_tree_path}. Run 'codewiki generate' first."
                    }
                )
            )
        ]

    module_tree = json.loads(module_tree_path.read_text(encoding="utf-8"))

    def _summarize_tree(tree, depth=0):
        lines = []
        for name, info in tree.items():
            indent = "  " * depth
            comp_count = len(info.get("components", []))
            children = info.get("children", {})
            child_count = len(children) if isinstance(children, dict) else 0
            lines.append(f"{indent}- {name} ({comp_count} components, {child_count} children)")
            if isinstance(children, dict) and children:
                lines.extend(_summarize_tree(children, depth + 1))
        return lines

    summary = "\n".join(_summarize_tree(module_tree))
    result = {
        "status": "success",
        "module_tree_path": str(module_tree_path),
        "total_modules": len(module_tree),
        "tree_summary": summary,
    }
    return [_text(json.dumps(result, indent=2))]


# ===================================================================
#  Helpers
# ===================================================================


def _text(content: str) -> TextContent:
    return TextContent(type="text", text=content)


def _write_generation_metadata(session: SessionState) -> None:
    """Write ``metadata.json`` to the session's output directory.

    Merges the current git commit and timestamp into any existing metadata
    so that stats from other workflows (e.g. CLI ``generate_docs``) are
    preserved.
    """
    try:
        output_dir = Path(session.output_dir)
        repo_path = Path(session.repo_path)

        # Baseline on the commit analyze_repo saw, NOT the current HEAD:
        # commits made mid-session were never analyzed, and recording HEAD
        # here would silently exclude them from the next incremental run.
        commit_id: str | None = session.analyzed_commit
        if not commit_id:
            from codewiki.cli.utils.repo_validator import get_git_commit_hash

            commit_id = get_git_commit_hash(repo_path) or None

        from datetime import datetime

        # Read existing metadata (if any) and merge
        metadata_path = output_dir / "metadata.json"
        existing: dict = {}
        if metadata_path.exists():
            try:
                existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        from codewiki.src.be.doc_layout import docs_layout, organize_docs

        # Keep the layout of existing docs; new docs get the default. Move
        # misplaced pages and fix links between pages before stamping it.
        layout = docs_layout(str(output_dir))
        organize_docs(str(output_dir), layout)

        existing["generation_info"] = {
            **existing.get("generation_info", {}),
            "commit_id": commit_id,
            "timestamp": datetime.now().isoformat(),
            "layout": layout,
        }
        metadata_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to write metadata.json: %s", e)


# ===================================================================
#  Entry point
# ===================================================================


async def main():
    """Run the MCP server with stdio transport."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
