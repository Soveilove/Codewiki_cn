# 模型服务设置

CodeWiki 使用模型完成模块聚类和文档编写。通过 `codewiki config set --provider ...` 选择服务。

| 服务类型 | 需要准备 | 适用情况 |
| --- | --- | --- |
| `openai-compatible`（默认） | API 密钥和服务地址 | OpenAI 兼容接口，包括 OpenAI、LiteLLM、vLLM、OpenRouter 等 |
| `atlas-cloud` | API 密钥 | 通过 Atlas Cloud 使用托管模型 |
| `anthropic` | API 密钥 | 直接连接 Anthropic API |
| `azure-openai` | API 密钥、资源地址和部署名称 | 使用 Azure OpenAI |
| `bedrock` | AWS 凭据和区域 | 使用 AWS Bedrock 上的模型 |
| `claude-code` | 已登录的 Claude Code 命令行 | 使用 Claude 订阅，不需要单独的 API 密钥 |
| `codex` | 已登录的 Codex 命令行 | 使用 Codex 订阅，不需要单独的 API 密钥 |

`config set` 只修改本次提供的字段。切换服务类型时，请同时重新填写 `--main-model` 和 `--cluster-model`，避免保留旧服务的模型名。

## 使用 API 密钥的服务

OpenAI 兼容服务的配置示例：

```bash
codewiki config set \
  --provider openai-compatible \
  --api-key YOUR_API_KEY \
  --base-url https://api.example.com/v1 \
  --main-model YOUR_MODEL \
  --cluster-model YOUR_MODEL \
  --fallback-model FALLBACK_MODEL
```

Anthropic API 示例：

```bash
codewiki config set \
  --provider anthropic \
  --api-key YOUR_API_KEY \
  --base-url https://api.anthropic.com \
  --main-model claude-sonnet-4 \
  --cluster-model claude-sonnet-4
```

Azure OpenAI 还需要资源地址和部署名称：

```bash
codewiki config set \
  --provider azure-openai \
  --api-key YOUR_AZURE_KEY \
  --base-url https://YOUR_RESOURCE.openai.azure.com \
  --azure-deployment YOUR_DEPLOYMENT \
  --main-model YOUR_MODEL \
  --cluster-model YOUR_MODEL
```

AWS Bedrock 使用本机已有的 AWS 凭据：

```bash
codewiki config set \
  --provider bedrock \
  --aws-region us-east-1 \
  --main-model YOUR_BEDROCK_MODEL_ID \
  --cluster-model YOUR_BEDROCK_MODEL_ID
```

## Atlas Cloud

Atlas Cloud 的 API 地址由 CodeWiki 自动设置。如果没有传入 `--api-key`，程序会读取环境变量 `ATLASCLOUD_API_KEY`。

```bash
codewiki config set \
  --provider atlas-cloud \
  --main-model YOUR_ATLAS_MODEL_ID \
  --cluster-model YOUR_ATLAS_MODEL_ID \
  --fallback-model YOUR_FALLBACK_MODEL_ID
```

可在 [Atlas Cloud 模型列表](https://api.atlascloud.ai/v1/models)查询模型 ID。

## 使用订阅模式

订阅模式通过本机的 `claude` 或 `codex` 命令行发送模型请求。先安装对应命令行并完成登录，再配置 CodeWiki。

```bash
claude login
codewiki config set \
  --provider claude-code \
  --main-model claude-sonnet-4-6 \
  --cluster-model claude-sonnet-4-6
```

Codex 示例：

```bash
codex login
codewiki config set \
  --provider codex \
  --main-model YOUR_CODEX_MODEL \
  --cluster-model YOUR_CODEX_MODEL
```

订阅模式的模型名称要使用命令行支持的原始名称，不要加 `openai/` 或 `anthropic/` 前缀。若之前使用过 API 服务，切换后重新填写两个模型名。

订阅模式依赖的组件使用了 macOS / Linux 专有的系统接口，**暂时不支持 Windows**。Windows 上请使用 API 服务（如 `openai-compatible`），配置方法见上文；订阅模式的相关验证也只在 macOS 上进行。

## 模型怎么选

主模型负责编写文档，需要能读懂代码并输出较长内容。聚类模型负责把组件分组，通常与主模型使用相同模型即可。备用模型在主模型请求失败时使用，可选择价格较低且能处理较长输入的模型。

完成配置后检查连接：

```bash
codewiki config show
codewiki config validate
```

更多参数见[命令与参数参考](cli-reference.md)。