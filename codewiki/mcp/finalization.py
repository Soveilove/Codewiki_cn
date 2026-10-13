"""Publish completed IDE-driven work without advancing unfinished baselines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codewiki.mcp.session import SessionState
from codewiki.mcp.tools.module_tree import _collect_component_ids, _get_processing_order
from codewiki.src.be.doc_layout import list_doc_files, organize_docs
from codewiki.src.be.updater.graph_store import graph_file_path, save_graph
from codewiki.src.be.updater.reference_index import build_reference_index, index_path
from codewiki.src.prompt_assets import FINGERPRINT_KEY, prompt_asset_fingerprint


def finalize_session(session: SessionState) -> dict[str, Any]:
    """Keep the session retryable when pages or source ownership are incomplete."""
    output = Path(session.output_dir)
    config = session.config
    if config is None:
        raise ValueError("Session generation context is missing.")
    order = _get_processing_order(session.module_tree, layout=config.layout)
    expected = {entry["doc_path"] for entry in order} | {"overview.md"}
    missing = sorted(
        path
        for path in expected
        if not (output / path).is_file() or not (output / path).read_text(encoding="utf-8").strip()
    )
    assigned = _collect_component_ids(session.module_tree)
    unknown = sorted(assigned - set(session.components))
    unowned = sorted(set(session.leaf_nodes) - assigned) if session.module_tree else []
    changes = session.registry.get("changes") or {}
    needs: set[str] = set()
    if not changes or session.regenerate or changes.get("requires_full_regeneration"):
        needs = expected
    elif changes and not changes.get("no_changes"):
        affected = set(changes.get("affected_modules", [])) | set(
            changes.get("cascade_modules", [])
        )
        needs = {entry["doc_path"] for entry in order if entry["module"] in affected}
        if changes.get("changed_files") or "overview" in affected:
            needs.add("overview.md")
    needs.update(set(session.registry.get("tree_pending_pages", set())) & expected)
    retired = sorted(
        path
        for path in session.registry.get("tree_removed_pages", set())
        if path not in expected and (output / path).exists()
    )
    unwritten = sorted(needs - session.written_paths)
    if missing or unknown or unowned or unwritten or retired:
        return {
            "status": "incomplete",
            "metadata_saved": False,
            "missing_pages": missing,
            "unknown_component_ids": unknown[:20],
            "unowned_component_ids": unowned[:20],
            "pending_pages": unwritten,
            "retired_pages": retired,
            "hint": "Complete the listed pages and source ownership; delete retired pages, then retry close_session. "
            "Use close_session(discard=true) to release the session without advancing the baseline.",
        }
    if not session.docs_written:
        return {"status": "closed", "metadata_saved": False}
    metadata_path = output / "metadata.json"
    current_metadata = metadata_path.read_bytes() if metadata_path.exists() else None
    if (
        "metadata_snapshot" in session.registry
        and current_metadata != session.registry["metadata_snapshot"]
    ):
        return {
            "status": "incomplete",
            "metadata_saved": False,
            "reason": "Generation metadata changed in another workflow. Reanalyze before publishing this session.",
        }
    try:
        metadata = (
            json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        )
    except (OSError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    old_info = metadata.get("generation_info") or {}
    change = session.prompt_change
    all_rewritten = expected <= session.written_paths
    prompt_hash = change.current if change else prompt_asset_fingerprint(session.repo_path)
    if change and change.status == "changed" and not (session.regenerate and all_rewritten):
        prompt_hash = change.stored
    organize_docs(str(output), config.layout, session.module_tree)
    if session.workspace is None:
        raise ValueError("Session workspace is missing.")
    staged_graph = session.workspace.root / "completed_graph.json"
    save_graph(session.components, str(staged_graph))
    graph_dir = str(output / "temp" / "dependency_graphs")
    index = build_reference_index(str(output), session.components, session.module_tree or None)
    payloads = {
        output / "module_tree.json": json.dumps(
            session.module_tree, indent=2, ensure_ascii=False
        ).encode("utf-8"),
        Path(graph_file_path(graph_dir, session.repo_path)): staged_graph.read_bytes(),
        Path(index_path(str(output))): json.dumps(index, indent=2, ensure_ascii=False).encode(
            "utf-8"
        ),
    }
    first_tree = output / "first_module_tree.json"
    if not first_tree.exists():
        payloads[first_tree] = payloads[output / "module_tree.json"]
    metadata["generation_info"] = {
        **old_info,
        "commit_id": session.analyzed_commit,
        "timestamp": session.analyzed_at,
        "repo_path": session.repo_path,
        "language": config.language,
        "layout": config.layout,
        "generation_mode": "mcp",
        "analysis_mode": "business",
        FINGERPRINT_KEY: prompt_hash,
    }
    metadata["statistics"] = {
        **metadata.get("statistics", {}),
        "total_components": len(session.components),
        "leaf_nodes": len(session.leaf_nodes),
    }
    metadata["files_generated"] = sorted(
        list_doc_files(str(output), session.module_tree).values()
    ) + [
        "module_tree.json",
        "first_module_tree.json",
        "metadata.json",
    ]
    payloads[metadata_path] = json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8")
    _publish_baseline(payloads)
    return {
        "status": "closed",
        "metadata_saved": True,
        "prompt_changed_during_session": prompt_asset_fingerprint(session.repo_path) != prompt_hash,
    }


def _publish_baseline(payloads: dict[Path, bytes]) -> None:
    previous = {path: path.read_bytes() if path.exists() else None for path in payloads}
    written: list[Path] = []
    try:
        for path, data in payloads.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            written.append(path)
            path.write_bytes(data)
    except OSError:
        for path in reversed(written):
            original = previous[path]
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        raise
