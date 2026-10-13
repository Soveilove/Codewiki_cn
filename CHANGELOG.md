# 更新日志

本文件记录 CodeWiki 的重要变更，格式参考 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)，版本号遵循[语义化版本](https://semver.org/)。

## [Unreleased]

### 新增

- **Rust 分析器。** 将结构体、联合体、枚举、Trait、独立函数和 impl / Trait 方法识别为组件，并提取 Trait 实现、字段类型、结构体字面量和调用关系。跳过 `#[cfg(test)]` 模块，并将 `build.rs` 识别为构建文件。
- **Dart / Flutter 分析器。** 类、Mixin、扩展、枚举、顶层函数与 getter、方法成为组件，带继承、字段类型、实例化和调用边。`package:`/相对导入、export 和 `part` 文件决定名称解析到哪个定义，库私有名称不会解析到库外；导入不可见的名称直接丢弃而不猜测。分析树中没有 `pubspec.yaml` 时 `package:` 导入无法解析，Dart 调用退化为跨 Dart 文件的名称匹配（带警告）。Flutter 感知：widget 和 State 类会被标记类型，`build()` 中的实例化产生 widget 到 widget 的边，`ref.watch`/`ref.read` 把 widget 链接到 Riverpod provider（含 `@riverpod` 代码生成），GoRouter 路由链接到对应页面。生成的 `*.g.dart`/`*.freezed.dart` 与 `.dart_tool/` 被忽略，Dart 模块获得 Flutter 专属的文档指导。
- **团队提示词生效可见。** `codewiki generate` 开头始终报告 `.codewiki/prompts.md` 的状态：已加载（含生效字段摘要）、字段名无法识别（列出键名和可用字段）、YAML 解析失败（带行号）或整体未生效。不再需要 `--verbose` 才能确认约定是否注入。
- **团队约定变更检测。** 生成文档时记录 `prompts.md` 的内容指纹（SHA256，存 `metadata.json`）。`--update` 发现约定变更（含删除文件）时警告“新旧约定混排”风险并询问是否全量重新生成；选择继续增量则基线不动，提醒持续到全量过一次。旧产物无指纹时静默记录基准，不误报。
- **IDE 驱动生成复用 CLI 的业务规则。** `codewiki mcp` 的六类提示词现在与 CLI 共用业务规划规则、源码证据纪律和唯一主解释归属；`analyze_repo` 补齐每个解析到的源码文件的分析入口。推理与写作仍由 IDE 提供，MCP 不调用额外模型。
- **移除 Mermaid 图的独立语法校验。** 该功能调用本地 JS 解析器，要求每台机器都装 Node.js 18+ 和 npm，只为复查寥寥几张图；而失败的校验本来就不阻塞生成，图内容一律保留。现在改为由生成模型在写同一步内自查图表语法（提示词中的自审要求保留），文档生成不再需要任何 JavaScript 运行时。删除 `mermaid-parser-py` 依赖、`mermaid_validator.cjs` 与 `utils.py` 中的校验实现，移除 `MERMAID_VALIDATE` 环境变量。MCP 的 `mermaid_validation` 字段保留，但如实返回"未做独立语法校验"，不改变响应结构。

### 变更

- **MCP 自动加载团队提示词。** `analyze_repo` 与旧 `generate_docs` 读取同一个 `.codewiki/prompts.md`，字段解析和优先级与 CLI 一致，并返回 `team_prompt` 生效诊断。`get_prompt` 传入 `session_id` 即自动应用该会话固定的语言、约定、布局和业务证据；不传会话时仍可查询通用模板。长提示词通过 `content_file` 返回，不截断。
- **MCP 不再覆盖 CLI 的更新基准。** 依赖图和生成元数据只在页面与源码归属完整时由 `close_session` 发布；此前仅调用 `analyze_repo` 就会写入依赖图，使后续 `--update` 误判相关代码已处理。收尾会校验受影响页面、新旧模块树差异、退役页面和元数据快照冲突，写盘失败自动回滚。
- **`close_session` 可报告未完成。** 页面、归属或基准不一致时返回 `incomplete` 并保留会话，可补写后重试；传 `discard: true` 放弃，已写页面保留但不推进基准。新增 `edit_doc_file` 的 `delete` 命令用于清理退役模块页面，支持 `undo` 恢复。
- **新旧输出目录自动发现。** 未显式指定输出目录时，CLI 与 MCP 优先沿用 `.codewiki/` 中的模块树，否则发现并沿用旧 `docs/`；不会自动搬迁，也不静默生成第二套文档。显式 `--output` / `output_dir` 行为不变。
- **页面工具保护团队资产。** `write_doc_file` 和 `edit_doc_file` 拒绝写入 `prompts.md`、`temp/` 和 `sessions/`，只允许删除模块树声明的生成页面。
- **文档按模块树组织**（[#125](https://github.com/FSoft-AI4Code/CodeWiki/issues/125)）。每个模块页面现在与其子模块目录并列，例如 `auth.md`、`auth/login.md`，不再全部放在同一层。页面链接使用相对路径；生成后会移动放错位置的页面并修正链接。`--flat` 仍可保留旧布局。布局写入 `metadata.json`，`--update` 会沿用已有布局，因此旧版平铺文档仍保持平铺。GitHub Pages 查看器、Web 应用和 MCP 工具（`processing_order.json` 中的 `doc_path`）也会采用对应布局。
- **README 和使用指南中文化。** 将常用配置、参数、团队提示词、MCP、增量更新和 Docker 使用说明整理为中文，并明确 CLI 与 MCP 都会读取 `.codewiki/prompts.md`，MCP 无需再手工把约定传给 IDE。
- **组件 ID 与相对路径在源头归一化为 POSIX 分隔符。** 各语言分析器经共享的 `posix_relpath` 取值，仓库分析器文件树改用 `as_posix`，Windows 与 macOS 生成一致的组件 ID（`src/lib.rs::App`），跨系统增量更新与产物比对可用；删除 `prompt_template` 下游反斜杠补丁。
- **增量更新补齐业务规则传递。** update 入口显式设置业务分析开关，pydantic 后端自建运行时时继承后端开关；此前增量更新新建/重写的页面会丢失业务主题化、唯一主说明和证据纪律，与 init 页面风格不一致。
- **update 补丁 agent 与 init 共享纪律常量。** 唯一主说明与证据纪律提为 `OWNERSHIP_AND_EVIDENCE` 共享常量，两条路径共同引用；update 侧追加主动去重条款，修补时把复述换成一句摘要加链接，增量更新从此降低而非增加重复归属。

### 修复

- **文档可能被写成乱码而无法打开。** `str_replace_editor` 原先先按系统默认编码读取（中文 Windows 是 GBK），再以 UTF-8 写回。像“配置文件解析模块”这类中文，UTF-8 字节恰好也是合法 GBK，于是读取不报错、内容被静默替换成 `閰嶇疆鏂囦欢…` 并写回，页面就此损坏。现在按 BOM → 严格 UTF-8 → latin-1 的顺序读取，且每个文件各自记住编码，不再因上一个文件而错配。
- **MCP 变更检测会漏掉只改约定或只删组件的情况。** 现在检测同时使用 Git、上次保存的依赖图和文件修改时间，并区分缺页、空页与不可读页；仅改团队约定时返回 `requires_full_regeneration`，不再报告“文档已是最新”。基准使用分析时的提交与约定快照，而不是结束会话时的新值。
- **MCP 模块树与文档生成不再静默丢内容。** `save_module_tree` 拒绝未知 ID、不安全模块名和未分配的源码入口，暂存在会话内直到收尾；`edit_doc_file` 拒绝空匹配串，避免误改整页。
- **Mermaid 校验测试不再互相抢状态。** CLI 生成器会永久修改共享 logger 的 `propagate` 和级别且不还原，导致其后运行的测试捕获不到日志；测试 fixture 现在防御性还原。回归基线恢复全绿（527 passed, 1 skipped）。
- **新增 MCP 生成契约回归**（`tests/test_mcp_generation_contract.py`，40 项）：覆盖零模型配置分析、六类提示词的语言与约定传递、长提示词文件通道、未完成会话可重试、约定变更需真实全量重建、基准不推进、团队资产保护、真实 stdio 会话生命周期，以及 MCP → CLI → MCP 在同一套产物上交替更新。回归基线 584 passed, 1 skipped。
- **增量更新现在把 doc-comment 变更算作组件变更。** 对比依赖图时，组件文档注释的修改与代码修改一样触发所属页面重新生成；此前仅改注释不会更新文档。

### 移除

- 移除 `codewiki generate --instructions` 和 `codewiki config agent --instructions`。自由文字要求（`custom_instructions`）现在只写在仓库的 `.codewiki/prompts.md` 正文中，避免多个来源互相覆盖导致命令行输入被静默丢弃。

## [2.0.0] - 2026-09-18

这是 1.0.1 之后的首次发布。本节记录期间合入 `main` 分支的变更。

### 新增

- **构建与配置文件分析**（#106）。构建、CI、容器、打包、项目清单、配置、Schema 和脚本文件成为依赖图节点，并与引用的代码建立启发式关联。每个模块提示中都包含确定性的辅助文件索引；聚类遗漏较多时，会补入 **Build, Deployment and Configuration** 模块。相关参数：`--artifacts` / `--no-artifacts`、`--artifact-token-budget`、`--with-prose`、`--artifact-exclude`。
- **组件级增量更新器**（#111）。`--update` 会将保存的依赖图与当前代码对比、修复模块树（重命名、删除、新组件路由、模块增长后的重新聚类），为叶子模块生成变更报告，并让 Agent 更新受影响页面及相关页面。改动过多时会改为全量生成；每项决定写入 `update_record.json`。相关参数：`--update-rung`、`--tau-ren`、`--tau-nb`、`--tau-grow`、`--tau-full`、`--tau-tree`、`--k-hop`、`--max-diff-tokens`。
- `--compare-to <commit>`：指定增量更新的 Git 基准（#67）。
- **更完整的代码依赖图。** 在以函数为主的项目中，独立函数也会成为文档组件。C、C++、Java（#64、#68）和 C# 的调用分析增加作用域、命名空间、头文件和导入关系；外部符号表用于排除库调用。
- **新增语言分析器：** Kotlin（#41）、PHP、Ruby（#97）、Scala（#108）。
- **订阅服务：** 新增 `claude-code` 和 `codex`（#60、#70），通过本机 CLI 和 `caw` 发起模型调用，不需要 API 密钥。
- 新增 Atlas Cloud（#69）、Azure OpenAI（#49）和 AWS Bedrock（#40）服务。
- **MCP 服务**（#9、#66）：提供八个不直接调用模型的工具（`analyze_repo`、`read_code_components`、`write_doc_file`、`edit_doc_file`、`save_module_tree`、`get_processing_order`、`get_prompt`、`close_session`），并保留旧版 `generate_docs` 和 `get_module_tree`。同时加入文件会话目录、模块级变更检测、`codewiki mcp` 命令及 Claude Skill。
- Agent 调用支持提示词缓存，可用 `--prompt-caching` / `--no-prompt-caching` 切换（#81）。
- 支持 `.gitignore` 过滤，可用 `--use-gitignore` / `--no-gitignore` 切换（#80）。
- 新增 `--max-tokens`、`--max-token-per-module`、`--max-token-per-leaf-module`、`--max-depth`、`--fallback-model`、`--instructions` 和 `codewiki config agent` 默认设置。
- 新增 `SECURITY.md` 和 `LICENSE`（#107）。CI 工作流在 Python 3.12 上运行 pytest 和 Ruff。
- 新增 `CHANGELOG.md` 和 `guides/` 使用指南目录。

### 变更

- `--update` 使用组件级更新器。1.x 的文件级失效方式仍可通过 `--update-rung 0` 使用。
- 默认分析辅助文件；使用 `--no-artifacts` 可恢复 1.x 的仅分析代码行为。
- `metadata.json` 中的 `generator_version` 改为读取包版本，不再硬编码。
- 手写指南从仓库根目录和 `docker/` 移至 `guides/`：`DEVELOPMENT.md` → `guides/development.md`、`IDE_DRIVEN_GUIDE.md` → `guides/mcp-ide-mode.md`、`docker/DOCKER_README.md` → `guides/docker.md`。新增 `guides/cli-reference.md`、`guides/providers.md`、`guides/artifact-aware-generation.md` 和 `guides/incremental-updates.md`。
- README 围绕 2.0 版本重新编排。基准测试部分使用相同设置重新测试七个 CodeWikiBench 仓库中的 DeepWiki 和 CodeWiki 1.0。
- 组件 ID 统一为 `path::name`（#48）。固定 Ruff 规则集版本；说明 Node.js 是安装期还是运行期依赖。
- `pyproject.toml` 中的项目链接改为真实仓库地址。

### 修复

- 修复大型仓库的总览和模块提示超过 Codex 输入限制的问题（#92、#94）。
- 修复恢复生成时跳过缺失文档模块的问题（#94）。
- 修复整仓库模式下 `--update` 未重新生成已失效模块的问题（#100）。
- 修复 CLI 适配器未传递 `commit_id`，导致 `--update` 实际执行全量生成的问题。
- 修复叶子节点筛选遗漏 `handleInvalidInput` 等标识符的问题（#102）；叶子节点缩减改为 INFO 级日志（#103）。
- 修复 C 项目未生成叶子节点的问题。
- 排除 `node_modules`；补齐缺失的运行依赖；处理 OpenAI 兼容代理返回的非标准响应。
- 修复 MCP Mermaid 校验卡住的问题；增加 MCP 路径遍历保护和编辑历史上限。
- 修复 README 星标历史图表（#95）和 mermaid-py 行为说明（#110）。

## [1.0.1] - 2025-10-18

- 发布 CLI：`codewiki config set`、`codewiki config show`、`codewiki config validate`、`codewiki generate`，以及 `--output`、`--create-branch`、`--github-pages`、`--no-cache`、`--include`、`--exclude`、`--focus`、`--doc-type`、`--verbose` 参数。
- API 密钥优先保存到系统密钥库，无法使用时保存到文件。

## [1.0.0] - 2025-09

- 随论文发布首个版本，包含分层模块组织、递归 Agent、Mermaid 图表、七种语言（Python、Java、JavaScript、TypeScript、C、C++、C#）、Web 应用、Docker 镜像和 CodeWikiBench。

[2.0.0]: https://github.com/FSoft-AI4Code/CodeWiki/releases/tag/v2.0.0
