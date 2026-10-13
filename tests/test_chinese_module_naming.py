# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Chinese module names follow the existing tree, layout and naming workflow."""

import json
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from codewiki.src.be import cluster_modules as clustering
from codewiki.src.be import doc_layout as L
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.module_naming import (
    dedupe_module_tree_names,
    find_missing_module_docs,
    plan_sub_module_specs,
    sanitize_module_name,
)
from codewiki.src.be.prompt_template import (
    format_cluster_prompt,
    format_super_group_prompt,
    format_system_prompt,
)
from codewiki.src.config import Config
from codewiki.src.language import format_language_directive, format_module_naming_directive

TREE = {
    "身份认证": {
        "components": ["auth.py::Auth"],
        "children": {
            "会话": {
                "components": ["session.py::Session"],
                "children": {"存储": {"components": ["store.py::Store"], "children": {}}},
            }
        },
    },
    "SDK配置": {"components": ["config.py::Config"], "children": {}},
}


def node(cid, component_type="class"):
    rel, name = cid.split("::", 1)
    return Node(
        id=cid,
        name=name,
        component_type=component_type,
        file_path=rel,
        relative_path=rel,
        source_code="code",
    )


def config(language="zh"):
    return Config.from_cli(
        repo_path=".",
        output_dir="output",
        llm_base_url="http://localhost",
        llm_api_key="test",
        main_model="m",
        cluster_model="m",
        agent_instructions={"language": language},
    )


@pytest.mark.parametrize(
    "language, name",
    [
        ("zh", "Chinese (Simplified)"),
        ("zh_CN", "Chinese (Simplified)"),
        ("zh-hant", "Chinese (Traditional)"),
        ("Chinese", "Chinese (Simplified)"),
    ],
)
def test_chinese_naming_directive(language, name):
    naming = format_module_naming_directive(language)
    assert f"in {name}" in naming
    assert "Name NEW modules" in naming
    assert "already assigned module names" in naming
    assert "component IDs" in naming
    assert naming in format_language_directive(language)
    system = format_system_prompt(
        "会话", format_language_directive(language), ["身份认证", "会话"], "hierarchical"
    )
    assert "身份认证/会话.md" in system
    assert naming in system


@pytest.mark.parametrize("language", [None, "en", "ja", "vi"])
def test_other_languages_keep_existing_naming(language):
    assert format_module_naming_directive(language) == ""
    assert format_cluster_prompt("a.py::A", language=language) == format_cluster_prompt("a.py::A")
    assert format_super_group_prompt(TREE, language=language) == format_super_group_prompt(TREE)


def test_cluster_calls_receive_language_and_preserve_ids():
    cid = "src/auth.py::Auth"
    components = {cid: node(cid)}
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return (
            f'<GROUPED_COMPONENTS>{{"身份认证": {{"components": ["{cid}"]}}}}</GROUPED_COMPONENTS>'
        )

    cfg = config()
    result = clustering._cluster_via_llm([cid], components, cfg, {}, None, "repo", complete)
    assert result["身份认证"]["components"] == [cid]
    clustering._cluster_via_llm([cid], components, cfg, TREE, "身份认证", "身份认证", complete)
    assert len(prompts) == 2
    assert all("<MODULE_NAMING_LANGUAGE>" in prompt and cid in prompt for prompt in prompts)
    assert "身份认证" in prompts[1]


def test_super_group_calls_receive_language_and_preserve_members():
    prompts = []
    cfg = config("zh-TW")
    cfg.min_modules_for_super_grouping = 1
    tree = deepcopy(TREE)
    tree.update({"日志": {"components": []}, "指标": {"components": []}})

    def complete(prompt):
        prompts.append(prompt)
        return (
            '<GROUPED_MODULES>{"應用服務": {"modules": ["身份认证", "SDK配置"]}, '
            '"可觀測性": {"modules": ["日志", "指标"]}}</GROUPED_MODULES>'
        )

    result = clustering.super_group_modules(tree, cfg, completer=complete)
    assert set(result["應用服務"]["children"]) == set(TREE)
    assert set(result["可觀測性"]["children"]) == {"日志", "指标"}
    assert "Chinese (Traditional)" in prompts[0]
    assert "Return the module names EXACTLY as given" in prompts[0]


@pytest.mark.parametrize("layout", ["hierarchical", "flat"])
def test_chinese_tree_files_lookup_and_links(tmp_path, layout):
    tree = deepcopy(TREE)
    (tmp_path / "module_tree.json").write_text(
        json.dumps(tree, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "metadata.json").write_text(
        json.dumps(
            {
                "generation_info": {
                    "language": "Chinese (Simplified)",
                    "layout": layout,
                }
            }
        ),
        encoding="utf-8",
    )
    paths = L.doc_relpaths(tree, layout)
    for stem in paths:
        (tmp_path / f"{stem}.md").write_text(
            f"# {stem}\n[配置](SDK配置.md) [存储](存储.md#接口)\n"
            "```markdown\n[示例](存储.md)\n```\n",
            encoding="utf-8",
        )
    L.organize_docs(str(tmp_path), layout, tree)
    assert L.doc_path_map(str(tmp_path), tree) == paths
    assert find_missing_module_docs(tree, str(tmp_path)) == []
    for stem, rel in paths.items():
        assert L.find_doc(str(tmp_path), stem, tree) == str(tmp_path / rel)
        assert L.target_doc_path(str(tmp_path), stem, layout, tree) == str(tmp_path / rel)
        text = (tmp_path / rel).read_text(encoding="utf-8")
        assert f"[配置]({L.relative_link(rel, paths['SDK配置'])})" in text
        assert f"[存储]({L.relative_link(rel, paths['存储'])}#接口)" in text
        assert "```markdown\n[示例](存储.md)\n```" in text
    if layout == "hierarchical":
        assert paths["存储"] == "身份认证/会话/存储.md"
    else:
        assert paths["存储"] == "存储.md"


def test_encoded_chinese_links_are_repaired():
    paths = L.doc_relpaths(TREE, "hierarchical")
    text = f"[存储]({quote('存储.md')}#接口)"
    assert L.rewrite_page_links(text, "身份认证/会话.md", paths) == "[存储](会话/存储.md#接口)"


def test_near_miss_links_redirect_to_unique_page():
    # Observed real cases: synonym, word order, missing/extra middle char,
    # delegation-prefix differences.
    paths = {
        "退费审批": "退费审批.md",
        "魔力耳朵业务服务接口": "魔力耳朵业务服务接口.md",
        "课程管理_魔力课程与固班排班": "课程管理/课程管理_魔力课程与固班排班.md",
        "阶段事件任务联动": "任务管理/阶段事件任务联动.md",
        "任务管理_完成与沟通": "任务管理/任务管理_完成与沟通.md",
    }

    def repaired(link, page="overview.md"):
        return L.rewrite_page_links(f"[x]({link})", page, paths)

    assert "(退费审批.md)" in repaired("退款审批.md")
    assert "(魔力耳朵业务服务接口.md)" in repaired("魔力耳朵业务接口.md")
    assert "(课程管理/课程管理_魔力课程与固班排班.md)" in repaired(
        "课程管理/魔力课程与固定班排班.md"
    )
    assert "(任务管理/阶段事件任务联动.md)" in repaired(
        "任务管理/任务管理_阶段事件联动.md", page="任务管理.md"
    )


def test_ambiguous_and_unrelated_links_are_left_untouched():
    # Two equally close candidates: no rewrite.
    paths = {"学员回访管理": "学员回访管理.md", "学员回访问候": "学员回访问候.md"}
    text = "[x](学员回访.md)"
    assert L.rewrite_page_links(text, "overview.md", paths) == text
    # Nothing remotely close: leave the dead target as-is for the warning list.
    paths2 = {"订单管理": "订单管理.md"}
    assert (
        L.rewrite_page_links("[x](完全无关页面.md)", "overview.md", paths2)
        == "[x](完全无关页面.md)"
    )


def test_organize_docs_repairs_real_dangling_links(tmp_path):
    tree = {
        "退费审批": {"components": []},
        "魔力耳朵业务服务接口": {"components": []},
        "课程管理": {
            "components": [],
            "children": {"课程管理_魔力课程与固班排班": {"components": []}},
        },
    }
    (tmp_path / "退费审批.md").write_text("# 退费审批", encoding="utf-8")
    (tmp_path / "魔力耳朵业务服务接口.md").write_text("# 接口", encoding="utf-8")
    folder = tmp_path / "课程管理"
    folder.mkdir()
    (folder / "课程管理_魔力课程与固班排班.md").write_text("# 固班", encoding="utf-8")
    overview = tmp_path / "overview.md"
    overview.write_text(
        "[退款](退款审批.md) [接口](魔力耳朵业务接口.md) "
        "[排班](课程管理/魔力课程与固定班排班.md) [死链](不存在的页面.md)",
        encoding="utf-8",
    )
    dangling = L.organize_docs(str(tmp_path), "hierarchical", tree)
    text = overview.read_text(encoding="utf-8")
    assert "(退费审批.md)" in text
    assert "(魔力耳朵业务服务接口.md)" in text
    assert "(课程管理/课程管理_魔力课程与固班排班.md)" in text
    assert dangling == [("overview.md", "不存在的页面.md")]


def test_subtopic_variants_recognize_prefix_styles_across_runs():
    from codewiki.src.be.module_naming import subtopic_name_variants

    children = ["魔力课程与固班排班", "课程列表与课后报告"]
    # This run the model returned the already-prefixed name; the plain child
    # created by the previous run must be the first reuse candidate.
    variants = subtopic_name_variants("课程管理_魔力课程与固班排班", "课程管理", children)
    assert variants[:2] == ["魔力课程与固班排班", "课程管理_魔力课程与固班排班"]
    assert variants[0] in children
    # Plain request keeps the same canonical order.
    assert subtopic_name_variants("固班", "课程管理", [])[0] == "固班"


def test_prefixed_repeat_request_is_skipped_when_plain_child_exists(tmp_path):
    from codewiki.src.be.module_naming import plan_sub_module_specs

    tree = {"课程管理": {"components": [], "children": {"魔力课程与固班排班": {"components": []}}}}
    plan = plan_sub_module_specs(
        {"课程管理_魔力课程与固班排班": ["x/A.java::A"]}, "课程管理", tree, str(tmp_path)
    )
    assert plan.name_map == {}
    assert "already documented" in next(iter(plan.skipped.values()))


def test_chinese_sanitizing_and_global_collisions(tmp_path):
    assert sanitize_module_name("../身份/认证:*?") == "身份_认证"
    tree = {
        "身份认证": {"children": {"登录": {"children": {}}}},
        "管理": {"children": {"登录": {"children": {}}}},
    }
    deduped = dedupe_module_tree_names(tree)
    assert "登录" in deduped["身份认证"]["children"]
    assert "管理_登录" in deduped["管理"]["children"]
    plan = plan_sub_module_specs({"登录": []}, "身份认证", deduped, str(tmp_path))
    assert plan.name_map == {"登录": "身份认证_登录"}
    (tmp_path / "身份认证_登录.md").write_text("# 登录", encoding="utf-8")
    repeat = plan_sub_module_specs({"登录": []}, "身份认证", deduped, str(tmp_path))
    assert not repeat.name_map and "登录" in repeat.skipped


@pytest.mark.parametrize(
    "language, expected",
    [
        ("zh", "构建、部署与配置"),
        ("zh-TW", "建置、部署與設定"),
        (None, clustering.ARTIFACT_MODULE_NAME),
        ("ja", clustering.ARTIFACT_MODULE_NAME),
    ],
)
def test_artifact_fallback_names_follow_chinese_language(language, expected):
    cid = "Dockerfile::artifact"
    tree = {"应用": {"components": [], "children": {}}}
    result = clustering.ensure_artifact_module(
        tree, [cid], {cid: node(cid, "artifact")}, language=language
    )
    assert result[expected]["components"] == [cid]


def test_dynamic_chinese_submodules_write_final_paths(tmp_path, monkeypatch):
    import asyncio
    from codewiki.src.be.agent_tools import generate_sub_module_documentations as mod
    from codewiki.src.be.agent_tools.deps import CodeWikiDeps

    runs = []
    cfg = config()
    cid = "auth.py::Auth"
    tree = {"身份认证": {"components": [cid], "children": {}}}
    instructions = cfg.get_prompt_addition()

    class FakeAgent:
        def __init__(self, **kwargs):
            assert format_module_naming_directive("zh") in kwargs["system_prompt"]

        async def run(self, prompt, deps, **kwargs):
            rel = L.module_doc_relpath(deps.path_to_current_module, "hierarchical")
            runs.append(rel)
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {deps.current_module_name}\n", encoding="utf-8")

    monkeypatch.setattr(mod, "Agent", FakeAgent)
    monkeypatch.setattr(mod, "create_fallback_models", lambda cfg: None)
    deps = CodeWikiDeps(
        absolute_docs_path=str(tmp_path),
        absolute_repo_path=str(tmp_path),
        registry={},
        components={cid: node(cid)},
        path_to_current_module=["身份认证"],
        current_module_name="身份认证",
        module_tree=tree,
        max_depth=2,
        current_depth=1,
        config=cfg,
        custom_instructions=instructions,
    )
    ctx = SimpleNamespace(deps=deps)
    specs = {"登录": [cid]}
    first = asyncio.run(mod.generate_sub_module_documentation(ctx, specs))
    assert "身份认证/登录.md" in first
    assert runs == ["身份认证/登录.md"]
    second = asyncio.run(mod.generate_sub_module_documentation(ctx, specs))
    assert "身份认证/身份认证_登录.md" in second
    third = asyncio.run(mod.generate_sub_module_documentation(ctx, specs))
    assert "Skipped sub-modules" in third
    assert runs == ["身份认证/登录.md", "身份认证/身份认证_登录.md"]
    assert set(tree["身份认证"]["children"]) == {"登录", "身份认证_登录"}
    assert deps.current_module_name == "身份认证"
    assert deps.path_to_current_module == ["身份认证"]


def test_mcp_cluster_prompt_receives_language():
    from codewiki.mcp.tools.prompt_server import _resolve_prompt

    prompt = _resolve_prompt("cluster", {"language": "zh", "potential_core_components": "a.py::A"})
    assert format_module_naming_directive("zh") in prompt
    assert "a.py::A" in prompt
