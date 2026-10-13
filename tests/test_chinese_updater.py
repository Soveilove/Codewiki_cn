# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Chinese page references and stable incremental updates."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from codewiki.src.be import doc_layout as L
from codewiki.src.be.backend import AgentReply, LLMBackend
from codewiki.src.be.documentation_generator import DocumentationGenerator
from codewiki.src.be.updater import pages as P
from codewiki.src.be.updater import tree as T
from codewiki.src.be.updater.graph_store import save_graph
from codewiki.src.be.updater.options import UpdateOptions
from codewiki.src.be.updater.orchestrator import IncrementalUpdater
from codewiki.src.be.updater.record import UpdateRecord
from codewiki.src.be.updater.reference_index import extract_references, inverse
from codewiki.src.be.updater.routing import RoutingAgent
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.config import Config
from codewiki.src.language import resolve_update_language


def node(cid: str) -> Node:
    rel, name = cid.split("::", 1)
    return Node(
        id=cid,
        name=name,
        component_type="class",
        file_path=rel,
        relative_path=rel,
        source_code="code",
    )


def test_chinese_reference_links_bare_names_and_encoding():
    refs = extract_references(
        f"[登录](身份认证/登录.md#接口) [配置]({quote('构建、部署与配置.md')}) "
        "另见 `会话.md` 和 `SDK配置.md`；`auth.py::Auth`。",
        {"auth.py::Auth"},
        {"Auth": {"auth.py::Auth"}},
        {"登录", "构建、部署与配置", "会话", "SDK配置"},
    )
    assert set(refs["links"]) == {"登录", "构建、部署与配置", "会话", "SDK配置"}
    assert refs["ids"] == ["auth.py::Auth"]
    assert refs["names"] == ["Auth"]
    assert inverse({"身份认证": refs})["登录"] == {"身份认证"}


@pytest.mark.parametrize("language", ["zh", "zh-TW"])
def test_routing_new_leaf_receives_language(tmp_path, language):
    cid = "new/oauth.py::OAuthClient"
    prompts = []

    class Backend(LLMBackend):
        last_usage = None

        def complete(self, prompt, **kwargs):
            prompts.append(prompt)
            return json.dumps(
                {
                    "decisions": [
                        {
                            "component_id": cid,
                            "action": "create",
                            "new_leaf": "OAuth认证",
                            "parent": "身份认证",
                        }
                    ]
                },
                ensure_ascii=False,
            )

        async def run_module_agent(
            self,
            module_name: str,
            components: dict[str, Node],
            core_component_ids: list[str],
            module_path: list[str],
            working_dir: str,
        ) -> dict[str, Any]:
            raise AssertionError("Routing must not run a documentation agent")

    tree = {"身份认证": {"children": {"会话": {"components": []}}}}
    context: dict[str, Any] = {"tree": tree, "owner": {}, "graph": {cid: node(cid)}}
    router = RoutingAgent(Backend(), str(tmp_path), UpdateRecord(), language=language)
    decisions = router([cid], context)
    assert decisions[0].leaf_path == ("身份认证", "OAuth认证")
    assert decisions[0].new_leaf
    assert "Name NEW modules" in prompts[0]
    assert ("Chinese (Traditional)" if language == "zh-TW" else "Chinese (Simplified)") in prompts[
        0
    ]
    assert cid in prompts[0] and "身份认证" in prompts[0]
    context["purpose"] = "ownership"
    assert router([cid], context)[0].leaf_path is None
    assert "<MODULE_NAMING_LANGUAGE>" not in prompts[1]


def test_chinese_pages_keep_paths_when_title_changes_and_can_be_removed(tmp_path):
    tree = {"身份认证": {"children": {"会话": {"components": []}}}}
    (tmp_path / "module_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    (tmp_path / "metadata.json").write_text(
        json.dumps(
            {
                "generation_info": {
                    "layout": "hierarchical",
                    "language": "Chinese (Simplified)",
                }
            }
        ),
        encoding="utf-8",
    )
    path = Path(P.page_path(str(tmp_path), "会话"))
    path.parent.mkdir(parents=True)
    path.write_text("# 会话管理\n旧内容", encoding="utf-8")
    before = P.page_hashes(str(tmp_path))
    path.write_text("# 会话生命周期\n新内容", encoding="utf-8")
    assert P.page_path(str(tmp_path), "会话") == str(path)
    assert P.changed_pages(before, P.page_hashes(str(tmp_path))) == {"会话"}
    assert P.page_rel(str(tmp_path), "会话") == "身份认证/会话.md"
    text = P.read_page(str(tmp_path), "会话")
    assert text is not None and "新内容" in text
    P.remove_page(str(tmp_path), "会话")
    assert not path.exists() and not path.parent.exists()
    assert (tmp_path / "module_tree.json").exists()
    assert resolve_update_language("Chinese (Simplified)", None) == "Chinese (Simplified)"
    with pytest.raises(ValueError, match="cannot switch language"):
        resolve_update_language("Chinese (Simplified)", "en")


def test_chinese_incremental_update_routes_and_writes_new_module(tmp_path):
    cid = "src/auth.py::Auth"
    orphan = "plugins/oauth.py::OAuthClient"
    old_graph = {cid: node(cid)}
    new_graph = deepcopy(old_graph)
    new_graph[cid].source_code = "changed code"
    new_graph[orphan] = node(orphan)
    tree = {"身份认证": {"components": [cid], "children": {}}}
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "module_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    (docs / "metadata.json").write_text(
        json.dumps(
            {
                "generation_info": {
                    "layout": "hierarchical",
                    "language": "Chinese (Simplified)",
                    "commit_id": "old",
                }
            }
        ),
        encoding="utf-8",
    )
    (docs / "身份认证.md").write_text("# 身份认证\n原内容\n", encoding="utf-8")
    (docs / "overview.md").write_text("# 概览\n[认证](身份认证.md)\n", encoding="utf-8")
    graph_dir = docs / "temp" / "dependency_graphs"
    graph_dir.mkdir(parents=True)
    previous = graph_dir / "previous.json"
    save_graph(old_graph, str(previous))
    prompts = []
    cfg = Config.from_cli(
        repo_path=str(tmp_path),
        output_dir=str(docs),
        llm_base_url="http://localhost",
        llm_api_key="test",
        main_model="fake",
        cluster_model="fake",
        max_depth=5,
        max_token_per_module=100000,
        agent_instructions={"language": resolve_update_language("Chinese (Simplified)", None)},
    )

    class Backend(LLMBackend):
        last_usage = None

        def complete(self, prompt, **kwargs):
            if "<ORPHANS>" in prompt:
                prompts.append(prompt)
                return json.dumps(
                    {
                        "decisions": [
                            {"component_id": orphan, "action": "create", "new_leaf": "OAuth认证"}
                        ]
                    },
                    ensure_ascii=False,
                )
            return "<OVERVIEW># 概览\n[认证](身份认证.md) [OAuth](OAuth认证.md)</OVERVIEW>"

        async def run_update_agent(self, system_prompt, user_prompt, deps):
            page = Path(P.page_path(str(docs), deps.current_module_name))
            page.write_text("# 身份认证生命周期\n更新内容\n", encoding="utf-8")
            return AgentReply(
                text=json.dumps(
                    {
                        "verdicts": {
                            f"{deps.current_module_name}.md": {
                                "verdict": "patch",
                                "reason": "changed",
                            }
                        }
                    }
                ),
                usage=None,
                seconds=0,
            )

        async def run_module_agent(
            self, module_name, components, core_component_ids, module_path, working_dir
        ):
            page = Path(working_dir) / L.module_doc_relpath(module_path, "hierarchical")
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(f"# {module_name}\n新增模块\n", encoding="utf-8")
            return L.load_module_tree(working_dir)

    backend = Backend()
    gen = object.__new__(DocumentationGenerator)
    gen.config, gen.backend, gen.commit_id = cfg, backend, "new"
    updater = IncrementalUpdater(cfg, backend, gen, UpdateOptions(tau_full=2.0, tau_tree=2.0))
    record = asyncio.run(
        updater.run(
            str(previous), new_graph, sorted(new_graph), {"old_commit": "old", "new_commit": "new"}
        )
    )
    assert record.outcome == "incremental"
    assert "Chinese (Simplified)" in prompts[0]
    updated_tree = L.load_module_tree(str(docs))
    assert set(updated_tree) == {"身份认证", "OAuth认证"}
    assert orphan in T.tracked_ids(updated_tree)
    assert (docs / "身份认证.md").exists() and (docs / "OAuth认证.md").exists()
    assert not (docs / "身份认证生命周期.md").exists()
