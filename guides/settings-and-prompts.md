# 参数设置与团队提示词

先配置模型服务，再决定是否要保存个人默认项。团队文档约定放在仓库的 `.codewiki/prompts.md`，提交后其他成员也能使用。

## 1. 配置模型服务

使用 API 服务时，填写服务地址、密钥和模型名：

```bash
codewiki config set --provider openai-compatible --api-key YOUR_API_KEY --base-url https://api.example.com/v1 --main-model YOUR_MODEL --cluster-model YOUR_MODEL
```

如果使用 Claude Code 或 Codex 订阅，先登录对应 CLI，再设置服务类型和模型：

```bash
codewiki config set --provider claude-code --main-model claude-sonnet-4-6 --cluster-model claude-sonnet-4-6
```

检查配置：

```bash
codewiki config show
codewiki config validate
```

不同服务的地址、模型名和认证方式见[模型服务配置](providers.md)。API 密钥由系统密钥库保存；不要把密钥写进仓库文件或提示词。

## 2. 生成文档

进入要分析的仓库目录后运行：

```bash
codewiki generate
```

默认结果写入仓库的 `.codewiki/`。想改输出目录时使用 `--output`：

```bash
codewiki generate --output wiki-docs
```

常用参数：

| 想做什么 | 命令 |
| --- | --- |
| 只分析某类文件 | `codewiki generate --include "*.ts,*.tsx"` |
| 排除测试目录 | `codewiki generate --exclude "tests,fixtures"` |
| 多写几个目录 | `codewiki generate --focus "src/orders,src/payments"` |
| 指定文档类型 | `codewiki generate --doc-type architecture` |
| 指定文档语言 | `codewiki generate --language zh` |
| 补充文字要求 | 写在 `.codewiki/prompts.md` 正文里，见下文[添加团队提示词](#4-添加团队提示词) |
| 更新已有文档 | `codewiki generate --update` |

这些参数只影响当前运行。参数完整列表见 [CLI 参数参考](cli-reference.md)。

## 3. 设置自己的常用默认项

`codewiki config agent` 保存的是当前用户的默认设置，不会写入仓库。适合不想每次都重复输入的个人习惯：

```bash
codewiki config agent --language zh
codewiki config agent --exclude "tests,fixtures"
```

查看或清除这些默认项：

```bash
codewiki config agent
codewiki config agent --clear
```

运行 `codewiki generate` 时，本次命令参数会覆盖个人默认项。团队文件只覆盖自己明确设置的字段；没写的字段继续使用命令参数、个人默认项或内置默认值。

## 4. 添加团队提示词

在仓库根目录创建 `.codewiki/prompts.md`。文件可包含 YAML 参数和普通 Markdown 约定：

```markdown
---
language: zh
doc_type: 业务知识
focus_modules:
  - src/orders
exclude_patterns:
  - "**/test/**"
---

# 文档约定

- 先说明用户场景，再说明规则和处理流程。
- 解释状态变化时，写清触发条件和后续结果。
- 提到接口、页面或数据表时，标出对应的代码路径。
- 源码没有说明的内容不要猜；有疑问就写"待确认"。
```

CodeWiki CLI 每次生成时都会读取这个文件。团队成员拉取仓库后无需单独配置，就能使用同一套约定。

### 可以设置哪些字段

| 字段 | 用途 | 示例 |
| --- | --- | --- |
| `language` | 文档文字语言；全新生成时也影响新模块命名 | `zh`、`en` |
| `doc_type` | 文档类型 | `api`、`architecture`、`user-guide`、`developer`，也可写团队自定义类型 |
| `focus_modules` | 优先详细说明的目录或模块 | `- src/orders` |
| `include_patterns` | 只分析匹配的文件 | `- "*.ts"` |
| `exclude_patterns` | 排除文件或目录 | `- "**/test/**"` |
| `artifact_exclude` | 排除构建、配置等辅助文件 | `- "generated/**"` |

Markdown 正文会作为补充说明。字段名必须按表格填写；不认识的 YAML 字段会被忽略。某个字段没有写在文件里时，仍使用命令行或个人配置中的值。

### 配置优先顺序

普通提示字段按下列顺序取值；同一字段出现多处时，排在前面的设置优先：

1. `.codewiki/prompts.md` 中明确填写的字段
2. 本次 `codewiki generate` 命令参数
3. `codewiki config agent` 保存的个人默认项
4. CodeWiki 内置默认值

Markdown 正文对应 `custom_instructions` 字段，是自定义文字要求的唯一入口：命令行和个人默认项不再提供自由文本，正文内容会直接作为补充说明。团队文件不会替换 CodeWiki 内置的系统提示词，也不控制并行度、重试次数等机器运行参数。

`--update` 通常沿用原文档语言，并忽略个人配置中的默认语言；命令行显式指定不同的 `--language` 会报错。不过，团队文件里的 `language` 字段优先级更高。若要切换整套文档语言，请指定一个新的空输出目录重新生成；不要只修改旧目录中的 `prompts.md` 后运行 `--update`，否则本次更新的页面可能与未更新页面语言不同。

### 可直接复制的精简模板

如果只想规定文字风格，可以不写 YAML，只保留正文：

```markdown
# 团队文档约定

- 使用简体中文，技术名词和代码标识符保留原文。
- 每页开头用一两句话说明这个模块负责什么。
- 说明重要流程时，按实际执行顺序写步骤。
- 所有结论都要能在代码中找到依据；不要补写源码没有的行为。
- 示例只使用仓库里真实存在的接口和字段。
```

提交 `.codewiki/prompts.md` 后，先运行一次 `codewiki generate` 检查效果，再按生成结果调整约定。CLI 和 MCP 的 `analyze_repo` / `generate_docs` 都会自动读取该文件。IDE 驱动时，后续 `get_prompt` 传入 `session_id` 才会应用会话中的团队约定；无会话调用仅返回通用模板。MCP 的 `team_prompt` 回执使用同样的正常、部分生效、无效状态，不会在响应中内联长篇正文。

### 生效情况每次生成都会显示

不用加任何参数，`codewiki generate` 开头就会报告这个文件的状态：

- 正常：`✓ 已加载团队提示词 .codewiki/prompts.md:语言 zh、排除模式 2 项、正文约定 480 字`
- 字段名写错：`⚠ 团队提示词 .codewiki/prompts.md 部分生效:语言 zh、正文约定 4 字。无法识别的字段:exclude_paterns（可用:language, doc_type, focus_modules, include_patterns, exclude_patterns, artifact_exclude）` —— 按提示改正字段名即可，写错的字段不会生效。
- YAML 格式错误：`⚠ 团队提示词 .codewiki/prompts.md 部分生效:正文约定 4 字。YAML 解析失败（第 3 行），结构化字段未生效` —— 按行号检查缩进和标点（YAML 不能用中文冒号）。若正文也为空，则整个文件不生效，会有 `未生效` 提示。

### 改了约定之后，增量更新会提醒

生成文档时会记录 `prompts.md` 的内容指纹。之后运行 `codewiki generate --update` 时如果发现约定变了（包括删除文件），会提示：

> ⚠ 团队提示词已变更(上次生成后 .codewiki/prompts.md 有改动)。增量更新只重写受代码变更影响的页面,未受影响的页面仍按旧约定编写,同一份文档会出现新旧约定混排。要全量重新生成吗?[y/N]

按 `y` 回车会备份旧生成产物、重新聚类，并按新约定重写页面。默认输出目录的备份位于 `.codewiki/.prev-<commit>/`，自定义输出目录的备份位于相邻的 `<输出目录>.prev-<commit>/`；团队提示词和 `.codewiki/sessions/` 不会移走。只有重建完成后才更新指纹基线。

直接回车（默认 N）继续增量更新；旧指纹保持不变，提醒会持续到全量重新生成，包括 `--update-rung 0` 的旧文件级更新。原来没有团队文件、后来新增，以及删除文件都算变更。

第一次对旧文档运行 `--update` 时不会提醒（没有指纹可对比），即使代码无变化也会静默记录基准。指纹字段为 `null` 表示已确认当时没有团队文件，不等于没有基线。检测状态、新旧指纹、用户选择与实际执行方式会写入 `update_record.json` 的 `prompt_asset_change`，并合并到 `metadata.json` 的更新摘要中。

改约定后也可以不带 `--update`，确认覆盖后会真正重建。约定未变时普通生成仍保留已有页面的缓存续跑行为。