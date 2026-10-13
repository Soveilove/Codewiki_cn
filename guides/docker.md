# 使用 Docker 运行 Web 应用

这份说明只适用于 CodeWiki Web 应用。命令行工具和 MCP 服务不需要 Docker，安装方法见[快速开始](../README.md#快速开始)。

## 准备配置

在仓库根目录执行：

```bash
cp docker/env.example .env
```

编辑 `.env`，至少填写模型名称、模型 API 地址和密钥：

```dotenv
MAIN_MODEL=YOUR_MODEL
FALLBACK_MODEL_1=YOUR_FALLBACK_MODEL
CLUSTER_MODEL=YOUR_MODEL
LLM_BASE_URL=https://api.example.com/v1
LLM_API_KEY=YOUR_API_KEY
APP_PORT=8000
```

把示例值替换成模型服务提供的信息。不要把含真实密钥的 `.env` 提交到 Git。

## 启动服务

Compose 配置引用外部网络 `codewiki-network`。首次启动前创建一次该网络，再启动服务：

```bash
docker network create codewiki-network
docker compose -f docker/docker-compose.yml up -d --build
```

浏览器打开 `http://localhost:8000`。如果修改了 `APP_PORT`，请使用对应端口。

Compose 将仓库的 `output/` 挂载到容器，生成结果和任务数据会保存在本地，不会因容器重建而消失。Web 应用通过 GitHub 仓库地址生成文档；这个入口与 `codewiki generate` CLI 的仓库本地 `.codewiki/` 输出设置相互独立。

## 查看运行状态

```bash
docker compose -f docker/docker-compose.yml logs -f
docker ps
```

停止容器但保留输出文件：

```bash
docker compose -f docker/docker-compose.yml down
```

## 常见问题

**端口已被占用**

把 `.env` 中的 `APP_PORT` 改成未使用的端口，再重新创建容器：

```bash
docker compose -f docker/docker-compose.yml down
docker compose -f docker/docker-compose.yml up -d
```

**容器无法连接模型服务**

检查 `LLM_BASE_URL` 和 `LLM_API_KEY`。如果模型服务运行在宿主机上，容器内的 `localhost` 指向容器本身；请使用容器可以访问的主机地址或同一 Docker 网络中的服务名。

**服务状态不正常**

查看日志定位启动错误：

```bash
docker compose -f docker/docker-compose.yml logs --tail=200
```

## 部署提示

生产环境不要把密钥写入镜像或提交到仓库。使用部署平台的密钥管理功能注入 `LLM_API_KEY`，并限制 `.env` 和 `output/` 的文件访问权限。需要改端口、网络或挂载目录时，以 `docker/docker-compose.yml` 中的实际配置为准。