# pyright: reportUnusedFunction=none
# ``generate_command`` exceeds basedpyright's complexity budget, so its body is
# not analyzed and the helpers it calls look unused to the checker. They are
# not.
"""
Generate command for documentation generation.
"""

import logging
import os
import sys
import time
import traceback
from pathlib import Path

import click

from codewiki.cli.adapters.doc_generator import CLIDocumentationGenerator
from codewiki.cli.config_manager import ConfigManager
from codewiki.cli.models.config import AgentInstructions
from codewiki.cli.utils.branding import print_banner
from codewiki.cli.utils.errors import (
    EXIT_SUCCESS,
    APIError,
    ConfigurationError,
    IncompleteGenerationError,
    RepositoryError,
    handle_error,
)
from codewiki.cli.utils.instructions import display_post_generation_instructions
from codewiki.cli.utils.logging import create_logger
from codewiki.src.be.doc_layout import find_doc, list_doc_files, read_layout, remove_doc
from codewiki.src.config import DEFAULT_LAYOUT, DEFAULT_OUTPUT_DIR, LAYOUT_FLAT
from codewiki.src.language import resolve_update_language
from codewiki.cli.utils.repo_validator import (
    check_writable_output,
    get_git_commit_hash,
    is_git_repository,
    validate_repository,
)


def parse_patterns(patterns_str: str) -> list[str]:
    """Parse comma-separated patterns into a list."""
    if not patterns_str:
        return []
    return [p.strip() for p in patterns_str.split(",") if p.strip()]


def _read_stored_language(output_dir: Path) -> str | None:
    """Language recorded in metadata.json by the previous generation (None = English)."""
    import json

    try:
        metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
        return (metadata.get("generation_info") or {}).get("language")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


# Human-readable labels for the team prompt asset fields, in report order.
_ASSET_FIELD_LABELS = {
    "language": "语言",
    "doc_type": "文档类型",
    "focus_modules": "重点模块",
    "include_patterns": "包含模式",
    "exclude_patterns": "排除模式",
    "artifact_exclude": "辅助文件排除",
    "custom_instructions": "正文约定",
}


def _describe_asset_fields(instructions: dict) -> str:
    """One-line summary of the fields that took effect, e.g. 语言 zh、排除 2 项."""
    parts = []
    for key, label in _ASSET_FIELD_LABELS.items():
        value = instructions.get(key)
        if value in (None, "", [], {}):
            continue
        if key == "custom_instructions":
            parts.append(f"{label} {len(str(value))} 字")
        elif isinstance(value, list):
            parts.append(f"{label} {len(value)} 项")
        else:
            parts.append(f"{label} {value}")
    return "、".join(parts) if parts else "无"


def report_team_prompt_asset(asset) -> None:
    """Print whether the team prompt file took effect — always, not just verbose.

    A test runner must never have to guess whether ``.codewiki/prompts.md``
    was applied, and a mistyped field or broken YAML must not pass unnoticed.
    """
    from codewiki.src.prompt_assets import (
        STATUS_ABSENT,
        STATUS_LOADED,
        STATUS_PARTIAL,
        STRUCTURED_KEYS,
    )

    if asset.status == STATUS_ABSENT:
        return
    name = ".codewiki/prompts.md"
    if asset.status == STATUS_LOADED:
        click.secho(
            f"✓ 已加载团队提示词 {name}:{_describe_asset_fields(asset.instructions or {})}",
            fg="green",
        )
        return
    if asset.status == STATUS_PARTIAL:
        problems = []
        if asset.reason:
            problems.append(asset.reason)
        if asset.ignored:
            problems.append(
                "无法识别的字段:"
                + ", ".join(asset.ignored)
                + "（可用:"
                + ", ".join(STRUCTURED_KEYS)
                + "）"
            )
        click.secho(
            f"⚠ 团队提示词 {name} 部分生效:"
            f"{_describe_asset_fields(asset.instructions or {})}。" + ";".join(problems),
            fg="yellow",
        )
        return
    click.secho(
        f"⚠ 团队提示词 {name} 未生效:{asset.reason or '原因未知'}。本次生成未使用团队约定。",
        fg="yellow",
    )


def check_prompt_asset_change(repo_path: Path, output_dir: Path) -> dict[str, str | None]:
    """Return the detection and user's action; baseline old metadata silently."""
    from codewiki.src.prompt_assets import (
        STATUS_CHANGED,
        detect_prompt_change,
        record_prompt_baseline,
    )

    change = detect_prompt_change(str(repo_path), str(output_dir))
    result = {
        "status": change.status,
        "stored": change.stored,
        "current": change.current,
        "action": "ok",
    }
    if change.status != STATUS_CHANGED:
        record_prompt_baseline(str(output_dir), change)
        return result
    click.secho(
        "\n⚠ 团队提示词已变更(上次生成后 .codewiki/prompts.md 有改动)。\n"
        "  增量更新只重写受代码变更影响的页面,未受影响的页面仍按旧约定编写,\n"
        "  同一份文档会出现新旧约定混排。",
        fg="yellow",
    )
    if click.confirm("要全量重新生成吗?", default=False):
        result["action"] = "full"
    else:
        click.secho("  继续增量更新。此提醒会持续到全量重新生成。", fg="yellow")
        result["action"] = "declined"
    return result


def _detect_changed_files(
    repo_path: Path, output_dir: Path, logger, verbose: bool, compare_to: str | None = None
) -> list[str] | None:
    """
    Detect files changed since the last documentation generation.

    Reads the commit_id from metadata.json and compares with current HEAD
    using git diff. When running inside a subdirectory of a monorepo,
    only files under that subdirectory are returned.

    Returns list of changed file paths relative to repo_path, or None if
    unable to determine (e.g., no metadata, not a git repo).
    """
    import json

    prev_commit = None
    if compare_to:
        prev_commit = compare_to
    else:
        metadata_path = output_dir / "metadata.json"
        if not metadata_path.exists():
            if verbose:
                logger.debug(
                    "No metadata.json found — cannot detect changes, running full generation."
                )
            return None

        try:
            metadata = json.loads(metadata_path.read_text())
            prev_commit = metadata.get("generation_info", {}).get("commit_id")
            if not prev_commit:
                if verbose:
                    logger.debug("No commit_id in metadata — running full generation.")
                return None
        except (json.JSONDecodeError, OSError):
            return None

    # Get current HEAD commit
    try:
        import git

        repo = git.Repo(repo_path, search_parent_directories=True)
        current_commit = repo.head.commit.hexsha
    except Exception:  # noqa: BLE001 — any git failure means "no incremental update"
        if verbose:
            logger.debug("Cannot access git repo — running full generation.")
        return None

    if prev_commit == current_commit:
        if verbose:
            logger.debug(f"HEAD is still at {current_commit[:8]} — no changes.")
        return []

    # Determine subdirectory prefix relative to the git root
    if repo.working_tree_dir is None:
        if verbose:
            logger.debug("Bare git repository — running full generation.")
        return None
    git_root = Path(repo.working_tree_dir).resolve()
    repo_path_resolved = repo_path.resolve()
    try:
        subpath_prefix = repo_path_resolved.relative_to(git_root).as_posix()
    except ValueError:
        # repo_path is outside git root — shouldn't happen, but fall back to full generation
        if verbose:
            logger.debug("Repo path is outside git root — running full generation.")
        return None

    # Get changed files between previous and current commit
    try:
        diff_index = repo.commit(prev_commit).diff(current_commit)
        changed = []
        for diff in diff_index:
            if diff.a_path:
                changed.append(diff.a_path)
            if diff.b_path and diff.b_path != diff.a_path:
                changed.append(diff.b_path)

        # Filter to files under the current subdirectory and strip the prefix
        # so paths align with module_tree.json component paths
        filtered = []
        if subpath_prefix == ".":
            filtered = changed
        else:
            prefix = subpath_prefix + "/"
            for path in changed:
                if path.startswith(prefix):
                    filtered.append(path[len(prefix) :])

        if verbose:
            logger.debug(f"Changes between {prev_commit[:8]} and {current_commit[:8]}:")
            if subpath_prefix != ".":
                logger.debug(f"  Scoped to subdirectory: {subpath_prefix}")
            for f in filtered[:10]:
                logger.debug(f"  {f}")
            if len(filtered) > 10:
                logger.debug(f"  ... and {len(filtered) - 10} more")

        return filtered
    except Exception as e:  # noqa: BLE001 — fall back to regenerating everything
        if verbose:
            logger.debug(f"Git diff failed: {e} — running full generation.")
        return None


def _invalidate_affected_modules(output_dir: Path, changed_files: list[str], logger, verbose: bool):
    """
    Remove cached module documentation for modules that contain changed files.

    Reads module_tree.json to find which modules contain changed files,
    then deletes their .md files so they get regenerated.
    """
    import json

    module_tree_path = output_dir / "module_tree.json"
    if not module_tree_path.exists():
        return

    try:
        module_tree = json.loads(module_tree_path.read_text())
    except (json.JSONDecodeError, OSError):
        return

    changed_set = set(changed_files)
    modules_to_invalidate = set()

    def _find_affected(tree, parent_names=None):
        if parent_names is None:
            parent_names = []
        for mod_name, mod_info in tree.items():
            components = mod_info.get("components", [])
            # Check if any component path overlaps with changed files
            for comp in components:
                # Component IDs may be class names, check if they match any changed file path
                if any(
                    changed_file in comp or comp in changed_file for changed_file in changed_set
                ):
                    modules_to_invalidate.add(mod_name)
                    # Also invalidate parent modules
                    for parent in parent_names:
                        modules_to_invalidate.add(parent)
                    break

            children = mod_info.get("children", {})
            if isinstance(children, dict) and children:
                _find_affected(children, parent_names + [mod_name])

    _find_affected(module_tree)

    # Also remove overview.md since it depends on child docs
    if modules_to_invalidate:
        modules_to_invalidate.add("overview")

    # Delete affected module docs (wherever the docs layout put them)
    for mod_name in modules_to_invalidate:
        doc_path = find_doc(str(output_dir), mod_name, module_tree)
        if doc_path is not None:
            remove_doc(str(output_dir), doc_path)
            if verbose:
                logger.debug(f"Invalidated: {os.path.relpath(doc_path, output_dir)}")

    if verbose:
        logger.debug(f"Invalidated {len(modules_to_invalidate)} modules for regeneration.")


@click.command(name="generate")
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=DEFAULT_OUTPUT_DIR,
    help="Output directory for generated documentation (default: ./.codewiki)",
)
@click.option(
    "--create-branch",
    is_flag=True,
    help="Create a new git branch for documentation changes",
)
@click.option(
    "--github-pages",
    is_flag=True,
    help="Generate index.html for GitHub Pages deployment",
)
@click.option(
    "--no-cache",
    is_flag=True,
    help="Force full regeneration, ignoring cache",
)
@click.option(
    "--include",
    "-i",
    type=str,
    default=None,
    help="Comma-separated file patterns to include (e.g., '*.cs,*.py'). Overrides defaults.",
)
@click.option(
    "--exclude",
    "-e",
    type=str,
    default=None,
    help="Comma-separated patterns to exclude (e.g., '*Tests*,*Specs*,test_*')",
)
@click.option(
    "--focus",
    "-f",
    type=str,
    default=None,
    help="Comma-separated modules/paths to focus on (e.g., 'src/core,src/api')",
)
@click.option(
    "--doc-type",
    "-t",
    type=click.Choice(["api", "architecture", "user-guide", "developer"], case_sensitive=False),
    default=None,
    help="Type of documentation to generate",
)
@click.option(
    "--language",
    "-l",
    type=str,
    default=None,
    help="Language of the generated documentation, as a code or name (e.g. 'ja', 'Japanese', 'vi'). "
    "Chinese output also names new modules, files and folders in Chinese; assigned names stay fixed. "
    "Default: English, or the language stored in "
    "metadata.json when used with --update",
)
@click.option(
    "--use-gitignore/--no-gitignore",
    default=None,
    help="Apply Git ignore rules during analysis (default: enabled)",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show detailed progress and debug information",
)
@click.option(
    "--max-tokens",
    type=int,
    default=None,
    help="Maximum tokens for LLM response (overrides config)",
)
@click.option(
    "--max-token-per-module",
    type=int,
    default=None,
    help="Maximum tokens per module for clustering (overrides config)",
)
@click.option(
    "--max-token-per-leaf-module",
    type=int,
    default=None,
    help="Maximum tokens per leaf module (overrides config)",
)
@click.option(
    "--max-depth",
    type=int,
    default=None,
    help="Maximum depth for hierarchical decomposition (overrides config)",
)
@click.option(
    "--request-limit",
    type=click.IntRange(min=1),
    default=None,
    help="Maximum model requests per agent run (overrides config)",
)
@click.option(
    "--concurrency",
    type=click.IntRange(min=1),
    default=None,
    help="API generation concurrency; 1 runs serially. Defaults to config (2). "
    "Subscription providers remain serial.",
)
@click.option(
    "--trace/--no-trace",
    default=None,
    help="Print stage and task timings. Defaults to config (enabled).",
)
@click.option(
    "--agent-retries",
    type=click.IntRange(min=0),
    default=None,
    help="Retries for a failing agent tool call (overrides config)",
)
@click.option(
    "--prompt-caching/--no-prompt-caching",
    default=None,
    help="Add prompt-cache breakpoints to agentic LLM calls; auto-falls back to "
    "normal calls if the provider rejects them (default: enabled)",
)
@click.option(
    "--artifacts/--no-artifacts",
    default=True,
    help="Document build, CI, container, packaging, manifest, config, schema and "
    "script files as part of the dependency graph (default: enabled)",
)
@click.option(
    "--artifact-token-budget",
    type=int,
    default=200_000,
    show_default=True,
    help="Total token budget for artifact file contents added to the graph",
)
@click.option(
    "--with-prose",
    is_flag=True,
    help="Also read the root README and docs/ as a `prose` artifact class (off by default)",
)
@click.option(
    "--flat",
    is_flag=True,
    help="Save every page in the output directory root instead of folders mirroring the "
    "module tree (for small models that struggle with relative links). --update keeps the "
    "layout the docs were generated with",
)
@click.option(
    "--artifact-exclude",
    type=str,
    default=None,
    help="Comma-separated patterns skipped by artifact analysis (e.g. 'docker/data/*,config/generated/*')",
)
@click.option(
    "--update",
    is_flag=True,
    help="Incremental update: only regenerate modules affected by changes since last generation",
)
@click.option(
    "--update-rung",
    type=click.Choice(["0", "1", "2", "3", "3b"]),
    default="3",
    show_default=True,
    help=(
        "Incremental updater variant used with --update. 0 = legacy file-level "
        "invalidation; 1-3 = component-level updater ablation rungs; 3b = rung 3 with 2 hops."
    ),
)
@click.option(
    "--tau-ren", type=float, default=None, help="Rename similarity threshold (default 0.95)."
)
@click.option(
    "--tau-nb", type=float, default=None, help="Neighbour-majority routing share (default 0.5)."
)
@click.option(
    "--tau-grow",
    type=float,
    default=None,
    help="Leaf growth share that re-clusters (default 0.33).",
)
@click.option(
    "--tau-full",
    type=float,
    default=None,
    help="Active-leaf share that forces a full build (default 0.5).",
)
@click.option(
    "--tau-tree",
    type=float,
    default=None,
    help="Structural-change share that forces a full build (default 0.3).",
)
@click.option(
    "--k-hop", type=int, default=None, help="Dependency hops followed for Up (default 1)."
)
@click.option(
    "--max-diff-tokens",
    type=int,
    default=None,
    help="Cap on one component diff in a report (default 8000).",
)
@click.option(
    "--no-ownership-closure",
    is_flag=True,
    default=False,
    help=(
        "Do not give changed components that no module tracks an effective owner by the "
        "placement rules; such changes then reach no page (default: give one)."
    ),
)
@click.option(
    "--compare-to",
    type=str,
    default=None,
    help="Commit hash to compare against for incremental updates (overrides stored commit in metadata.json)",
)
@click.pass_context
def generate_command(  # pyright: ignore[reportGeneralTypeIssues]
    ctx,
    output: str,
    create_branch: bool,
    github_pages: bool,
    no_cache: bool,
    include: str | None,
    exclude: str | None,
    focus: str | None,
    doc_type: str | None,
    language: str | None,
    use_gitignore: bool | None,
    verbose: bool,
    max_tokens: int | None,
    max_token_per_module: int | None,
    max_token_per_leaf_module: int | None,
    max_depth: int | None,
    request_limit: int | None,
    agent_retries: int | None,
    prompt_caching: bool | None,
    concurrency: int | None = None,
    trace: bool | None = None,
    artifacts: bool = True,
    artifact_token_budget: int = 200_000,
    with_prose: bool = False,
    flat: bool = False,
    artifact_exclude: str | None = None,
    update: bool = False,
    compare_to: str | None = None,
    update_rung: str = "3",
    tau_ren: float | None = None,
    tau_nb: float | None = None,
    tau_grow: float | None = None,
    tau_full: float | None = None,
    tau_tree: float | None = None,
    k_hop: int | None = None,
    max_diff_tokens: int | None = None,
    no_ownership_closure: bool = False,
):
    """
    Generate comprehensive documentation for a code repository.

    Analyzes the current repository and generates documentation using LLM-powered
    analysis. Documentation is output to ./.codewiki/ by default.

    Examples:

    \b
    # Basic generation
    $ codewiki generate

    \b
    # With git branch creation and GitHub Pages
    $ codewiki generate --create-branch --github-pages

    \b
    # Force full regeneration
    $ codewiki generate --no-cache

    \b
    # Analyze ignored files as well
    $ codewiki generate --no-gitignore

    \b
    # C# project: only .cs files, exclude tests
    $ codewiki generate --include "*.cs" --exclude "*Tests*,*Specs*"

    \b
    # Focus on specific modules with architecture docs
    $ codewiki generate --focus "src/core,src/api" --doc-type architecture

    \b
    # Override max tokens for this generation
    $ codewiki generate --max-tokens 16384

    \b
    # Set all max token limits
    $ codewiki generate --max-tokens 32768 --max-token-per-module 40000 --max-token-per-leaf-module 20000

    \b
    # Override max depth for hierarchical decomposition
    $ codewiki generate --max-depth 3
    """
    print_banner()
    logger = create_logger(verbose=verbose)
    start_time = time.time()

    # Suppress httpx INFO logs
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        # Pre-generation checks
        logger.step("正在校验配置...", 1, 4)

        # Load configuration
        config_manager = ConfigManager()
        if not config_manager.load():
            raise ConfigurationError(
                "未找到配置或配置无效。\n\n"
                "请先运行 'codewiki config set' 配置模型服务：\n"
                "  codewiki config set --api-key <你的密钥> --base-url <服务地址> \\\n"
                "    --main-model <主模型> --cluster-model <聚类模型>\n\n"
                "更多帮助：codewiki config --help"
            )

        if not config_manager.is_configured():
            raise ConfigurationError("配置不完整，请运行 'codewiki config validate' 检查")

        config = config_manager.get_config()
        api_key = config_manager.get_api_key()

        logger.success("配置有效")

        # Validate repository
        logger.step("正在校验仓库...", 2, 4)

        repo_path = Path.cwd()
        repo_path, languages = validate_repository(repo_path)

        logger.success(f"仓库有效：{repo_path.name}")
        if verbose:
            logger.debug(
                f"检测到语言：{', '.join(f'{lang}（{count} 个文件）' for lang, count in languages)}"
            )

        # Check git repository
        if not is_git_repository(repo_path):
            if create_branch:
                raise RepositoryError(
                    "当前不是 Git 仓库。\n\n--create-branch 需要 Git 仓库。\n\n初始化方法：git init"
                )
            else:
                logger.warning("当前不是 Git 仓库，Git 相关功能不可用。")

        # Validate output directory
        from codewiki.src.be.doc_layout import resolve_output_dir

        explicit_output = click.get_current_context().get_parameter_source("output")
        output_dir = resolve_output_dir(
            repo_path,
            output if explicit_output != click.core.ParameterSource.DEFAULT else None,
        )
        check_writable_output(output_dir.parent)

        logger.success(f"输出目录：{output_dir}")

        # If a base commit is specified to compare against, implicitly enable update
        if compare_to:
            update = True

        # Incremental update: detect changed files and selectively regenerate
        changed_files = None
        update_language = None
        updating_existing_docs = update and (output_dir / "metadata.json").exists()
        prompt_asset_declined = False
        full_regenerate = False
        prompt_asset_change = {}
        if updating_existing_docs:
            # Keep updated pages in the language of the existing docs
            try:
                update_language = resolve_update_language(
                    _read_stored_language(output_dir), language
                )
            except ValueError as e:
                raise ConfigurationError(str(e)) from None
            # The team conventions may have moved without any code change;
            # check before the "no changes" exit can hide it.
            prompt_asset_change = check_prompt_asset_change(repo_path, output_dir)
            asset_action = prompt_asset_change["action"]
            if asset_action == "full":
                update = False
                full_regenerate = True
                updating_existing_docs = False
                update_language = None
            elif asset_action == "declined":
                prompt_asset_declined = True
        layout = LAYOUT_FLAT if flat else DEFAULT_LAYOUT
        if updating_existing_docs:
            # Keep updated pages where the existing docs put them
            stored_layout = read_layout(str(output_dir))
            if flat and stored_layout != LAYOUT_FLAT:
                logger.warning(
                    f"These docs use the {stored_layout} layout; --flat is ignored by --update. "
                    "Rerun without --update to regenerate them flat."
                )
            layout = stored_layout
        if update and output_dir.exists():
            changed_files = _detect_changed_files(
                repo_path, output_dir, logger, verbose, compare_to=compare_to
            )
            if changed_files is not None and len(changed_files) == 0:
                from codewiki.src.be.updater.record import UpdateRecord, merge_into_metadata
                import json

                metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
                previous_commit = compare_to or (metadata.get("generation_info") or {}).get(
                    "commit_id"
                )
                record = UpdateRecord(
                    outcome="no_change",
                    finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    options={"rung": update_rung},
                    revision={
                        "old_commit": previous_commit,
                        "new_commit": get_git_commit_hash(repo_path),
                        "repo_path": str(repo_path),
                    },
                    prompt_asset_change={**prompt_asset_change, "execution": "no_change"},
                )
                record.save(str(output_dir))
                merge_into_metadata(str(output_dir), record.summary())
                if prompt_asset_declined:
                    logger.warning(
                        "代码无变更,但团队提示词已变更且未全量重新生成;既有页面仍按旧约定编写。"
                    )
                else:
                    logger.success(
                        "No changes detected since last generation. Documentation is up to date."
                    )
                sys.exit(EXIT_SUCCESS)
            if changed_files is not None and update_rung == "0":
                logger.info(
                    f"  Detected {len(changed_files)} changed files — regenerating affected modules."
                )
                # Remove cached module docs for affected files so they get regenerated
                _invalidate_affected_modules(output_dir, changed_files, logger, verbose)
            elif update_rung != "0":
                if changed_files is not None:
                    logger.info(
                        f"  Detected {len(changed_files)} changed files — running the "
                        f"component-level updater (rung {update_rung})."
                    )
                else:
                    logger.info(
                        f"  Git diff unavailable — the component-level updater (rung {update_rung}) "
                        f"will compare the saved dependency graph instead."
                    )

        # Check for existing documentation
        if not update and output_dir.exists() and list_doc_files(str(output_dir)):
            if not full_regenerate and not click.confirm(
                f"\n{output_dir} 中已存在文档。\n"
                "  · 选 y：全量重新生成（已生成的页面会被覆盖）\n"
                "  · 选 n：取消本次运行\n"
                "如需只更新受代码变更影响的页面，请改用 --update。",
                default=True,
            ):
                logger.info("用户取消了生成。")
                sys.exit(EXIT_SUCCESS)
            if not full_regenerate:
                from codewiki.src.prompt_assets import STATUS_CHANGED, detect_prompt_change

                full_regenerate = (
                    detect_prompt_change(str(repo_path), str(output_dir)).status == STATUS_CHANGED
                )

        # Git branch creation (if requested)
        branch_name = None
        if create_branch:
            logger.step("Creating git branch...", 3, 4)

            from codewiki.cli.git_manager import GitManager

            git_manager = GitManager(repo_path)

            # Check clean working directory
            is_clean, status_msg = git_manager.check_clean_working_directory()
            if not is_clean:
                raise RepositoryError(
                    "Working directory has uncommitted changes.\n\n"
                    f"{status_msg}\n\n"
                    "Cannot create documentation branch with uncommitted changes.\n"
                    "Please commit or stash your changes first:\n"
                    '  git add -A && git commit -m "Your message"\n'
                    "  # or\n"
                    "  git stash"
                )

            # Create branch
            branch_name = git_manager.create_documentation_branch()
            logger.success(f"Created branch: {branch_name}")

        # Generate documentation
        logger.step("正在生成文档...", 4, 4)
        click.echo()

        # Create runtime agent instructions from CLI options
        runtime_instructions = None
        if any([include, exclude, focus, doc_type, artifact_exclude, language]):
            runtime_instructions = AgentInstructions(
                include_patterns=parse_patterns(include) if include else None,
                exclude_patterns=parse_patterns(exclude) if exclude else None,
                focus_modules=parse_patterns(focus) if focus else None,
                doc_type=doc_type,
                artifact_exclude=parse_patterns(artifact_exclude) if artifact_exclude else None,
                language=language,
            )

            if verbose:
                if include:
                    logger.debug(f"Include patterns: {parse_patterns(include)}")
                if exclude:
                    logger.debug(f"Exclude patterns: {parse_patterns(exclude)}")
                if focus:
                    logger.debug(f"Focus modules: {parse_patterns(focus)}")
                if doc_type:
                    logger.debug(f"Doc type: {doc_type}")
                if artifact_exclude:
                    logger.debug(f"Artifact exclude patterns: {parse_patterns(artifact_exclude)}")
                if language:
                    logger.debug(f"Output language: {language}")

        # Log max token settings if verbose
        if verbose:
            effective_max_tokens = max_tokens if max_tokens is not None else config.max_tokens
            effective_max_token_per_module = (
                max_token_per_module
                if max_token_per_module is not None
                else config.max_token_per_module
            )
            effective_max_token_per_leaf = (
                max_token_per_leaf_module
                if max_token_per_leaf_module is not None
                else config.max_token_per_leaf_module
            )
            effective_max_depth = max_depth if max_depth is not None else config.max_depth
            effective_use_gitignore = (
                use_gitignore if use_gitignore is not None else config.use_gitignore
            )
            effective_prompt_caching = (
                prompt_caching if prompt_caching is not None else config.prompt_caching
            )
            logger.debug(f"Max tokens: {effective_max_tokens}")
            logger.debug(f"Max token/module: {effective_max_token_per_module}")
            logger.debug(f"Max token/leaf module: {effective_max_token_per_leaf}")
            logger.debug(f"Max depth: {effective_max_depth}")
            logger.debug(f"Use gitignore: {effective_use_gitignore}")
            logger.debug(f"Prompt caching: {effective_prompt_caching}")
            logger.debug(
                f"Artifacts: {artifacts} (token budget {artifact_token_budget}, prose {with_prose})"
            )

        # Get agent instructions (merge runtime with persistent)
        agent_instructions_dict = None
        if runtime_instructions and not runtime_instructions.is_empty():
            # Merge with persistent settings
            merged = runtime_instructions.merged_with(config.agent_instructions)
            agent_instructions_dict = merged.to_dict()
        elif config.agent_instructions and not config.agent_instructions.is_empty():
            agent_instructions_dict = config.agent_instructions.to_dict()

        if updating_existing_docs:
            # The stored language overrides the saved `config agent --language` default
            agent_instructions_dict = dict(agent_instructions_dict or {})
            agent_instructions_dict.pop("language", None)
            if update_language:
                agent_instructions_dict["language"] = update_language
            if verbose:
                logger.debug(
                    f"Output language (from metadata.json): {update_language or 'English'}"
                )

        # A repository prompt asset (<repo>/.codewiki/prompts.md) is a team
        # decision and replaces the same prompt field from config or CLI flags.
        # The built-in system prompt is untouched either way.
        from codewiki.src.prompt_assets import resolve_prompt_instructions

        agent_instructions_dict, prompt_asset = resolve_prompt_instructions(
            str(repo_path),
            agent_instructions_dict,
        )
        report_team_prompt_asset(prompt_asset)

        # Create generator
        # Get commit_id early so it can be stored in metadata.json for --update support
        commit_id = get_git_commit_hash(repo_path)
        generator = CLIDocumentationGenerator(
            repo_path=repo_path,
            output_dir=output_dir,
            config={
                "main_model": config.main_model,
                "cluster_model": config.cluster_model,
                "fallback_model": config.fallback_model,
                "base_url": config.base_url,
                "api_key": api_key,
                "provider": getattr(config, "provider", "openai-compatible"),
                "aws_region": getattr(config, "aws_region", "us-east-1"),
                "agent_instructions": agent_instructions_dict,
                # Max token settings (runtime overrides take precedence)
                "max_tokens": max_tokens if max_tokens is not None else config.max_tokens,
                "max_token_per_module": max_token_per_module
                if max_token_per_module is not None
                else config.max_token_per_module,
                "max_token_per_leaf_module": max_token_per_leaf_module
                if max_token_per_leaf_module is not None
                else config.max_token_per_leaf_module,
                # Max depth setting (runtime override takes precedence)
                "max_depth": max_depth if max_depth is not None else config.max_depth,
                # Agent run limits (runtime override takes precedence)
                "request_limit": request_limit
                if request_limit is not None
                else config.request_limit,
                "concurrency": concurrency if concurrency is not None else config.concurrency,
                "trace": trace if trace is not None else config.trace,
                "agent_retries": agent_retries
                if agent_retries is not None
                else config.agent_retries,
                # Gitignore setting (runtime override takes precedence)
                "use_gitignore": use_gitignore
                if use_gitignore is not None
                else config.use_gitignore,
                # Prompt caching setting (runtime override takes precedence)
                "prompt_caching": prompt_caching
                if prompt_caching is not None
                else config.prompt_caching,
                # Artifact-aware generation (runtime-only flags)
                "artifacts_enabled": artifacts,
                "artifact_token_budget": artifact_token_budget,
                "with_prose": with_prose,
                # Docs layout (runtime-only; --update keeps the stored one)
                "layout": layout,
                # Incremental updater (runtime-only)
                "update": update,
                "full_regenerate": full_regenerate,
                "prompt_asset_change": prompt_asset_change,
                "update_options": {
                    "rung": update_rung,
                    "tau_ren": tau_ren,
                    "tau_nb": tau_nb,
                    "tau_grow": tau_grow,
                    "tau_full": tau_full,
                    "tau_tree": tau_tree,
                    "k_hop": k_hop,
                    "max_diff_tokens": max_diff_tokens,
                    "use_ownership_closure": False if no_ownership_closure else None,
                },
            },
            verbose=verbose,
            generate_html=github_pages,
            commit_id=commit_id,
        )

        # Run generation
        job = generator.generate()

        # Post-generation
        generation_time = time.time() - start_time

        # Get repository info
        repo_url = None

        if is_git_repository(repo_path):
            try:
                import git

                repo = git.Repo(repo_path)
                if repo.remotes:
                    repo_url = repo.remotes.origin.url
            except Exception:  # noqa: BLE001, S110 — the remote URL is optional
                pass

        # Display instructions
        display_post_generation_instructions(
            output_dir=output_dir,
            repo_name=repo_path.name,
            repo_url=repo_url,
            branch_name=branch_name,
            github_pages=github_pages,
            files_generated=job.files_generated,
            statistics={
                "module_count": job.module_count,
                "total_files_analyzed": job.statistics.total_files_analyzed,
                "generation_time": generation_time,
                "total_tokens_used": job.statistics.total_tokens_used,
            },
        )

    except ConfigurationError as e:
        logger.error(e.message)
        logger.error(f"Traceback: {traceback.format_exc()}")
        sys.exit(e.exit_code)
    except RepositoryError as e:
        logger.error(e.message)
        logger.error(f"Traceback: {traceback.format_exc()}")
        sys.exit(e.exit_code)
    except APIError as e:
        logger.error(e.message)
        logger.error(f"Traceback: {traceback.format_exc()}")
        sys.exit(e.exit_code)
    except IncompleteGenerationError as e:
        click.secho(f"\n✗ {e.message}", fg="red", err=True)
        for module_name in e.missing_modules:
            click.secho(f"  - {module_name}.md", fg="red", err=True)
        click.echo(
            "Re-run the same command to resume generation for the missing modules.",
            err=True,
        )
        sys.exit(e.exit_code)
    except KeyboardInterrupt:
        click.echo("\n\n已被用户中断")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 — top-level CLI error handler
        sys.exit(handle_error(e, verbose=verbose))
