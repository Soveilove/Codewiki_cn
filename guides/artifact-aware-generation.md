# 构建与配置文件分析

CodeWiki 默认分析构建、部署和配置文件，并把它们与相关源码一起组织进依赖图。需要时可以限制文件范围或关闭这类分析。

## 会分析哪些文件

CodeWiki 按文件名、扩展名和目录识别以下类别：

| 类别 | 示例 |
| --- | --- |
| `manifest`（项目清单） | `package.json`、`pyproject.toml`、`setup.py`、`Cargo.toml`、`go.mod`、`Gemfile` |
| `build`（构建） | `Makefile`、`CMakeLists.txt`、`*.gradle`、`BUILD.gn`、`Rakefile` |
| `container`（容器） | `Dockerfile*`、`Containerfile`、`docker-compose*.yml`、`docker/` 下的文件 |
| `ci`（持续集成） | `.github/workflows/*.yml`、`.gitlab-ci.yml`、`Jenkinsfile`、`.circleci/` |
| `packaging`（打包） | `debian/`、`rpm/`、`*.spec`、`*.service`、安装和生命周期脚本 |
| `test_infra`（测试基础设施） | `pytest.ini`、`conftest.py`、`tox.ini`、`vitest.config.*`、`playwright.config.*` |
| `schema`（数据结构） | `*.proto`、`*.fbs`、`openapi.*`、`*.schema.json` |
| `config`（配置） | `config/`、`etc/`、`settings/` 下的文件，以及根目录中的 TOML、YAML、INI 等文件 |
| `script`（脚本） | 根目录的 `*.sh`，以及 `bin/`、`scripts/`、`tools/`、`hack/` 下的文件 |

锁文件、二进制文件、`tests/` 和 `fixtures/` 下的内容、供应商代码、Issue / PR 模板及普通源码不会作为辅助文件处理。像 `setup.py`、`conftest.py` 这样的已知构建或测试文件除外。

README 和 `docs/` 默认不参与分析。需要把人工编写的说明也交给模型参考时，使用 `--with-prose`；此时会读取仓库根目录的 README、CONTRIBUTING 和 `docs/*.md`。

## 它们如何参与文档生成

每个辅助文件会成为依赖图节点；对于 Makefile 目标、CI 任务、Docker 阶段和项目脚本等可识别内容，还会提取子节点。CodeWiki 会尝试把这些文件与其引用的代码关联起来，再交给聚类和文档生成流程。

如果聚类结果遗漏较多，CodeWiki 会补入 **Build, Deployment and Configuration** 模块，收纳未分配的辅助文件。辅助文件索引保存在 `<输出目录>/temp/artifact_index.json`。

## 文件数量与输入上限

| 限制 | 默认值 |
| --- | --- |
| 单个文件读取长度 | 16 KB，超出部分截断 |
| 每个类别的文件数 | 40 |
| 辅助文件总 token 上限 | 200,000，可用 `--artifact-token-budget` 修改 |

达到总量上限时，优先保留项目清单、构建、容器、CI、打包、测试基础设施、Schema、配置、脚本，最后才是 prose。

## 控制分析范围

```bash
# 默认开启，也可以明确关闭
codewiki generate --artifacts
codewiki generate --no-artifacts

# 调低辅助文件总输入上限
codewiki generate --artifact-token-budget 100000

# 把仓库说明也交给模型
codewiki generate --with-prose

# 跳过生成出来的大型配置目录
codewiki generate --artifact-exclude "docker/data/*,config/generated/*"
```

`--artifact-exclude` 的模式匹配仓库相对路径或文件名；单独填写目录名时会跳过该目录。

## 测试结果

在 [CodeWikiBench](https://github.com/FSoft-AI4Code/CodeWikiBench) 的七个仓库中，辅助文件使依赖图节点数增加了 0.4%–4.7%。构建主题得分从 1.x 的 28.2 提升到 2.0 的 71.9。完整结果见[基准测试](../README.md#基准测试)。