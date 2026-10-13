# 在 IDE 中使用 CodeWiki（MCP）

CodeWiki 可以作为 MCP 服务接入支持 MCP 的 IDE。模型由 IDE 提供；CodeWiki 提供代码分析、模块结构管理和文档读写工具，因此不必在 CodeWiki 中再配置模型 API。

如果希望由 CodeWiki 自己调用模型并一次性生成文档，请使用 [CLI](cli-reference.md)。

## 安装并连接 IDE

```bash
pip install git+https://github.com/FSoft-AI4Code/CodeWiki.git
codewiki --version
```

在 IDE 的 MCP 配置中添加：

```json
{
  "mcpServers": {
    "codewiki": {
      "command": "codewiki",
      "args": ["mcp"]
    }
  }
}
```

不同 IDE 的配置入口：

| IDE | 配置入口 |
| --- | --- |
| Cursor | Settings → MCP → Add Server |
| Claude Desktop（macOS） | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Code | `claude mcp add codewiki -- codewiki mcp` |
| CodeBuddy | 设置中的 MCP 配置 |
| 其他 stdio MCP 客户端 | 使用上面的命令和参数 |

连接后，可以直接对 IDE 说：

```text
请分析当前仓库，生成一套简体中文业务文档，保存在默认的 .codewiki/ 目录。
```

新仓库默认把文档产物放在 `.codewiki/` 下；未指定 `output_dir` 时，优先沿用 `.codewiki/` 中的模块树，否则发现并继续使用旧 `docs/` 中的模块树，不自动搬迁。分析会话始终位于目标仓库的 `.codewiki/sessions/`。显式 `output_dir` 保持原行为。

## 可用工具

CodeWiki 提供八个 IDE 驱动工具和两个旧版工具。通常使用前八个工具，让 IDE 自己分析并逐页生成文档。

| 工具 | 用途 |
| --- | --- |
| `analyze_repo` | 分析仓库，加载团队约定，输出业务规划证据并检查已有文档的变更 |
| `read_code_components` | 将指定组件源码写入会话目录，返回文件路径 |
| `get_prompt` | 获取六类提示词；传 `session_id` 自动应用语言、团队约定、业务归属和布局 |
| `save_module_tree` | 校验组件覆盖与主解释归属，暂存模块树并计算页面生成顺序 |
| `get_processing_order` | 获取从叶子模块开始的页面顺序 |
| `write_doc_file` | 创建 Markdown 页面 |
| `edit_doc_file` | 修改或撤销已有页面的编辑 |
| `close_session` | 写入 `metadata.json` 并清理会话 |
| `generate_docs`（旧版） | 用 CodeWiki 自己的 Agent 一次性生成，需要先配置模型 |
| `get_module_tree`（旧版） | 读取现有的 `module_tree.json` |

## 一次生成的步骤

1. 调用 `analyze_repo`，保存返回的 `session_id`，检查 `team_prompt` 状态和有效语言。
2. 读取 `component_index.json`、`leaf_nodes.json`、`planning_evidence.json` 和 `generation_context.json`。分析范围与 CLI 一致，每个解析到的源码文件至少有一个业务分析入口。
3. 带 `session_id` 调用 `get_prompt` 获取 `cluster` 规则，按业务能力、流程和规则组织主题，而不是直接复制目录；读取完整源码后调用 `save_module_tree`。组件 ID 必须原样保留，不能保存仅分配部分入口的模块树。
4. 按 `processing_order.json` 先写叶子模块，再根据子页面写父模块。叶子页面使用同一会话的 `system_leaf` 和 `user` 提示词；如果响应的 `content` 为 `null`，读取完整 `content_file`。页面按 `doc_path` 保存，已有页面使用 `edit_doc_file`。
5. 带 `session_id` 获取 `overview_repo` 提示，生成 `overview.md`。小仓库可保存空模块树，全部源码由 `module_path: []` 的作者提示词分析并直接写 `overview.md`。
6. 调用 `close_session`。页面及受影响范围完整时才发布模块树、依赖图、引用索引和生成元数据；返回 `incomplete` 时会话保留，按返回的页面清单补写后重试。

示例会话文件：

```
.codewiki/sessions/<session_id>/
├── component_index.json
├── leaf_nodes.json
├── languages.json
├── summary.json
├── planning_evidence.json       # 与 CLI 共用的业务规划证据
├── generation_context.json      # 固定的语言、约定和布局
├── prompt_<type>_<id>.txt        # 获取提示词后出现
├── changes.json                 # 有已有文档时出现
├── processing_order.json        # 保存模块结构后出现
├── module_tree_validation.json  # 保存模块结构后出现
└── sources/<component_id>.src   # 读取源码后出现
```

大文件内容通过会话目录传递，MCP 响应只返回路径和简要统计。会话两小时无活动后过期，最多保留十个会话；过期或被淘汰的会话文件会清理。`write_doc_file` 和 `edit_doc_file` 不能写到会话输出目录以外。

## 更新已有文档

已有 `metadata.json` 和 `module_tree.json` 时，`analyze_repo` 返回 `changes`。检测结合 Git 提交、未提交/未跟踪文件以及已保存依赖图；旧产物没有依赖图时退回文件修改时间。仅分析不会覆盖 CLI 使用的依赖图基准。

- `no_changes: true`：无需改页面，可直接结束会话，不推进基准。
- 普通代码变化：更新 `affected_modules`、`cascade_modules` 和 `overview.md`，恢复 `missing_pages`。有 `unowned_component_ids` 时补齐主题归属并保存修复后的树。删除或改名主题后用 `edit_doc_file(command="delete")` 清理 `retired_pages`，可用 `undo` 恢复；工具只允许删除模块树声明的生成页面。
- `requires_full_regeneration: true`：团队约定变化或基准不可判定，先由 IDE 确认是否全量生成，再以 `regenerate: true` 创建会话、应用新约定并重写全部页面；不要仍然宣称文档已最新。

MCP 按模块提示更新范围，CLI 的 `codewiki generate --update` 按组件更新。完成会话后保存的语言、模块树、依赖图、引用索引及 `metadata.json` 可以供另一种模式继续使用。生成基准使用分析时的提交和约定快照，而不是结束会话时的新值。

`close_session` 返回 `incomplete` 时不会更新基准，也不会清理会话。可以补齐页面后重试，或传 `discard: true` 放弃：已写页面保留，但不发布新树和新基准。文件完整性校验不等于模型内容的业务正确性验证。不要同时使用 CLI 和 MCP 写同一输出目录；收尾会拒绝覆盖其他流程已修改的元数据。

## 团队文档约定

`analyze_repo` 与旧 `generate_docs` 自动加载仓库的 `.codewiki/prompts.md`，字段解析及优先级与 CLI 相同。新会话返回 `team_prompt` 的正常、部分生效或无效诊断。

后续 `get_prompt` 传入 `session_id`，六类提示词都会自动应用这次分析固定的语言和约定。无会话调用仍可查询通用模板，但不会猜测目标仓库或加载约定。CLI 的并发和模型配置不控制 IDE 自己的推理。

页面工具禁止编辑团队 `prompts.md` 以及 `temp/`、`sessions/` 工作资产。仅约定变化也会返回需要更新，不能当作代码无变化而直接宣告完成。

提示词模板和字段说明见[参数设置与团队提示词](settings-and-prompts.md)。

## 常见问题

**MCP 服务启动失败**

检查是否已安装 CodeWiki 及其依赖。克隆源码开发时，在仓库根目录运行 `pip install -e .`。

**仓库太大，分析范围过宽**

在 `analyze_repo` 中传入 `include_patterns` 或 `exclude_patterns`，缩小分析范围。

**Mermaid 图显示异常**

没有独立的图表校验步骤，工具不会返回图表错误。写页面时自行检查 Mermaid 语法：保持简单、标签成对、括号闭合。预览时如果图表渲染失败，按渲染器报错修改，再用 `edit_doc_file` 更新。

**会话过期**

重新调用 `analyze_repo`，它会创建一个新会话。重新生成前先确认需要的文档已保存。

**想在 IDE 中使用旧的一次性生成方式**

先用 `codewiki config set` 配置模型服务，再让 IDE 调用 `generate_docs`。团队约定与运行配置都会生效；已有代码或约定变化时返回 `needs_confirmation`，确认后传 `regenerate: true` 备份旧产物并全量生成。需要局部更新时改用 IDE 驱动工具。