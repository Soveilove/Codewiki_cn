"""MCP tool: get_prompt — serve CodeWiki's prompt templates to the IDE agent.

CodeWiki ships with carefully designed prompt templates for each stage of
the wiki generation pipeline.  This tool lets the IDE agent retrieve them
(with optional variable substitution) so it can follow the same proven
methodology without needing its own copy of the prompts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from codewiki.mcp.session import SessionState, SessionStore
from codewiki.src.be.business_clustering import BUSINESS_PLANNING_RULES
from codewiki.src.be.doc_layout import iter_tree_paths, relative_link, module_doc_relpath
from codewiki.src.prompt_assets import prompt_asset_summary
from codewiki.src.be.prompt_template import (
    USER_PROMPT,
    REPO_OVERVIEW_PROMPT,
    MODULE_OVERVIEW_PROMPT,
    format_system_prompt,
    format_leaf_system_prompt,
    format_cluster_prompt,
    format_links_note,
    format_user_prompt,
    format_overview_system_prompt,
    BUSINESS_DOCUMENTATION_RULES,
)
from codewiki.src.config import Config, DEFAULT_LAYOUT


# Prompt catalog: maps prompt_type to (raw_template, usage_hint, variables_doc)
_PROMPT_CATALOG: dict[str, dict[str, str]] = {
    "cluster": {
        "description": "Prompt for grouping components into modules. The LLM receives a component list and returns a JSON module tree.",
        "usage_hint": (
            "Use this prompt to cluster components into logical modules. "
            "The response should contain <GROUPED_COMPONENTS> JSON. "
            "Pass the component list from analyze_repo's component_index."
        ),
    },
    "system_complex": {
        "description": "System prompt for documenting a complex (multi-file, parent) module. Includes sub-module delegation instructions.",
        "usage_hint": (
            "Use as the system prompt when generating docs for a parent module. "
            "The agent should create the module's page at its doc_path (from "
            "processing_order.json) with architecture overview and cross-references to "
            "sub-module docs. Pass variables module_name and module_path."
        ),
    },
    "system_leaf": {
        "description": "System prompt for documenting a leaf (single-file or simple) module.",
        "usage_hint": (
            "Use as the system prompt when generating docs for a leaf module. "
            "The agent should create the module's page at its doc_path (from "
            "processing_order.json) with detailed documentation including Mermaid diagrams. "
            "Pass variables module_name and module_path."
        ),
    },
    "user": {
        "description": "User prompt template that provides the module tree and core component source code.",
        "usage_hint": (
            "Use as the user/assistant prompt alongside system_leaf or system_complex. "
            "It provides the module tree context and the actual source code of core components."
        ),
    },
    "overview_module": {
        "description": "Prompt for generating a parent module overview from its children's documentation.",
        "usage_hint": (
            "Use this after all child modules are documented. "
            "Provide the module tree with children's docs embedded. "
            "The response should be wrapped in <OVERVIEW> tags."
        ),
    },
    "overview_repo": {
        "description": "Prompt for generating the final repository overview.",
        "usage_hint": (
            "Use this as the LAST step after all modules are documented. "
            "Provide the full module tree with child docs. "
            "Save the result as overview.md."
        ),
    },
}


def handle_get_prompt(
    arguments: dict[str, Any],
    store: SessionStore,
) -> str:
    """Return a prompt template, optionally with variables filled in."""
    prompt_type = arguments["prompt_type"]
    variables = arguments.get("variables", {})

    if prompt_type not in _PROMPT_CATALOG:
        available = list(_PROMPT_CATALOG.keys())
        return json.dumps(
            {
                "error": f"Unknown prompt_type: {prompt_type}",
                "available_types": available,
            }
        )

    catalog_entry = _PROMPT_CATALOG[prompt_type]

    session_id = arguments.get("session_id") or variables.get("session_id")
    session = store.get(session_id) if session_id else None
    if session_id and session is None:
        return json.dumps({"error": f"Session {session_id} not found or expired."})
    variables = dict(variables)
    if session is not None and session.config is not None:
        variables = _session_variables(session, variables, prompt_type)
    content = _resolve_prompt(prompt_type, variables)
    if session is not None and session.config is not None and prompt_type == "user":
        path, layout = _page_location(variables, variables["module_name"])
        ids = variables.get("component_ids", [])
        provided: set[str] = set()
        content = (
            format_user_prompt(
                variables["module_name"],
                ids,
                session.components,
                session.module_tree,
                path,
                layout,
                business_analysis=True,
                prompt_read_ids=provided,
            )
            + "\n\n"
            + variables.get("custom_instructions", "")
        )
        content = content.replace("str_replace_editor", "your IDE's read-only file tools")
        session.source_reads.update(provided)
    result = {
        "prompt_type": prompt_type,
        "description": catalog_entry["description"],
        "usage_hint": catalog_entry["usage_hint"],
        "content": content,
    }
    if session is not None and session.config is not None:
        result["language"] = session.config.language
        result["team_prompt"] = (
            prompt_asset_summary(session.prompt_asset) if session.prompt_asset else None
        )
        if session.workspace is not None:
            digest = hashlib.sha256(
                json.dumps(variables, sort_keys=True, default=str).encode()
            ).hexdigest()[:12]
            prompt_file = session.workspace.root / f"prompt_{prompt_type}_{digest}.txt"
            prompt_file.write_text(content, encoding="utf-8")
            result["content_file"] = str(prompt_file)
            if len(content) > 12_000:
                result["content"] = None
                result["hint"] = "Read content_file for the complete prompt; it is not truncated."
    return json.dumps(result, indent=2, ensure_ascii=False)


def _session_variables(
    session: SessionState,
    variables: dict[str, Any],
    prompt_type: str,
) -> dict[str, Any]:
    config = session.config
    if config is None:
        return variables
    path = variables.get("module_path")
    name = variables.get("module_name")
    if isinstance(path, str):
        path = [part for part in path.split("/") if part]
    if path is None and name:
        path = next((p for p in iter_tree_paths(session.module_tree) if p[-1] == name), [name])
    if path is None:
        path = []
    if prompt_type == "overview_repo":
        path = []
    name = name or (path[-1] if path else Path(session.repo_path).name)
    if not path and prompt_type in ("system_leaf", "system_complex", "user"):
        name = "overview"
    topic = session.module_tree
    for index, part in enumerate(path):
        topic = topic.get(part, {})
        if index < len(path) - 1:
            topic = topic.get("children", {})
    additions = config.get_prompt_addition()
    extra = variables.get("custom_instructions")
    if extra:
        additions += "\n" + extra
    result = {
        **variables,
        "module_name": name,
        "module_path": path,
        "module_tree": session.module_tree,
        "layout": config.layout,
        "language": config.language,
        "custom_instructions": additions,
        "_resolved_instructions": True,
        "component_ids": topic.get("components", []) if path else session.leaf_nodes,
        "repo_name": Path(session.repo_path).name,
    }
    if prompt_type == "cluster" and session.workspace is not None:
        result["planning_evidence_file"] = str(session.workspace.root / "planning_evidence.json")
        result["potential_core_components"] = (
            "Read leaf_nodes.json and planning_evidence.json from this session."
        )
    if prompt_type in ("overview_module", "overview_repo"):
        children = topic.get("children", {}) if path else session.module_tree
        page = module_doc_relpath(path, config.layout)
        result["repo_structure"] = {
            child: {
                "purpose": info.get("purpose"),
                "docs_path": str(
                    Path(session.output_dir) / module_doc_relpath(path + [child], config.layout)
                ),
                "link": relative_link(page, module_doc_relpath(path + [child], config.layout)),
            }
            for child, info in children.items()
        }
    return result


def _page_location(variables: dict[str, Any], module_name: str) -> tuple[list[str], str]:
    """``module_path`` and docs ``layout`` from the variables (top-level module by default)."""
    module_path = variables.get("module_path", [module_name])
    if isinstance(module_path, str):
        module_path = [p for p in module_path.split("/") if p]
    return list(module_path), variables.get("layout") or DEFAULT_LAYOUT


def _resolve_prompt(prompt_type: str, variables: dict[str, Any]) -> str:
    """Resolve a prompt template with optional variable substitution."""
    config = Config.from_cli(
        repo_path=".",
        output_dir=".",
        llm_base_url="not-needed",
        llm_api_key="not-needed",
        main_model="unused",
        cluster_model="unused",
        agent_instructions={
            key: variables[key]
            for key in ("language", "doc_type", "focus_modules", "custom_instructions")
            if variables.get(key)
        },
    )
    custom = config.get_prompt_addition()
    if variables.get("_resolved_instructions"):
        custom = variables.get("custom_instructions", "")
    custom = BUSINESS_DOCUMENTATION_RULES + ("\n\n" + custom if custom else "")

    if prompt_type == "cluster":
        potential_core_components = variables.get(
            "potential_core_components", "<POTENTIAL_CORE_COMPONENTS placeholder>"
        )
        module_tree = variables.get("module_tree", {})
        module_name = variables.get("module_name", None)
        rules = (
            BUSINESS_PLANNING_RULES.replace(
                "Each supplied C reference",
                "Each supplied component ID",
            )
            .replace('["C0"]', '["file.py::Component"]')
            .replace(
                "Use only C references from INPUT_EVIDENCE",
                "Use only exact supplied component IDs",
            )
        )
        evidence = variables.get("planning_evidence_file")
        return (
            format_cluster_prompt(
                potential_core_components, module_tree, module_name, variables.get("language")
            )
            + "\n\n"
            + rules
            + "\nReturn full IDs, not C references, to save_module_tree."
            + (f"\nRead planning evidence from: {evidence}" if evidence else "")
            + "\n\n"
            + custom
        )

    elif prompt_type in ("system_complex", "system_leaf"):
        module_name = variables.get("module_name", "MODULE_NAME")
        module_path, layout = _page_location(variables, module_name)
        formatter = (
            format_system_prompt if prompt_type == "system_complex" else format_leaf_system_prompt
        )
        content = formatter(module_name, custom, module_path, layout)
        return (
            content.replace("`str_replace_editor`", "`write_doc_file` / `edit_doc_file`").replace(
                "`generate_sub_module_documentation`",
                "`save_module_tree` followed by IDE-driven child generation",
            )
            + "\nFor repository reads use your IDE's read-only file tools. Never edit repository source or team assets."
        )

    elif prompt_type == "user":
        module_name = variables.get("module_name", "MODULE_NAME")
        module_tree = variables.get("module_tree", {})

        module_path, layout = _page_location(variables, module_name)
        # Return the template with placeholders filled as possible
        return (
            USER_PROMPT.format(
                module_name=module_name,
                module_tree=json.dumps(module_tree, indent=2)
                if module_tree
                else "<MODULE_TREE placeholder>",
                formatted_core_component_codes=variables.get(
                    "formatted_core_component_codes",
                    "<CORE_COMPONENT_CODES placeholder — use read_code_components to get source code>",
                ),
            )
            + "\n\n"
            + format_links_note(module_name, module_path, layout)
            + "\n\n"
            + custom
        )

    elif prompt_type == "overview_module":
        module_name = variables.get("module_name", "MODULE_NAME")
        repo_structure = variables.get("repo_structure", "<REPO_STRUCTURE placeholder>")
        return (
            MODULE_OVERVIEW_PROMPT.format(
                module_name=module_name,
                repo_structure=repo_structure
                if isinstance(repo_structure, str)
                else json.dumps(repo_structure, indent=4),
            )
            + "\n\n"
            + (format_overview_system_prompt(custom) or "")
        )

    elif prompt_type == "overview_repo":
        repo_name = variables.get("repo_name", "REPO_NAME")
        repo_structure = variables.get("repo_structure", "<REPO_STRUCTURE placeholder>")
        return (
            REPO_OVERVIEW_PROMPT.format(
                repo_name=repo_name,
                repo_structure=repo_structure
                if isinstance(repo_structure, str)
                else json.dumps(repo_structure, indent=4),
            )
            + "\n\n"
            + (format_overview_system_prompt(custom) or "")
        )

    return f"Unknown prompt type: {prompt_type}"
