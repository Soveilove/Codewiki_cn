"""Generation context shared by the IDE-driven MCP tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codewiki.src.be.doc_layout import docs_layout, resolve_output_dir
from codewiki.src.config import Config
from codewiki.src.language import resolve_update_language
from codewiki.src.prompt_assets import (
    PromptAsset,
    detect_prompt_change,
    resolve_prompt_instructions,
)


def build_context(
    repo_path: Path,
    arguments: dict[str, Any],
    base: dict[str, Any] | None = None,
) -> tuple[Config, PromptAsset]:
    """Resolve repository conventions without requiring any model credentials."""
    output_dir = resolve_output_dir(repo_path, arguments.get("output_dir"))
    instructions = dict(base or {})
    for key in (
        "language",
        "doc_type",
        "custom_instructions",
        "focus_modules",
        "include_patterns",
        "exclude_patterns",
        "artifact_exclude",
    ):
        if key in arguments:
            value = arguments[key]
            if key in ("focus_modules", "include_patterns", "exclude_patterns", "artifact_exclude"):
                if isinstance(value, str):
                    value = [part.strip() for part in value.split(",") if part.strip()]
            instructions[key] = value
    instructions, asset = resolve_prompt_instructions(str(repo_path), instructions)
    metadata_path = output_dir / "metadata.json"
    if metadata_path.exists() and not arguments.get("regenerate", False):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata.json must contain an object.")
        stored = (metadata.get("generation_info") or {}).get("language")
        change = detect_prompt_change(str(repo_path), str(output_dir))
        requested = arguments.get("language")
        if change.status != "changed":
            language = resolve_update_language(stored, requested)
        else:
            language = stored
        instructions = {**(instructions or {}), "language": language}
    config = Config.from_cli(
        repo_path=str(repo_path),
        output_dir=str(output_dir),
        llm_base_url="not-needed",
        llm_api_key="not-needed",
        main_model="unused",
        cluster_model="unused",
        agent_instructions=instructions or None,
        use_gitignore=arguments.get("use_gitignore", True),
        artifacts_enabled=arguments.get("artifacts_enabled", True),
        artifact_token_budget=arguments.get("artifact_token_budget", 200_000),
        with_prose=arguments.get("with_prose", False),
        layout=docs_layout(str(output_dir)),
        trace=False,
    )
    return config, asset
