from dataclasses import dataclass, field
from typing import Any

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.config import Config


@dataclass
class CodeWikiDeps:
    absolute_docs_path: str
    absolute_repo_path: str
    registry: dict
    components: dict[str, Node]
    path_to_current_module: list[str]
    current_module_name: str
    module_tree: dict[str, Any]
    max_depth: int
    current_depth: int
    config: Config  # LLM configuration
    custom_instructions: str | None = None
    # Incremental updates: when set, ``str_replace_editor`` refuses any write
    # (create / str_replace / insert / undo_edit) to a docs file whose
    # absolute, resolved path is not in this set. ``None`` = unrestricted.
    allowed_write_paths: set[str] | None = None
    generation_runtime: object | None = None
    source_reads: set[str] = field(default_factory=set)
    source_files_read: set[str] = field(default_factory=set)
    source_files_missing: set[str] = field(default_factory=set)
    unresolved_components: set[str] = field(default_factory=set)
    business_analysis: bool = False
    required_source_ids: set[str] = field(default_factory=set)
    # Out-param of format_user_prompt: dependency component IDs that did not
    # fit the inline budget and must still be read before the page is valid.
    # Direct-write workflow is only safe while this set stays empty.
    deferred_source_ids: set[str] = field(default_factory=set)
