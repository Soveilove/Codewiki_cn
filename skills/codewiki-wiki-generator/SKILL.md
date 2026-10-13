---
name: codewiki-wiki-generator
description: "使用 CodeWiki MCP 工具分析代码仓库并生成 Wiki 文档。用户要求生成项目文档、代码说明、仓库结构分析时使用。需要已配置 CodeWiki MCP 服务。"
version: 2.0.0
---

# 使用 CodeWiki 生成仓库文档

CodeWiki MCP 提供仓库分析、源码读取、模块结构管理和文档读写工具。模型推理由当前 IDE 提供，CodeWiki MCP 工具本身不需要单独配置模型。

## MCP 会话文件

大段分析数据保存在文件中，MCP 响应返回文件路径和简要统计。需要使用自己的文件读取工具查看完整内容。

会话目录：`{repo_path}/.codewiki/sessions/{session_id}/`

常见文件：

- `component_index.json`：组件 ID、类型和源码路径。
- `leaf_nodes.json`：可用于模块划分的叶子组件 ID。
- `languages.json`：仓库语言统计。
- `summary.json`：分析摘要。
- `planning_evidence.json`：业务规划证据与关系线索。
- `generation_context.json`：固定的语言、团队约定和目录布局。
- `prompt_<type>_<id>.txt`：完整提示词，避免 MCP 长响应截断。
- `changes.json`：已有文档时的变更检测结果。
- `processing_order.json`：模块页面的生成顺序。
- `sources/`：由 `read_code_components` 写入的组件源码。

## 开始前

确认 MCP 工具列表中有以下工具：`analyze_repo`、`read_code_components`、`write_doc_file`、`edit_doc_file`、`save_module_tree`、`get_processing_order`、`get_prompt`、`close_session`。

如果没有这些工具，请提示用户安装 CodeWiki 并配置 MCP 服务；不要假装调用了尚不可用的工具。

源码方式安装：

```bash
git clone https://github.com/FSoft-AI4Code/CodeWiki.git
cd CodeWiki
pip install -e .
```

MCP 配置：

```json
{"mcpServers":{"codewiki":{"command":"codewiki","args":["mcp"]}}}
```

## 团队提示词

`analyze_repo` 会自动读取仓库的 `.codewiki/prompts.md`，与 CLI 使用同一字段解析和优先级。检查返回的 `team_prompt` 状态；后续 `get_prompt` 必须携带 `session_id`，才能自动应用会话固定的语言、团队约定、业务解释归属和目录布局。大提示词通过 `content_file` 返回，必须读取完整文件。

该文件可以约定文档语言、领域术语、内容范围和写作方式，但不能替代源码依据和主解释归属规则。页面工具禁止修改 `prompts.md`、`temp/` 和 `sessions/`。无会话的 `get_prompt` 只查询通用模板，不会读取团队约定。

## 生成流程

除第一步外，所有工具调用都要带上 `analyze_repo` 返回的 `session_id`。

### 1. 分析仓库

调用 `analyze_repo`，提供目标仓库的绝对路径。新仓库默认输出到 `.codewiki/`；已有 `.codewiki/module_tree.json` 时沿用它，否则自动发现旧 `docs/module_tree.json`。只有用户指定了其他位置时才传 `output_dir`。显式语言通过 `language` 传入，但团队语言对新生成拥有更高优先级，增量更新沿用已记录的语言。

```json
{"repo_path":"<仓库绝对路径>"}
```

记下返回的 `session_id`，并读取：

1. `{workspace_dir}/component_index.json`：完整组件列表。
2. `{workspace_dir}/leaf_nodes.json`：叶子组件 ID。
3. `planning_evidence.json`：规划用源码摘录与调用关系。摘录不是完成源码分析的证据。
4. `generation_context.json`：本会话固定的语言、布局和团队约定。
5. 返回的 `stats` 和 `team_prompt`：组件数量、源码语言分布及约定的生效诊断。

分析范围与 CLI 一致，`leaf_nodes.json` 中每个入口必须有主说明归属。团队文件无需重复手动注入。

### 2. 整理模块

带 `session_id` 调用 `get_prompt` 获取 `cluster` 规则，结合规划证据按业务能力、流程和规则组织主题；批次边界不是页面边界。用 `read_code_components` 获取源码文件路径，并用文件读取工具查看完整实现。模型与推理仍由 IDE 提供，MCP 不调用额外模型。

保存模块树时，必须原样保留组件 ID，包括 `::` 分隔符。示例：

```json
{
  "session_id": "<session_id>",
  "module_tree": {
    "订单处理": {
      "components": ["src/orders/service.py::OrderService"],
      "children": {}
    }
  }
}
```

调用 `save_module_tree` 后，读取它返回的 `processing_order_file`，后续按其中的 `doc_path` 写页面。

### 3. 编写模块页面

从叶子模块开始，逐层向上处理。

对于每个叶子模块：

1. 用 `get_prompt` 获取 `system_leaf` 提示，提供 `module_name` 和 `module_path`。
2. 用 `read_code_components` 读取该模块的组件，再查看返回的源码文件。
3. 只写能从源码或相关配置中确认的行为。必要时直接读取仓库中的其他文件补充上下文。
4. 页面说明模块职责、主要流程、关键状态和组件关系。只在确有帮助且内容有依据时添加 Mermaid 图。
5. 用 `write_doc_file` 按 `processing_order.json` 中的 `doc_path` 保存。

写完页面后自查一遍 Mermaid 语法：图类型拼写正确、围栏闭合、标签里的括号和引号成对。工具不会替你校验图表。不要声称图表已通过校验或渲染验证。

对于父模块：

1. 读取其子模块页面。
2. 用 `get_prompt` 获取 `overview_module` 提示。
3. 汇总子模块职责和关系，不要重复抄写所有细节。
4. 按模块的 `doc_path` 保存页面。

### 4. 编写仓库总览

调用 `get_prompt` 获取 `overview_repo` 提示，读取已生成的模块页面，再用 `write_doc_file` 保存 `overview.md`。总览应介绍仓库用途、主要模块和重要关系，并提供指向模块页面的相对链接。

### 5. 结束会话

调用 `close_session`：

```json
{"session_id":"<session_id>"}
```

页面完整且本次受影响范围均已更新时，此调用才发布模块树、依赖图、引用索引和 `metadata.json`，并清理工作区。返回 `incomplete` 时按 `missing_pages`、`pending_pages` 和归属缺口补齐后重试；不要把它当作成功。用户放弃时传 `discard: true` 清理会话，已写页面保留但不推进基准。文件完整不代表业务内容已通过人工验收。

## 更新已有文档

当 `analyze_repo` 返回 `changes` 时，先检查 `no_changes`：

- `true`：告诉用户文档已是最新，不要重复重写。
- `false`：先检查 `requires_full_regeneration`；为 `true` 时询问用户是否全量重建，确认后重新 `analyze_repo(regenerate=true)`，应用新约定并重写全部页面。不能仍宣称已最新。
- 普通代码变化：查看 `affected_modules`、`cascade_modules` 和 `missing_pages`，更新受影响页面。存在 `unowned_component_ids` 时补齐主题归属并保存修复后的模块树。

尽量用 `edit_doc_file` 局部修改已有页面，保留未受影响的内容。主题删除或改名时，用 `command="delete"` 清理收尾报告的 `retired_pages`，可以 `undo` 恢复；不要删除人工页面或团队资产。处理完父模块后检查 `overview.md`，最后调用 `close_session`。

MCP 的变更判断按模块进行：模块里任一组件改变，整个模块都会被标记。CLI 的 `codewiki generate --update` 使用更细的组件级更新方式。

## 写作要求

- 文档语言按用户要求；未指定时使用英文。团队提示词或仓库已有文档明确约定语言时，按约定执行。
- 只写能由源码、配置、测试或仓库文档支持的内容；不确定时标为“待确认”，不要猜测。
- 保留代码标识符、组件 ID 和路径原文。
- 页面按模块树存放。每条链接使用相对路径，例如从 `auth.md` 链接到 `auth/login.md` 时写成 `[登录](auth/login.md)`。
- Mermaid 节点 ID 使用英文、数字和下划线；展示文字可以使用目标文档语言。优先用简单的 `graph TD` 或 `graph LR`。
- 示例只使用仓库中存在的接口、字段和命令。

## 常见问题

- **会话过期：** 重新调用 `analyze_repo`，使用新返回的 `session_id`。
- **分析范围太大：** 在 `analyze_repo` 中传入 `include_patterns` 或 `exclude_patterns`。
- **组件 ID 不匹配：** 以 `component_index.json` 中的原始 ID 为准，保留 `::`。
- **图表校验失败：** 按错误信息修正语法，再调用 `edit_doc_file`。
