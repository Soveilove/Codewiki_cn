# 命令与参数参考

本页列出 `codewiki` 常用命令。想先完成安装、配置和首次生成，请看[快速设置与团队提示词](settings-and-prompts.md)。

| 命令 | 用途 |
| --- | --- |
| `codewiki generate` | 分析当前仓库并生成或更新文档 |
| `codewiki config set` | 设置模型服务和生成参数 |
| `codewiki config agent` | 保存个人常用的生成要求 |
| `codewiki config show` | 查看当前配置 |
| `codewiki config validate` | 检查配置并测试模型服务 |
| `codewiki mcp` | 启动供 IDE 使用的 MCP 服务 |

## 生成文档：`codewiki generate`

先切换到要分析的仓库目录，再运行命令。默认产物放在 `<仓库>/.codewiki/`。旧的 `docs/` 不会自动搬迁；继续更新旧文档时，明确指定原目录。

```bash
cd /path/to/your/repository
codewiki generate
codewiki generate --output docs --update
```

### 输出和运行方式

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--output`, `-o PATH` | `.codewiki` | 产物目录，可填写相对或绝对路径 |
| `--github-pages` | 关闭 | 同时生成可浏览文档的 `index.html` |
| `--create-branch` | 关闭 | 在 Git 仓库中创建文档分支；工作区需要干净 |
| `--no-cache` | 关闭 | 忽略已有结果，重新生成 |
| `--update` | 关闭 | 根据代码变更更新现有文档 |
| `--compare-to COMMIT` | 元数据中的提交 | 指定增量更新的 Git 基准；同时启用 `--update` |
| `--verbose`, `-v` | 关闭 | 显示详细进度和调试信息 |
| `--flat` | 关闭 | 把所有页面放在产物目录根部，而不是按模块层级建目录 |

### 选择分析范围和文档内容

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--include`, `-i PATTERNS` | 支持的文件类型 | 只分析匹配的文件；填写后会替换默认文件类型 |
| `--exclude`, `-e PATTERNS` | 内置忽略规则 | 额外排除文件或目录，不会取消内置忽略规则 |
| `--focus`, `-f PATHS` | 无 | 优先详细说明列出的目录或模块 |
| `--doc-type`, `-t TYPE` | 无 | 文档类型：`api`、`architecture`、`user-guide`、`developer` |
| `--language`, `-l LANG` | 英文；更新时沿用原语言 | 设置文档文字的语言，如 `zh`、`ja`、`vi`。新建文档时还会影响模块命名；更新已有文档时模块名和路径保持不变 |
| `--use-gitignore` / `--no-gitignore` | 开启 | 是否使用仓库中的 `.gitignore` 规则 |

多个路径或模式用逗号分隔。模式支持 `*.py`、`src/**/*.ts` 和 `*.{js,jsx}` 等写法。举例：

```bash
# 只分析 TypeScript 文件，并排除测试目录
codewiki generate --include "*.ts,*.tsx" --exclude "tests,fixtures"

# 优先说明订单和支付模块
codewiki generate --focus "src/orders,src/payments"

# 生成中文架构文档
codewiki generate --language zh --doc-type architecture
```

`--include` 和 `--exclude` 管文件范围；`--focus` 只是要求多写一些内容，不会自动排除其他模块。

### 构建、配置等文件

构建脚本、CI、容器、包清单、配置、Schema 和相关脚本默认会参与分析。需要调整时使用以下参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--artifacts` / `--no-artifacts` | 开启 | 是否分析构建、部署和配置类文件 |
| `--artifact-token-budget N` | `200000` | 加入依赖图的辅助文件总 token 上限 |
| `--artifact-exclude PATTERNS` | 无 | 排除辅助文件的路径模式，多个模式用逗号分隔 |
| `--with-prose` | 关闭 | 同时读取仓库根目录说明和 `docs/*.md` |

```bash
codewiki generate --no-artifacts
codewiki generate --artifact-exclude "docker/data/*,config/generated/*"
```

辅助文件类别和筛选规则见[构建与配置文件分析](artifact-aware-generation.md)。

### 模型调用和输出上限

下列参数只覆盖本次运行；不传时使用 `codewiki config set` 保存的值。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--max-tokens N` | `32768` | 单次模型响应的最大输出 token 数 |
| `--max-token-per-module N` | `36369` | 触发模块聚类的输入 token 阈值 |
| `--max-token-per-leaf-module N` | `16000` | 叶子模块的输入 token 上限 |
| `--max-depth N` | `2` | 模块拆分的最大层级 |
| `--request-limit N` | `100` | 每个 Agent 运行时允许的模型请求数 |
| `--agent-retries N` | `3` | Agent 工具调用失败后的重试次数 |
| `--concurrency N` | `2` | API 模式下并行请求数；`1` 表示串行。订阅模式仍为串行 |
| `--trace` / `--no-trace` | 开启 | 是否输出阶段和任务耗时 |
| `--prompt-caching` / `--no-prompt-caching` | 开启 | 是否使用提示词缓存；服务不支持时会自动退回普通请求 |

常用设置也可以保存到个人配置，不必每次重复输入：

```bash
codewiki config set --concurrency 3 --max-depth 3
codewiki generate --concurrency 1  # 仅本次运行使用串行请求
```

### 增量更新选项

一般只需使用 `--update`。其余选项用于调整变更识别和更新范围。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--update-rung RUNG` | `3` | `0` 为旧版按文件重建；`1` 到 `3` 为组件级更新的不同档位；`3b` 使用两层依赖范围 |
| `--tau-ren FLOAT` | `0.95` | 判断删除和新增组件是否属于重命名的相似度阈值 |
| `--tau-nb FLOAT` | `0.5` | 新组件根据依赖关系归入模块所需的邻居占比 |
| `--tau-grow FLOAT` | `0.33` | 触发父模块重新聚类的新增组件占比 |
| `--tau-full FLOAT` | `0.5` | 受影响模块占比达到此值时改为全量生成 |
| `--tau-tree FLOAT` | `0.3` | 模块结构变化占比达到此值时改为全量生成 |
| `--k-hop N` | `1` | 检查上游接口变更时沿依赖关系追踪的层数 |
| `--max-diff-tokens N` | `8000` | 单个组件变更内容的 token 上限 |
| `--no-ownership-closure` | 关闭 | 不为尚未归属任何模块的变更分配默认归属 |

更新过程和回退到全量生成的情况见[增量更新说明](incremental-updates.md)。

## 设置模型服务：`codewiki config set`

配置保存在当前用户的 `~/.codewiki/config.json`；只填写需要修改的参数即可。API 密钥优先保存在系统密钥库中，无法使用时才保存到 `~/.codewiki/credentials.json`。

| 参数 | 说明 |
| --- | --- |
| `--provider NAME` | 服务类型：`openai-compatible`、`atlas-cloud`、`anthropic`、`bedrock`、`azure-openai`、`claude-code`、`codex` |
| `--api-key KEY` | API 密钥；Claude Code 和 Codex 订阅模式不需要 |
| `--base-url URL` | API 地址；Atlas Cloud 会自动填写 |
| `--main-model NAME` | 编写模块文档使用的模型 |
| `--cluster-model NAME` | 组织模块使用的模型 |
| `--fallback-model NAME` | 主模型调用失败时使用的备用模型 |
| `--aws-region REGION` | AWS Bedrock 区域 |
| `--api-version VERSION` | Azure OpenAI API 版本 |
| `--azure-deployment NAME` | Azure OpenAI 部署名称 |
| `--max-tokens N`、`--max-token-per-module N`、`--max-token-per-leaf-module N`、`--max-depth N` | 保存对应的生成上限 |
| `--request-limit N`、`--agent-retries N` | 保存模型请求数和重试次数 |
| `--concurrency N` | API 模式下保存默认并行请求数 |
| `--trace` / `--no-trace` | 保存是否输出耗时 |
| `--use-gitignore` / `--no-gitignore` | 保存是否使用 `.gitignore` |
| `--prompt-caching` / `--no-prompt-caching` | 保存是否使用提示词缓存 |

示例：

```bash
codewiki config set --provider openai-compatible --api-key YOUR_API_KEY --base-url https://api.example.com/v1 --main-model YOUR_MODEL --cluster-model YOUR_MODEL
```

服务商专属设置见[模型服务设置](providers.md)。

## 设置个人默认项：`codewiki config agent`

这些设置只保存在当前用户配置中，不会和仓库一起提交。运行时传入的 `generate` 参数会覆盖相同字段。

```bash
codewiki config agent --exclude "tests,fixtures"
codewiki config agent --focus "src/orders,src/payments"
codewiki config agent --doc-type architecture
codewiki config agent --language zh
codewiki config agent             # 查看当前设置
codewiki config agent --clear     # 清除全部个人默认项
```

团队共享要求请写入仓库的 `.codewiki/prompts.md`，见[团队提示词教程](settings-and-prompts.md)。

## 查看和检查配置

```bash
codewiki config show          # 查看配置
codewiki config show --json   # 输出 JSON
codewiki config validate      # 检查配置并测试连接
codewiki config validate --quick  # 只检查配置，不测试连接
```

## 在 IDE 中使用：`codewiki mcp`

`codewiki mcp` 通过标准输入输出启动 MCP 服务。把下面配置添加到支持 MCP 的 IDE 中：

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

MCP 模式下，模型由 IDE 提供。工具和完整操作流程见 [MCP / IDE 使用方法](mcp-ide-mode.md)。
