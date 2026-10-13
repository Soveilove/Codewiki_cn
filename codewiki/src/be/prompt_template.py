import logging
from collections import defaultdict
from typing import Any

from codewiki.src.be.doc_layout import doc_relpaths, module_doc_file
from codewiki.src.config import LAYOUT_FLAT
from codewiki.src.language import format_module_naming_directive
from codewiki.src.utils import file_manager

MERMAID_SELF_REVIEW = """
Before writing or returning documentation, self-review Mermaid diagrams in this same generation step. Use simple Mermaid 11.9-compatible syntax; check diagram types, balanced delimiters, quoted labels containing special characters, and relationships against the source code. Do not launch a separate review agent or make extra model calls for this review. If local validation is unavailable or disabled, preserve the diagrams, self-review them and continue; do not retry validation or rewrite solely because the validator is unavailable. Self-review is not actual parsing or rendering verification; do not claim diagrams passed tool validation unless the tool confirmed it.
""".strip()

DELEGATION_WORKFLOW_STEP = (
    "3. Use `generate_sub_module_documentation` to generate detailed sub-modules documentation "
    "for COMPLEX modules which at least have more than 1 code file and are able to clearly split "
    "into sub-modules. Sub-module names must be unique across the whole wiki "
    "({uniqueness_reason}) — prefer names prefixed with the current module name, e.g. "
    "`{module_name}_search`\n"
)
DELEGATION_FINALIZE_STEP = (
    "5. After all sub-modules are documented, adjust `{doc_file}` with ONLY ONE STEP to ensure "
    "all generated files including sub-modules documentation are properly cross-refered, using "
    "the final file names reported by `generate_sub_module_documentation`\n"
)
DELEGATION_TOOL_LINE = (
    "- `generate_sub_module_documentation`: Generate detailed documentation for individual "
    "sub-modules via sub-agents\n"
)
# Workflow step 1 of the module system prompt. Replaced wholesale when every
# required source file is already inlined in the user prompt (see
# _source_complete_fields): "explore first" is what makes an agent re-read
# source it already holds.
EXPLORE_SOURCE_WORKFLOW_STEP = (
    "1. Analyze the provided code components and module structure, explore the "
    "not given dependencies between the components if needed\n"
)
DIRECT_WRITE_WORKFLOW_STEP = (
    "1. Analyze the assigned source already provided in full. Do not retrieve those files "
    "again unless a concrete inconsistency requires checking them. Inspect deferred or "
    "missing dependencies when needed to verify the behaviour you describe; complete assigned "
    "source alone does not establish that every dependency has been inspected. Then create "
    "the required documentation page without redundant exploration.\n"
)

READ_TOOL_LINE = (
    "- `read_code_components`: Explore additional code dependencies not included in the "
    "provided components\n"
)

NO_DELEGATION_FINALIZE_STEP = (
    "5. Review the main documentation file once at the end to confirm every source component is "
    "covered and all cross-references are correct\n"
)

SYSTEM_PROMPT = (
    """
<ROLE>
You are an AI documentation assistant. Your task is to generate comprehensive system documentation based on a given module name and its core code components.
</ROLE>

<OBJECTIVES>
Create documentation that helps developers and maintainers understand:
1. The module's purpose and core functionality
2. Architecture and component relationships
3. How the module fits into the overall system
</OBJECTIVES>

<DOCUMENTATION_STRUCTURE>
Generate documentation following this structure:

1. **Main Documentation File** (`{doc_file}`):
   - Brief introduction and purpose
   - Architecture overview with diagrams
   - High-level functionality of each sub-module including references to its documentation file
   - Link to other module documentation instead of duplicating information

2. **Sub-module Documentation** (if applicable):
   - Detailed descriptions of each sub-module saved {sub_module_location}
   - Core components and their responsibilities

3. **Visual Documentation**:
   - Mermaid diagrams for architecture, dependencies, and data flow
   - Component interaction diagrams
   - Process flow diagrams where relevant
</DOCUMENTATION_STRUCTURE>

<WORKFLOW>
{analyse_step}2. Create the main `{doc_file}` file with overview and architecture in working directory
{delegation_step}4. Include relevant Mermaid diagrams throughout the documentation
{finalize_step}</WORKFLOW>

<AVAILABLE_TOOLS>
- `str_replace_editor`: File system operations for creating and editing documentation files, and (with `working_dir="repo"`, `view` only) for reading build, CI, container, packaging, manifest and config files such as Dockerfile, Makefile, .github/workflows/*.yml, pyproject.toml or package.json
{read_tool_line}{delegation_tool}</AVAILABLE_TOOLS>
{custom_instructions}
""".strip()
    + "\n\n"
    + MERMAID_SELF_REVIEW
)

LEAF_SYSTEM_PROMPT = (
    """
<ROLE>
You are an AI documentation assistant. Your task is to generate comprehensive system documentation based on a given module name and its core code components.
</ROLE>

<OBJECTIVES>
Create a comprehensive documentation that helps developers and maintainers understand:
1. The module's purpose and core functionality
2. Architecture and component relationships
3. How the module fits into the overall system
</OBJECTIVES>

<DOCUMENTATION_REQUIREMENTS>
Generate documentation following the following requirements:
1. Structure: Brief introduction → comprehensive documentation with Mermaid diagrams
2. Diagrams: Include architecture, dependencies, data flow, component interaction, and process flows as relevant
3. References: Link to other module documentation instead of duplicating information
</DOCUMENTATION_REQUIREMENTS>

<WORKFLOW>
{analyse_step}2. Explore dependencies between components if needed
3. Generate complete {doc_file} documentation file
</WORKFLOW>

<AVAILABLE_TOOLS>
- `str_replace_editor`: File system operations for creating and editing documentation files, and (with `working_dir="repo"`, `view` only) for reading build, CI, container, packaging, manifest and config files such as Dockerfile, Makefile, .github/workflows/*.yml, pyproject.toml or package.json
{read_tool_line}</AVAILABLE_TOOLS>
{custom_instructions}
""".strip()
    + "\n\n"
    + MERMAID_SELF_REVIEW
)

USER_PROMPT = """
Generate comprehensive documentation for the {module_name} module using the provided module tree and core components.

<MODULE_TREE>
{module_tree}
</MODULE_TREE>
* NOTE: the tree lists page names and hierarchy only. Where a referenced component is owned by
another page, DEPENDENCY_OWNERSHIP below names the page: link to it instead of restating the
component's behaviour, so the documentation stays structured and de-duplicated.

<CORE_COMPONENT_CODES>
{formatted_core_component_codes}
</CORE_COMPONENT_CODES>
""".strip()

REPO_OVERVIEW_PROMPT = (
    """
You are an AI documentation assistant. Your task is to generate a brief overview of the {repo_name} repository.

The overview should be a brief documentation of the repository, including:
- The purpose of the repository
- The end-to-end architecture of the repository visualized by mermaid diagrams
- The references to the core modules documentation

Provide `{repo_name}` repo structure:
<REPO_STRUCTURE>
{repo_structure}
</REPO_STRUCTURE>

The core modules' documentation is NOT inlined above. Each top-level module carries a `docs_path` field with the absolute path to its documentation file — read those files with your file-reading tools before writing the overview (skip entries whose `docs_path` is null). When referencing a child's documentation, link it with the relative path in its `link` field, e.g. [alt text](<link>).

Please generate the overview of the `{repo_name}` repository in markdown format with the following structure:
<OVERVIEW>
overview_content
</OVERVIEW>
Return only this envelope, without introductory text or an outer Markdown code fence.
The <OVERVIEW> tags are transport delimiters: keep them unchanged and put the page's
Markdown title and all localized prose INSIDE the tags, not before them.
""".strip()
    + "\n\n"
    + MERMAID_SELF_REVIEW
)

MODULE_OVERVIEW_PROMPT = (
    """
You are an AI documentation assistant. Your task is to generate a brief overview of `{module_name}` module.

The overview should be a brief documentation of the module, including:
- The purpose of the module
- The architecture of the module visualized by mermaid diagrams
- The references to the core components documentation

Provide repo structure of the `{module_name}` module (marked with `is_target_for_overview_generation`):
<REPO_STRUCTURE>
{repo_structure}
</REPO_STRUCTURE>

The child modules' documentation is NOT inlined above. Each child of the target module carries a `docs_path` field with the absolute path to its documentation file — read those files with your file-reading tools before writing the overview (skip entries whose `docs_path` is null). When referencing a child's documentation, link it with the relative path in its `link` field, e.g. [alt text](<link>).

Please generate the overview of the `{module_name}` module in markdown format with the following structure:
<OVERVIEW>
overview_content
</OVERVIEW>
Return only this envelope, without introductory text or an outer Markdown code fence.
The <OVERVIEW> tags are transport delimiters: keep them unchanged and put the page's
Markdown title and all localized prose INSIDE the tags, not before them.
""".strip()
    + "\n\n"
    + MERMAID_SELF_REVIEW
)

CLUSTER_REPO_PROMPT = """
Here is the list of potential core components of the repository. All supplied components require an explicit module assignment, even if they are less central:
<POTENTIAL_CORE_COMPONENTS>
{potential_core_components}
</POTENTIAL_CORE_COMPONENTS>

Please group the components into groups such that each group is a set of components that are closely related to each other and together they form a module. Assign EVERY supplied component to a module. Never omit supplied IDs; less central components still need an explicit module assignment.

Files marked `(artifact: <class>)` are build, CI, container, packaging, manifest, configuration, schema or script files. Their components describe how the system is built, packaged, shipped, configured and tested. They ARE essential: group them into a dedicated build/deployment/configuration module, or attach them to the module they configure. Never drop them.

Entries may have the form `C0 = <file_path>::<name>`. When these short references are supplied, return ONLY the left-hand references (e.g. "C0", "C1") in components, never the full paths or names. References are local to this request; use only those in POTENTIAL_CORE_COMPONENTS, not IDs from MODULE_TREE. Assign each supplied reference exactly once; do not use ranges, combine references into one string, or select only representative components. If entries have no short references, return their full `<file_path>::<name>` IDs exactly as supplied. Before returning, verify that the assigned references or IDs cover the entire supplied list.

Reason internally, then return ONLY the following envelope containing valid compact JSON.
Use string paths (file or directory) and string component references. Replace the example values; for unnumbered input, replace "C0" with a supplied full component ID.
Do not include comments, ellipses, trailing commas, Markdown fences or explanations.
Check that every opening brace and bracket has the matching closing delimiter.
<GROUPED_COMPONENTS>
{{"module_name_1": {{"path": "path/to/module", "components": ["C0"]}}}}
</GROUPED_COMPONENTS>
""".strip()

CLUSTER_MODULE_PROMPT = """
Here is the module tree of a repository:

<MODULE_TREE>
{module_tree}
</MODULE_TREE>

Here is the list of potential core components of the module {module_name}. All supplied components require an explicit sub-module assignment, even if they are less central:
<POTENTIAL_CORE_COMPONENTS>
{potential_core_components}
</POTENTIAL_CORE_COMPONENTS>

Please group the components into groups such that each group is a set of components that are closely related to each other and together they form a smaller module. Assign EVERY supplied component to a sub-module. Never omit supplied IDs; less central components still need an explicit assignment.

Files marked `(artifact: <class>)` are build, CI, container, packaging, manifest, configuration, schema or script files. Their components describe how the system is built, packaged, shipped, configured and tested. They ARE essential: group them into a dedicated build/deployment/configuration module, or attach them to the module they configure. Never drop them.

Entries may have the form `C0 = <file_path>::<name>`. When these short references are supplied, return ONLY the left-hand references (e.g. "C0", "C1") in components, never the full paths or names. References are local to this request; use only those in POTENTIAL_CORE_COMPONENTS, not IDs from MODULE_TREE. Assign each supplied reference exactly once; do not use ranges, combine references into one string, or select only representative components. If entries have no short references, return their full `<file_path>::<name>` IDs exactly as supplied. Before returning, verify that the assigned references or IDs cover the entire supplied list.

Reason internally, then return ONLY the following envelope containing valid compact JSON.
Use string paths (file or directory) and string component references. Replace the example values; for unnumbered input, replace "C0" with a supplied full component ID.
Do not include comments, ellipses, trailing commas, Markdown fences or explanations.
Check that every opening brace and bracket has the matching closing delimiter.
<GROUPED_COMPONENTS>
{{"module_name_1": {{"path": "path/to/module", "components": ["C0"]}}}}
</GROUPED_COMPONENTS>
""".strip()

SUPER_GROUP_PROMPT = """
Here is the flat list of top-level modules of a repository. Each module has a path and its core components:

<MODULES>
{formatted_modules}
</MODULES>

The module list is flat: conceptually related modules sit next to unrelated ones. Please group these modules into higher-level architectural subsystems so the repository reads as an architecture rather than a directory listing.

Guidelines:
- Each subsystem is a set of modules that together fulfil one architectural responsibility (e.g. a processing pipeline, a user-facing interface layer, a shared platform/foundation).
- Name each subsystem by its architectural role, not by a directory name.
- There must be significantly fewer subsystems than modules; do not simply mirror the module list back.
- Subsystems should contain multiple modules. A single-module subsystem is just a rename — keep a module alone ONLY when it truly shares no architectural responsibility with any other module.
- Every module must be assigned to exactly one subsystem.
- Return the module names EXACTLY as given — do NOT rename or shorten them.

Firstly reason about the modules' responsibilities and relationships and identify the architectural layers they form, then return a grouping that MATCHES your reasoning in the following format:
<GROUPED_MODULES>
{{
    "subsystem_name_1": {{
        "modules": [
            <module_name_1>,
            <module_name_2>,
            ...
        ]
    }},
    "subsystem_name_2": {{
        "modules": [
            <module_name_3>,
            ...
        ]
    }},
    ...
}}
</GROUPED_MODULES>
""".strip()

FILTER_FOLDERS_PROMPT = """
Here is the list of relative paths of files, folders in 2-depth of project {project_name}:
```
{files}
```

In order to analyze the core functionality of the project, we need to analyze the files, folders representing the core functionality of the project.

Please shortlist the files, folders representing the core functionality and ignore the files, folders that are not essential to the core functionality of the project (e.g. test files, documentation files, etc.) from the list above.

Reasoning at first, then return the list of relative paths in JSON format.
"""

logger = logging.getLogger(__name__)

# codex rejects any turn whose total input exceeds 1,048,576 characters
# (input_too_large, code -32602 — server-side, not configurable).  Cap the
# user prompt below that, leaving headroom for the system prompt, tool
# schemas and protocol overhead.
MAX_USER_PROMPT_CHARS = 900_000

# Dependencies are filled up to the user-prompt limit: every direct dependency
# the evidence validator requires is either inlined here or inevitably read
# later (which re-sends the whole prompt once per extra turn), so leaving
# budget unused only bought extra turns. Reserve room for the prompt skeleton,
# the deferred-ID list and SOURCE_ALREADY_PROVIDED note.
DEPENDENCY_NOTE_RESERVE_CHARS = 20_000

# Small dependency files win the remaining budget first: maximising the number
# of fully-evidenced files minimises the forced read-back turns. Purely a
# selection order for the dependency budget, never the module's own files
# (those stay in their assigned order and are never dropped).

# Marker appended by the artifact analyzer when a file head was cut short.
ARTIFACT_TRUNCATION_HINT = "# [codewiki: truncated"

# Ownership and evidence discipline shared by the init author prompt (the
# business note in format_user_prompt) and the incremental-update patch prompt
# (format_update_system_prompt). One source of truth: the update agent must
# enforce the same rules the init build was measured against, not a second,
# drifting copy. Keep the wording stable — the low duplicate-ownership rate
# depends on it.
OWNERSHIP_AND_EVIDENCE = (
    "Do not invent missing behavior. "
    "Primary documentation ownership does not restrict reading shared source or "
    "explaining its role in other topics. Link to the primary explanation instead "
    "of duplicating every field."
)

BUSINESS_DOCUMENTATION_RULES = (
    "Document business knowledge, not a source-directory catalog. Explain source-backed "
    "business rules, entry points, complete workflows, state changes, validation, exceptional "
    "branches and data conversions where present. Split complex "
    "topics only along meaningful responsibilities; preserve all assigned analysis scope. "
    + OWNERSHIP_AND_EVIDENCE
    + " Static dependencies are clues, not a complete graph: investigate injection, dynamic calls "
    "and producer/consumer relationships when the source suggests them. Read unresolved or "
    "truncated implementations with read_code_components or repository view before completion. "
    "Do not claim unread implementations or directory supplement pages constitute completion."
)

MODULE_TREE_TRIMMED_NOTE = (
    "NOTE: per-module component listings were omitted because the full module "
    "tree exceeds the model input limit. Module names and hierarchy are "
    "complete; read the referenced modules' documentation files or use your "
    "code-reading tools when you need component-level detail."
)

CODE_TRUNCATED_NOTE = (
    "\n... [file contents truncated to fit the model input limit — use your "
    "file-reading tools to read the full files]"
)

# Added when every implementation this page needs is already inlined in full.
# Each extra turn re-sends the whole module source, so reading what is already
# present is the single largest avoidable cost of a generation run. Splitting a
# large module stays explicitly available: suppressing the read loop must not
# suppress the split that gives a large module its own sub-pages. The splitting
# clause is only included when the delegation tool is actually registered —
# naming a tool the agent cannot call just burns a request.
DIRECT_WRITE_NOTE = (
    "\n\n<SOURCE_COMPLETE>\n"
    "The assigned files and every inlined dependency listed under SOURCE_ALREADY_PROVIDED "
    "are present IN FULL. Avoid retrieving those same files again. "
    "If behaviour you must describe depends on an implementation that is NOT in this prompt, "
    "read it with read_code_components before writing; never infer internals from a "
    "signature alone.\n"
    "If this scope fits one coherent page, go straight to str_replace_editor create and write "
    "it completely, covering entry points, the full flow, state changes, validation, and "
    "error branches.\n"
    "{splitting}"
    "</SOURCE_COMPLETE>"
)

DIRECT_WRITE_SPLIT_NOTE = (
    "Only if the scope is genuinely too large or too varied for a single page, split it "
    "instead of writing an index: use generate_sub_module_documentation to create the "
    "sub-pages, assign every component to one of them, and give this page the overview that "
    "ties them together. Splitting on a real boundary is welcome; splitting to avoid writing "
    "is not.\n"
)

# How pages link to each other, appended to the user prompt (after the module
# tree) by layout. Kept out of USER_PROMPT so the MCP prompt server, which
# formats USER_PROMPT directly, keeps working.
FLAT_LINKS_NOTE = (
    "* NOTE: Know that all documentation files are saved in the same folder not structured "
    "as module tree. e.g. [alt text]([ref_module_name].md)"
)

HIERARCHICAL_LINKS_NOTE = (
    "* NOTE: Documentation files are organized in folders mirroring the module tree; each "
    "module above is listed with its page path (relative to the docs working directory). "
    "Your page is `{doc_file}`{children_hint}. Link to other pages with paths relative to "
    "your page, e.g. from `a/b.md` to `c.md` write [alt text](../c.md) and to `a/b/d.md` "
    "write [alt text](b/d.md)."
)

# Appended to the user prompt (after USER_PROMPT) when the dependency graph
# contains artifact nodes. Kept out of USER_PROMPT itself so callers that
# format the template directly (MCP prompt server) keep working.
ARTIFACT_USAGE_NOTE = (
    "* NOTE: when this module's behaviour depends on how the system is built, "
    "configured, packaged, deployed or tested, read the relevant artifact file "
    'with `str_replace_editor` (`command="view"`, `working_dir="repo"`, path as '
    "listed above) and cite the file path in the documentation."
)

# Appended to the user prompt when the module contains Dart sources. Kept
# out of USER_PROMPT for the same reason as ARTIFACT_USAGE_NOTE.
DART_FLUTTER_NOTE = (
    "* NOTE (Dart/Flutter): when these components are Flutter code, also cover, "
    "where they apply: (1) the widget tree — which widgets each widget builds "
    "and where it is used, and Stateless vs Stateful/Consumer widgets; (2) state "
    "management — where state lives (State classes, Riverpod providers and "
    "notifiers, Bloc/Cubit, ChangeNotifier), which providers each widget "
    "watches or reads, and how updates flow to the UI; (3) navigation — routes, "
    "paths and screens (GoRouter, Navigator pushes) and how parameters are "
    "passed; (4) lifecycle and async behaviour (initState/dispose, Futures, "
    "Streams). Leave out any of these that do not apply."
)

REPO_OVERVIEW_ARTIFACT_ADDENDUM = """
The repository also contains the following build, CI, container, packaging, manifest and configuration artifacts:
{artifact_index}

Include a short section on how it is built and run that summarises how the project is built, tested, packaged and deployed, and links to the module documentation that covers these artifacts (for example a `Build, Deployment and Configuration` module) instead of repeating its content.
""".strip()

EXTENSION_TO_LANGUAGE = {
    ".py": "python",
    ".md": "markdown",
    ".sh": "bash",
    ".bash": "bash",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "text",
    ".mk": "makefile",
    ".gradle": "groovy",
    ".proto": "protobuf",
    ".gn": "text",
    ".gni": "text",
    ".rake": "ruby",
    ".ps1": "powershell",
    ".xml": "xml",
    ".html": "html",
    ".css": "css",
    # extension-less artifact files are looked up by basename
    "Dockerfile": "dockerfile",
    "Containerfile": "dockerfile",
    "Makefile": "makefile",
    "GNUmakefile": "makefile",
    "Jenkinsfile": "groovy",
    "Rakefile": "ruby",
    "Gemfile": "ruby",
    ".java": "java",
    ".js": "javascript",
    ".ts": "typescript",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".tsx": "typescript",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sc": "scala",
    ".rs": "rust",
    ".dart": "dart",
    ".php": "php",
    ".phtml": "php",
    ".inc": "php",
    ".rb": "ruby",
}


def is_flat_layout(layout: str | None) -> bool:
    return layout == LAYOUT_FLAT


def _layout_fields(module_name: str, module_path: list[str] | None, layout: str | None) -> dict:
    doc_file = module_doc_file(module_name, module_path, layout)
    if is_flat_layout(layout):
        return {
            "doc_file": doc_file,
            "sub_module_location": "in the working directory under the name of `sub-module_name.md`",
            "uniqueness_reason": "all docs share one flat directory",
        }
    folder = doc_file[: -len(".md")] + "/" if module_path else ""
    location = (
        f"by `generate_sub_module_documentation` under `{folder}` as `sub-module_name.md`"
        if folder
        else "by `generate_sub_module_documentation` as `sub-module_name.md`"
    )
    return {
        "doc_file": doc_file,
        "sub_module_location": location,
        "uniqueness_reason": "the module name is the page's filename",
    }


def format_links_note(module_name: str, module_path: list[str] | None, layout: str | None) -> str:
    """How the agent documenting ``module_name`` names and links pages."""
    if is_flat_layout(layout):
        return FLAT_LINKS_NOTE
    doc_file = module_doc_file(module_name, module_path, layout)
    children_hint = (
        f" and its sub-module pages go in `{doc_file[: -len('.md')]}/`" if module_path else ""
    )
    return HIERARCHICAL_LINKS_NOTE.format(doc_file=doc_file, children_hint=children_hint)


def _format_module_tree_str(
    module_tree: dict[str, Any],
    current_module_name: str | None = None,
    include_components: bool = True,
    doc_paths: dict[str, str] | None = None,
) -> str:
    """
    Render a module tree as an indented text outline.

    With include_components=False only module names and hierarchy are
    emitted, which keeps the outline small enough for huge trees that would
    otherwise blow past MAX_USER_PROMPT_CHARS. ``doc_paths`` (module name ->
    page path) adds each module's page path, for the hierarchical layout.
    """
    lines: list[str] = []

    def _walk(tree: dict[str, Any], indent: int = 0) -> None:
        for key, value in tree.items():
            label = key
            if doc_paths and key in doc_paths:
                label += f" [page: {doc_paths[key]}]"
            if key == current_module_name:
                lines.append(f"{'  ' * indent}{label} (current module)")
            else:
                lines.append(f"{'  ' * indent}{label}")

            if include_components:
                # Group components by file
                by_file = defaultdict(list)
                for c in value["components"]:
                    if "::" in c:
                        fpath, name = c.split("::", 1)
                        by_file[fpath].append(name)
                    else:
                        by_file[""].append(c)
                for fpath, names in by_file.items():
                    if fpath:
                        lines.append(f"{'  ' * (indent + 1)} {fpath}: {', '.join(names)}")
                    else:
                        lines.append(f"{'  ' * (indent + 1)} {', '.join(names)}")

            if (
                ("children" in value)
                and isinstance(value["children"], dict)
                and len(value["children"]) > 0
            ):
                lines.append(f"{'  ' * (indent + 1)} Children:")
                _walk(value["children"], indent + 2)

    _walk(module_tree, 0)
    return "\n".join(lines)


def _dependency_ownership_index(
    module_tree: dict[str, Any],
    dependency_ids: set[str] | list[str],
    current_path: list[str],
) -> str:
    """Map only THIS page's dependencies to the page(s) that own them.

    The slim module tree no longer carries every page's component list, so the
    agent gets the ownership facts it actually needs: for each dependency
    referenced by the page's source, which documentation page explains it.
    Components shared by several pages list every owner; the current page is
    skipped. Empty output means no cross-page evidence is needed.
    """
    wanted = set(dependency_ids)
    if not wanted:
        return ""
    owners: dict[str, list[str]] = {}

    def _walk(tree: dict[str, Any], path: list[str]) -> None:
        for name, value in tree.items():
            here = path + [name]
            label = "/".join(here)
            for cid in value.get("components") or []:
                if cid in wanted:
                    pages = owners.setdefault(cid, [])
                    if label not in pages:
                        pages.append(label)
            children = value.get("children")
            if isinstance(children, dict):
                _walk(children, here)

    _walk(module_tree, [])
    current_label = "/".join(current_path or [])
    lines = [
        "<DEPENDENCY_OWNERSHIP>",
        "Dependencies below are documented on OTHER pages. When behaviour from one matters "
        "here, link to that page instead of restating its implementation:",
    ]
    found = False
    for cid in sorted(owners):
        pages = [page for page in owners[cid] if page != current_label]
        if pages:
            lines.append(f"- {cid} -> {', '.join(pages)}")
            found = True
    if not found:
        return ""
    lines.append("</DEPENDENCY_OWNERSHIP>")
    return "\n".join(lines)


def _fence_language(path: str) -> str:
    """Markdown fence language for ``path`` (falls back to ``text``)."""
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    if base in EXTENSION_TO_LANGUAGE:
        return EXTENSION_TO_LANGUAGE[base]
    if "." in base:
        ext = "." + base.rsplit(".", 1)[-1].lower()
        if ext in EXTENSION_TO_LANGUAGE:
            return EXTENSION_TO_LANGUAGE[ext]
        stem = base.split(".", 1)[0]  # Dockerfile.dev -> Dockerfile
        if stem in EXTENSION_TO_LANGUAGE:
            return EXTENSION_TO_LANGUAGE[stem]
    return "text"


def _artifact_group_source(component_ids: list[str], components: dict[str, Any]) -> str | None:
    """Return the capped artifact text for a file group made only of artifact
    nodes, or ``None`` when the group contains code components."""
    nodes = [components[c] for c in component_ids if c in components]
    if not nodes or any(getattr(n, "component_type", None) != "artifact" for n in nodes):
        return None
    file_nodes = [n for n in nodes if getattr(n, "node_type", None) == "artifact_file"]
    if file_nodes:
        return file_nodes[0].source_code or ""
    # Only unit nodes were selected: the file node carries the full head, use
    # it when present in the graph, otherwise join the unit slices.
    rel = nodes[0].relative_path
    file_id = f"{rel}::{rel.rsplit('/', 1)[-1]}"
    file_node = components.get(file_id)
    if file_node is not None and file_node.source_code:
        return file_node.source_code
    return "\n\n".join((n.source_code or "") for n in nodes)


def format_user_prompt(
    module_name: str,
    core_component_ids: list[str],
    components: dict[str, Any],
    module_tree: dict[str, Any],
    module_path: list[str] | None = None,
    layout: str | None = LAYOUT_FLAT,
    *,
    business_analysis: bool = False,
    required_source_ids: set[str] | None = None,
    prompt_read_ids: set[str] | None = None,
    deferred_source_ids: set[str] | None = None,
    can_delegate: bool = False,
) -> str:
    """
    Format the user prompt with module name and organized core component codes.

    Args:
        module_name: Name of the module to document
        core_component_ids: List of component IDs to include
        components: Dictionary mapping component IDs to CodeComponent objects
        module_tree: Current module tree
        module_path: Path of the module in the tree (``[]`` for the whole repo)
        layout: Docs layout; the hierarchical layout lists each module's page path
        deferred_source_ids: Out-param populated with the dependency component
            IDs that did NOT fit the inline budget and must still be read.
            Direct-write is only valid when this set stays empty.

    Returns:
        Formatted user prompt string
    """
    from codewiki.src.be.dependency_analyzer.analyzers.artifact import render_artifact_index

    doc_paths = None if is_flat_layout(layout) else doc_relpaths(module_tree, layout)
    links_note = format_links_note(module_name, module_path, layout)

    # Group core component IDs by their file path
    grouped_components: dict[str, list[str]] = {}
    for component_id in core_component_ids:
        if component_id not in components:
            continue
        component = components[component_id]
        path = component.relative_path
        if path not in grouped_components:
            grouped_components[path] = []
        grouped_components[path].append(component_id)

    dependency_owners = core_component_ids
    if business_analysis:
        files = {components[cid].relative_path for cid in core_component_ids if cid in components}
        dependency_owners = [cid for cid, node in components.items() if node.relative_path in files]
    dependencies = sorted(
        {
            dep
            for cid in dependency_owners
            if cid in components
            for dep in components[cid].depends_on
            if dep in components and dep not in core_component_ids
        }
    )

    # The page author needs page names/hierarchy and, for cross-page links,
    # where its own dependencies live — not every other page's component list
    # (measured ~40k tokens/request of pure re-sent ownership data on a 75-page
    # tree). Component-level ownership for THIS page's references follows below.
    formatted_module_tree = _format_module_tree_str(
        module_tree,
        module_name,
        include_components=False,
        doc_paths=doc_paths,
    )
    ownership_index = _dependency_ownership_index(module_tree, dependencies, module_path or [])
    if ownership_index:
        formatted_module_tree += "\n\n" + ownership_index

    source_cache: dict[str, str | None] = {}

    def file_source(path, ids):
        if path not in source_cache:
            source = _artifact_group_source(ids, components)
            if source is None:
                try:
                    source = file_manager.load_text(components[ids[0]].file_path)
                except OSError:
                    source = None
            source_cache[path] = source
        return source_cache[path]

    business_note = ""
    if business_analysis:
        topic = module_tree
        for index, part in enumerate(module_path or []):
            topic = topic.get(part, {})
            if index < len(module_path or []) - 1:
                topic = topic.get("children", {})
        business_note = "\nBusiness topic purpose: " + str(topic.get("purpose", module_name))
        shared = topic.get("shared_components") or []
        shared_with = topic.get("components_shared_with") or []
        if shared or shared_with:
            lines = []
            if shared:
                lines.append(
                    "The components below are ALSO claimed by another topic and are NOT yours to "
                    "explain in full here. Explain only how this topic uses them, and state where the "
                    "full explanation lives (see SHARED_COMPONENTS):"
                )
                lines.extend(f"- {cid}" for cid in shared)
            if shared_with:
                lines.append(
                    "You own the primary explanation of these components. Another topic also uses "
                    "them; note that fact once so the reader can follow the cross-topic link:"
                )
                for cid, other in shared_with:
                    lines.append(f"- {cid} (also used by {other})")
            business_note += (
                "\n<SHARED_COMPONENTS>\n"
                + "\n".join(lines)
                + "\n</SHARED_COMPONENTS>\nDo not restate a component's full behaviour in the place "
                "that refers to it."
            )
        business_note += (
            "\n\n" + BUSINESS_DOCUMENTATION_RULES + "\n"
            "Assigned repository IDs (retain the entire scope even if source text is trimmed):\n"
            + "\n".join(core_component_ids)
        )

    artifact_index = render_artifact_index(components)
    artifact_section = f"\n\n{artifact_index}\n{ARTIFACT_USAGE_NOTE}" if artifact_index else ""

    core_component_codes = ""
    file_evidence_ends: dict[str, int] = {}

    def _append_file_block(path: str, component_ids_in_file: list[str]) -> None:
        nonlocal core_component_codes
        complete_file = False
        core_component_codes += f"# File: {path}\n\n"
        core_component_codes += "## Core Components in this file:\n"

        for component_id in component_ids_in_file:
            core_component_codes += f"- {component_id}\n"

        core_component_codes += f"\n## File Content:\n```{_fence_language(path)}\n"

        artifact_source = _artifact_group_source(component_ids_in_file, components)
        if artifact_source is not None:
            # Artifact files are inlined from their capped head, never re-read
            # in full (a 150 KB YAML must not blow up the prompt).
            core_component_codes += artifact_source
        else:
            # Read content of the file using the first component's file path
            try:
                source = file_source(path, component_ids_in_file)
                if source is None:
                    raise OSError(f"Source unavailable: {path}")
                core_component_codes += source
                complete_file = True
            except (OSError, FileNotFoundError) as e:
                core_component_codes += f"# Error reading file: {e}\n"

        core_component_codes += "\n```\n\n"
        if complete_file:
            file_evidence_ends[path] = len(core_component_codes)
        elif artifact_source is not None and ARTIFACT_TRUNCATION_HINT not in artifact_source:
            # Only an artifact whose capped head was NOT cut short counts as a
            # fully provided file; a truncated head is partial evidence.
            file_evidence_ends[path] = len(core_component_codes)

    # The module's own files come first in assigned order: they are the
    # evidence baseline (never dropped, never reordered), and truncation cuts
    # from the end, so emergency trimming removes dependency files first.
    for own_path, own_ids in grouped_components.items():
        _append_file_block(own_path, own_ids)

    has_dart = any(
        components[cid].relative_path.endswith(".dart")
        for cid in core_component_ids
        if cid in components
    )
    dart_section = f"\n\n{DART_FLUTTER_NOTE}" if has_dart else ""

    # Group dependencies per file (whole-file evidence is what the read
    # validator accepts), then fill the REMAINING prompt budget. Every direct
    # dependency left out here is forced back later as an extra turn that
    # re-sends the whole prompt — so unused headroom was pure waste, and small
    # files go first to maximise the number of files fully evidenced.
    dependency_files: dict[str, list[str]] = {}
    for component_id in dependencies:
        component = components.get(component_id)
        if component is None or component.relative_path in grouped_components:
            continue
        ids = dependency_files.setdefault(component.relative_path, [])
        if component_id not in ids:
            ids.append(component_id)

    sized_dependencies = sorted(
        ((len(file_source(path, ids) or ""), path, ids) for path, ids in dependency_files.items()),
        key=lambda item: item[0],
    )
    skeleton_chars = (
        len(core_component_codes)
        + len(formatted_module_tree)
        + len(links_note)
        + len(artifact_section)
        + len(business_note)
        + DEPENDENCY_NOTE_RESERVE_CHARS
        + 4_000
    )
    dependency_budget = max(0, MAX_USER_PROMPT_CHARS - skeleton_chars)
    dependency_chars = 0
    deferred_dependencies: list[str] = []
    inlined_dependency_files: set[str] = set()
    for size, path, ids in sized_dependencies:
        if size and dependency_chars + size <= dependency_budget:
            _append_file_block(path, ids)
            dependency_chars += size
            inlined_dependency_files.add(path)
        else:
            deferred_dependencies.extend(ids)
    if deferred_dependencies:
        logger.debug(
            "Module %s: prefetched %d/%d dependency files (%d chars), deferred %d of %d "
            "dependency components to on-demand reads (remaining budget %d chars)",
            module_name,
            len(inlined_dependency_files),
            len(dependency_files),
            dependency_chars,
            len(deferred_dependencies),
            len(dependencies),
            dependency_budget,
        )

    # dependency_note is built AFTER the emergency truncation below: a file
    # prefetched into the prompt but cut back out must be listed as deferred,
    # otherwise the prompt would both deny evidence and forbid the read.

    def _assemble(
        codes: str,
        tree: str,
        provided: tuple[str, ...] = (),
        direct_write: bool = False,
        split: bool = False,
    ) -> str:
        provided_note = ""
        if provided:
            provided_note = (
                "\n\n<SOURCE_ALREADY_PROVIDED>\n"
                "The files below are provided IN FULL above. Their content is already in this "
                "prompt, so do NOT read them again with read_code_components or "
                "str_replace_editor view:\n"
                + "\n".join(provided)
                + "\nRead only implementations that are missing, truncated, or referenced but "
                "not listed here.\n</SOURCE_ALREADY_PROVIDED>"
            )
        write_note = (
            DIRECT_WRITE_NOTE.format(splitting=DIRECT_WRITE_SPLIT_NOTE if split else "")
            if (direct_write and provided)
            else ""
        )
        return (
            USER_PROMPT.format(
                module_name=module_name,
                formatted_core_component_codes=codes,
                module_tree=tree,
            )
            + f"\n\n{links_note}"
            + artifact_section
            + dependency_note
            + provided_note
            + write_note
            + business_note
            + dart_section
        )

    def _provided_paths() -> tuple[str, ...]:
        # Only files whose full text survived truncation count as provided.
        return tuple(
            path for path, _end in sorted(file_evidence_ends.items(), key=lambda kv: kv[1])
        )

    def _build_dependency_note() -> str:
        if not dependencies:
            return ""
        deferred_ids = set(deferred_dependencies)
        lines = ["\n\nRepository dependencies (exact IDs):"]
        inlined_ids = [cid for cid in dependencies if cid not in deferred_ids]
        if inlined_ids:
            lines.append("Source inlined above under `# File:`; do not read these again:")
            lines.extend(f"- {cid}" for cid in inlined_ids)
        if deferred_dependencies:
            lines.append(
                "The source of the following is NOT in this prompt: it was left out to keep the "
                "prompt within limits. If this module's behaviour depends on one, read it with "
                "read_code_components before writing; otherwise ignore it."
            )
            lines.extend(f"- {cid}" for cid in deferred_dependencies)
        return "\n".join(lines)

    dependency_note = _build_dependency_note()
    prompt = _assemble(core_component_codes, formatted_module_tree, _provided_paths())

    if len(prompt) > MAX_USER_PROMPT_CHARS:
        full_len = len(prompt)
        formatted_module_tree = (
            MODULE_TREE_TRIMMED_NOTE
            + "\n\n"
            + _format_module_tree_str(
                module_tree, module_name, include_components=False, doc_paths=doc_paths
            )
        )
        prompt = _assemble(core_component_codes, formatted_module_tree, _provided_paths())
        logger.warning(
            "Module %s: user prompt (%d chars) exceeds %d; "
            "module tree trimmed to names only (%d chars)",
            module_name,
            full_len,
            MAX_USER_PROMPT_CHARS,
            len(prompt),
        )

    if len(prompt) > MAX_USER_PROMPT_CHARS:
        # Even the slim tree was not enough — the inlined file contents
        # dominate.  Truncation is recoverable: the agent has file-reading
        # tools (read_code_components / str_replace_editor).
        excess = len(prompt) - MAX_USER_PROMPT_CHARS + len(CODE_TRUNCATED_NOTE)
        cutoff = max(0, len(core_component_codes) - excess)
        file_evidence_ends = {
            path: end for path, end in file_evidence_ends.items() if end <= cutoff
        }
        core_component_codes = core_component_codes[:cutoff] + CODE_TRUNCATED_NOTE
        # A prefetched dependency file lost to truncation is de facto deferred:
        # keep the dependency list truthful, or the "do not read again" note
        # would hide code the validator still requires evidence for.
        surviving_paths = set(file_evidence_ends)
        reverted = False
        for _size, dep_path, dep_ids in sized_dependencies:
            if dep_path not in surviving_paths and dep_path not in grouped_components:
                for cid in dep_ids:
                    if cid not in deferred_dependencies:
                        deferred_dependencies.append(cid)
                        reverted = True
        if reverted:
            dependency_note = _build_dependency_note()
        prompt = _assemble(core_component_codes, formatted_module_tree, _provided_paths())
        logger.warning(
            "Module %s: user prompt still over %d chars after tree trim; "
            "truncated inlined file contents (now %d chars)",
            module_name,
            MAX_USER_PROMPT_CHARS,
            len(prompt),
        )

    if business_analysis:
        provided = {
            cid for cid, node in components.items() if node.relative_path in file_evidence_ends
        }
        if prompt_read_ids is not None:
            prompt_read_ids.update(provided)
        assigned_paths = {
            components[cid].relative_path for cid in core_component_ids if cid in components
        }
        # Compare paths with paths: a file that survived truncation in full needs
        # no read, and only the files actually assigned to this module count.
        # Mixing component ids and paths here would report every inlined file as
        # still missing and send the agent back to read what it already has.
        provided_paths = {
            node.relative_path
            for node in components.values()
            if node.relative_path in file_evidence_ends
        }
        still_needed = {
            cid
            for cid, node in components.items()
            if node.relative_path in assigned_paths and node.relative_path not in provided_paths
        }
        if required_source_ids is not None:
            required_source_ids.update(still_needed)
        # Direct write requires BOTH the assigned files complete AND every
        # direct dependency present: a deferred dependency forces the
        # read-before-write workflow, otherwise the page is written blind and
        # every missing ID costs a full-context repair turn.
        if assigned_paths and not still_needed and not deferred_dependencies:
            complete_prompt = _assemble(
                core_component_codes,
                formatted_module_tree,
                _provided_paths(),
                direct_write=True,
                split=can_delegate,
            )
            if len(complete_prompt) <= MAX_USER_PROMPT_CHARS:
                prompt = complete_prompt
    if deferred_source_ids is not None:
        deferred_source_ids.update(deferred_dependencies)
    return prompt


def format_cluster_prompt(
    potential_core_components: str,
    module_tree: dict[str, Any] | None = None,
    module_name: str | None = None,
    language: str | None = None,
) -> str:
    """
    Format the cluster prompt with potential core components and module tree.
    """
    naming = format_module_naming_directive(language)
    prefix = f"{naming}\n\n" if naming else ""
    if module_tree is None:
        module_tree = {}

    if module_tree == {}:
        return prefix + CLUSTER_REPO_PROMPT.format(
            potential_core_components=potential_core_components
        )

    formatted_module_tree = _format_module_tree_str(module_tree, module_name)
    prompt = CLUSTER_MODULE_PROMPT.format(
        potential_core_components=potential_core_components,
        module_tree=formatted_module_tree,
        module_name=module_name,
    )

    if len(prompt) > MAX_USER_PROMPT_CHARS:
        full_len = len(prompt)
        formatted_module_tree = (
            MODULE_TREE_TRIMMED_NOTE
            + "\n\n"
            + _format_module_tree_str(module_tree, module_name, include_components=False)
        )
        prompt = CLUSTER_MODULE_PROMPT.format(
            potential_core_components=potential_core_components,
            module_tree=formatted_module_tree,
            module_name=module_name,
        )
        logger.warning(
            "Module %s: cluster prompt (%d chars) exceeds %d; "
            "module tree trimmed to names only (%d chars)",
            module_name,
            full_len,
            MAX_USER_PROMPT_CHARS,
            len(prompt),
        )

    return prefix + prompt


def format_super_group_prompt(module_tree: dict[str, Any], language: str | None = None) -> str:
    """
    Format the super-grouping prompt with the flat top-level modules of a tree.
    """
    lines = []
    for name, info in module_tree.items():
        path = info.get("path", "")
        lines.append(f"{name} (path: {path})" if path else name)
        for component in info.get("components", []):
            lines.append(f"\t{component}")
    prompt = SUPER_GROUP_PROMPT.format(formatted_modules="\n".join(lines))
    naming = format_module_naming_directive(language)
    return f"{naming}\n\n{prompt}" if naming else prompt


def _delegation_fields(can_delegate: bool, layout_fields: dict, module_name: str) -> dict:
    """Workflow steps and tool list entries for sub-module delegation.

    When the module cannot be decomposed any further (``max_depth`` reached),
    the delegation step and tool are left out entirely: telling a model to call
    a tool that will refuse only burns a request.
    """
    if not can_delegate:
        return {
            "delegation_step": "",
            "finalize_step": NO_DELEGATION_FINALIZE_STEP,
            "delegation_tool": "",
        }
    # These fragments carry their own placeholders (doc_file, uniqueness_reason),
    # so they are substituted here — str.format does not recurse into values.
    return {
        "delegation_step": DELEGATION_WORKFLOW_STEP.format(
            module_name=module_name, **layout_fields
        ),
        "finalize_step": DELEGATION_FINALIZE_STEP.format(**layout_fields),
        "delegation_tool": DELEGATION_TOOL_LINE,
    }


def _source_complete_fields(direct_write: bool) -> dict[str, str]:
    """Workflow step and tool line for a page whose source is already inlined.

    The default workflow tells the agent to analyse the source and explore
    dependencies before writing. When every required file is already in the
    prompt, that step is not just redundant — it is what the model actually
    follows, so a note in the user prompt loses to it and the agent burns
    requests re-reading source it already has. Measured: ten requests and 30k
    tokens of history for one module that needed a single write.
    """
    if not direct_write:
        return {
            "analyse_step": EXPLORE_SOURCE_WORKFLOW_STEP,
            "read_tool_line": READ_TOOL_LINE,
        }
    return {
        "analyse_step": DIRECT_WRITE_WORKFLOW_STEP,
        "read_tool_line": "",
    }


def format_system_prompt(
    module_name: str,
    custom_instructions: str | None = None,
    module_path: list[str] | None = None,
    layout: str | None = LAYOUT_FLAT,
    can_delegate: bool = True,
    direct_write: bool = False,
) -> str:
    """
    Format the system prompt with module name and optional custom instructions.

    Args:
        module_name: Name of the module to document
        custom_instructions: Optional custom instructions to append
        module_path: Path of the module in the tree (``[]`` for the whole repo)
        layout: Docs layout, which decides the page path the agent writes
        can_delegate: Whether sub-module delegation is still possible; when
            ``False`` the delegation workflow step and tool are omitted
        direct_write: Whether every required source file is already inlined in
            the user prompt; when ``True`` the workflow skips the source
            exploration step that otherwise sends the agent back to re-read it

    Returns:
        Formatted system prompt string
    """
    custom_section = ""
    if custom_instructions:
        custom_section = f"\n\n<CUSTOM_INSTRUCTIONS>\n{custom_instructions}\n</CUSTOM_INSTRUCTIONS>"

    layout_fields = _layout_fields(module_name, module_path, layout)
    return SYSTEM_PROMPT.format(
        module_name=module_name,
        custom_instructions=custom_section,
        **layout_fields,
        **_delegation_fields(can_delegate, layout_fields, module_name),
        **_source_complete_fields(direct_write),
    ).strip()


def format_leaf_system_prompt(
    module_name: str,
    custom_instructions: str | None = None,
    module_path: list[str] | None = None,
    layout: str | None = LAYOUT_FLAT,
    direct_write: bool = False,
) -> str:
    """
    Format the leaf system prompt with module name and optional custom instructions.

    Args:
        module_name: Name of the module to document
        custom_instructions: Optional custom instructions to append
        module_path: Path of the module in the tree (``[]`` for the whole repo)
        layout: Docs layout, which decides the page path the agent writes
        direct_write: Whether every required source file is already inlined in
            the user prompt; when ``True`` the workflow skips the source
            exploration step that otherwise sends the agent back to re-read it

    Returns:
        Formatted leaf system prompt string
    """
    custom_section = ""
    if custom_instructions:
        custom_section = f"\n\n<CUSTOM_INSTRUCTIONS>\n{custom_instructions}\n</CUSTOM_INSTRUCTIONS>"

    return LEAF_SYSTEM_PROMPT.format(
        module_name=module_name,
        custom_instructions=custom_section,
        doc_file=module_doc_file(module_name, module_path, layout),
        **_source_complete_fields(direct_write),
    ).strip()


def format_overview_system_prompt(custom_instructions: str | None = None) -> str | None:
    """
    System message for the module / repository overview completions.

    MODULE_OVERVIEW_PROMPT and REPO_OVERVIEW_PROMPT have no custom-instructions
    slot, so without this the user's instructions (language, audience, style)
    never reach overview pages. Returns None when there is nothing to add.
    """
    if not custom_instructions:
        return None
    return (
        f"<CUSTOM_INSTRUCTIONS>\n{custom_instructions}\n</CUSTOM_INSTRUCTIONS>\n\n"
        "Follow these instructions when writing the overview; they take precedence over "
        "conflicting defaults in the request (language, audience, structure, style).\n"
        "Return the documentation inside exactly one <OVERVIEW>...</OVERVIEW> envelope. "
        "These tags are transport delimiters, not page prose; do not translate them. "
        "Apply the output language and page-title instructions to the Markdown INSIDE the envelope. "
        "Do not add an outer code fence or commentary outside the envelope."
    )
