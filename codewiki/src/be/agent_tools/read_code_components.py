from pathlib import Path

from pydantic_ai import RunContext, Tool, ModelRetry
from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.cluster_modules import ComponentIdResolver
from codewiki.src.be.generation_runtime import source_is_confirmed_missing

# Non-graph files the reader may surface on a direct path request (MyBatis
# XML, configs under resources): cap so a generated file cannot flood a turn.
_EXTERNAL_FILE_CAP = 200_000
_DIRECTORY_ENTRY_CAP = 200


def _looks_like_path(requested: str) -> bool:
    file_part = requested.replace("\\", "/").partition("::")[0]
    if not file_part or "::" in file_part:
        return False
    return "/" in file_part or "." in Path(file_part).name


def _inspect_filesystem(deps, requested: str):
    """Deterministically classify a path-like request against the repository.

    Equivalent to the evidence an explicit ``str_replace_editor view`` would
    establish. An agent that keeps repeating an unverified path otherwise burns
    its whole request budget on ModelRetry (observed: three continuation rounds
    on a hallucinated controller and on a resource directory, then page
    failure). Returns ``None`` for bare symbols, missing repo metadata, or paths
    escaping the repository — those stay on the candidate/ModelRetry path.

    Returns ``(kind, repo_relative, payload)`` where kind is ``missing``,
    ``file`` or ``directory``.
    """
    if not _looks_like_path(requested):
        return None
    repo_root = getattr(deps, "absolute_repo_path", None)
    if not repo_root:
        return None
    file_part = requested.replace("\\", "/").partition("::")[0]
    try:
        base = Path(repo_root).resolve()
        target = (base / file_part).resolve()
        target.relative_to(base)
    except (OSError, ValueError):
        return None
    relative = target.relative_to(base).as_posix()
    if target.is_file():
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if len(content) > _EXTERNAL_FILE_CAP:
            content = (
                content[:_EXTERNAL_FILE_CAP]
                + "\n... [file truncated by read_code_components; use view ranges if you need the tail]"
            )
        return "file", relative, content
    if target.is_dir():
        entries = []

        def walk(directory: Path, depth: int):
            if depth > 2 or len(entries) >= _DIRECTORY_ENTRY_CAP:
                return
            for child in sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name)):
                if len(entries) >= _DIRECTORY_ENTRY_CAP:
                    return
                if child.name.startswith(".") and child.name != ".github":
                    continue
                rel = child.relative_to(target).as_posix()
                entries.append(rel + ("/" if child.is_dir() else ""))
                if child.is_dir():
                    walk(child, depth + 1)

        walk(target, 1)
        listing = "\n".join(entries)
        if len(entries) >= _DIRECTORY_ENTRY_CAP:
            listing += "\n... [listing truncated]"
        return "directory", relative, listing
    return "missing", relative, None


async def read_code_components(ctx: RunContext[CodeWikiDeps], component_ids: list[str]) -> str:
    """Read the code of a given component id

    Args:
        component_ids: Exact file_path::symbol IDs or repository-relative file paths.
            File paths expand to every parsed component in that file. Paths the
            parser did not index (absent paths, resource directories, non-code
            files like MyBatis XML) are checked against the repository directly.
    """

    components = ctx.deps.components
    resolver = ComponentIdResolver(components)
    by_file = {}
    for cid, node in components.items():
        by_file.setdefault(node.relative_path.replace("\\", "/"), []).append(cid)
    resolved_ids = []
    results: list[str] = []
    recovered = set()
    for requested in component_ids:
        file_ids = by_file.get(requested.replace("\\", "/")) if "::" not in requested else None
        expanded = file_ids or resolver.expand(requested)
        for component_id in expanded:
            resolved = resolver.resolve(component_id)
            candidates = []
            if resolved is None:
                symbol = component_id.rsplit("::", 1)[-1]
                candidates = [key for key in components if key.rsplit("::", 1)[-1] == symbol]
                if len(candidates) == 1:
                    resolved = candidates[0]
            if resolved is None or not components[resolved].source_code:
                # Deterministic filesystem evidence for path-like requests:
                # the same conclusion a repo `view` would reach, delivered
                # without another retry round. Bare symbols still ModelRetry.
                inspection = _inspect_filesystem(ctx.deps, requested)
                if inspection is not None:
                    kind, relative, payload = inspection
                    unresolved = getattr(ctx.deps, "unresolved_components", None)
                    if kind == "missing":
                        missing = getattr(ctx.deps, "source_files_missing", None)
                        if missing is not None:
                            missing.add(relative)
                        if unresolved is not None:
                            unresolved.discard(requested)
                        results.append(
                            f"# File not found (verified on disk): {relative}\n"
                            "A direct filesystem inspection confirmed this path does not exist in "
                            "the repository. Do not guess this ID or claim its behaviour; state that "
                            "the implementation is unavailable, and rely on verified imports/call sites."
                        )
                    elif kind == "file":
                        files_read = getattr(ctx.deps, "source_files_read", None)
                        if files_read is not None:
                            files_read.add(relative)
                        if unresolved is not None:
                            unresolved.discard(requested)
                        language = relative.rsplit(".", 1)[-1]
                        results.append(
                            f"# Unindexed repository file: {relative}\n"
                            "This file exists on disk but was not parsed into the component graph "
                            "(e.g. a mapper XML or resource file). Its content is provided in full:\n"
                            f"```{language}\n{payload}\n```"
                        )
                    else:
                        if unresolved is not None:
                            unresolved.discard(requested)
                        results.append(
                            f"# Directory (no parsed components): {relative}\n"
                            "This path is a directory, not a source component. Inspect a specific "
                            "file with str_replace_editor working_dir='repo', command='view'; do not "
                            "cite the directory itself as an implementation.\n" + (payload or "")
                        )
                    recovered.add(requested)
                    continue
                confirmed_missing = source_is_confirmed_missing(
                    ctx.deps, requested.partition("::")[0]
                )
                unresolved = getattr(ctx.deps, "unresolved_components", None)
                if unresolved is not None:
                    if confirmed_missing:
                        unresolved.discard(requested)
                    else:
                        unresolved.add(requested)
                guidance = (
                    "This exact path was inspected and does not exist in the repository. "
                    "Do not guess another local ID; use verified imports and call sites, "
                    "and state that the implementation is unavailable."
                    if confirmed_missing
                    else "Use an exact repository-relative file path or file_path::symbol ID; "
                    "use str_replace_editor working_dir='repo', command='view' to inspect the file."
                )
                raise ModelRetry(
                    f"Read request {requested!r} is unresolved, ambiguous or has no source. "
                    f"Actual graph ID candidates: {candidates[:20]}. " + guidance
                )
            resolved_ids.append(resolved)
        recovered.add(requested)
    resolved_ids = list(dict.fromkeys(resolved_ids))
    results = [
        f"# Component {cid}:\n{(components[cid].source_code or '').strip()}\n\n"
        for cid in resolved_ids
    ] + results
    reads = getattr(ctx.deps, "source_reads", None)
    if reads is not None:
        reads.update(resolved_ids)
    unresolved = getattr(ctx.deps, "unresolved_components", None)
    if unresolved is not None:
        unresolved.difference_update(recovered | set(resolved_ids))
        for item in list(unresolved):
            file_ids = by_file.get(item.replace("\\", "/")) if "::" not in item else None
            expanded = resolver.expand(item)
            actual = {resolver.resolve(cid) for cid in expanded}
            if (file_ids and set(file_ids) <= (reads or set(resolved_ids))) or (
                None not in actual and actual <= (reads or set(resolved_ids))
            ):
                unresolved.discard(item)
    return "\n".join(results)


read_code_components_tool = Tool(
    function=read_code_components,
    name="read_code_components",
    description="Read source using exact file_path::symbol IDs or repository-relative file paths; file paths expand to all parsed components in a file.",
    takes_ctx=True,
)
