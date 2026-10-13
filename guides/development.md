# 开发指南

本文介绍如何在本地运行、测试和修改 CodeWiki。配置参数和团队提示词的用法见[参数设置与团队提示词](settings-and-prompts.md)。

## 本地安装

需要 Python 3.12+ 和 Git。运行时不需要 Node.js 或 npm。

```bash
git clone https://github.com/FSoft-AI4Code/CodeWiki.git
cd CodeWiki
python3.12 -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
codewiki --version
```

Mermaid 图不经过独立语法校验，由生成模型在写同一步内自查；代码生成过程不启动任何 JavaScript 运行时，也不请求远程渲染服务。

## 目录结构

```text
codewiki/
├── cli/                         # 命令行入口、配置、参数和适配器
├── mcp/                         # IDE 使用的 MCP 服务和工具
├── src/config.py                # 后端配置
├── src/be/
│   ├── dependency_analyzer/     # 代码、依赖关系和构建文件分析
│   ├── updater/                 # 组件级增量更新
│   ├── agent_tools/             # Agent 读取代码和写入文档的工具
│   ├── documentation_generator.py
│   ├── backend.py               # 模型后端接口
│   ├── pydantic_ai_backend.py   # API 服务
│   └── caw_backend.py           # Claude Code / Codex 订阅
├── src/fe/                      # Web 应用
└── run_web_app.py               # Web 服务入口

guides/                          # 手写使用和开发指南
adr/                             # 架构决策记录
skills/                          # IDE 使用的 CodeWiki Skill
tests/                           # 自动化测试
docker/                          # Web 应用容器配置
```

## 生成流程

1. **分析仓库**：Tree-sitter 解析源码，将类、函数等组件建成依赖图节点；辅助分析器加入构建、CI、容器、配置等节点及其代码关系。
2. **选择并聚类模块**：根据组件依赖选择叶子节点，由模型将组件聚成模块，并按 `max_depth` 递归拆分。
3. **编写页面**：Agent 通过工具读取源码，先写叶子模块，再根据子页面写父模块，最后生成总览；`metadata.json` 保存模型、版本和提交信息。
4. **增量更新**：传入 `--update` 后，对比旧依赖图和当前代码，修复模块结构、生成变更报告，再更新受影响的页面。每次决定写入 `update_record.json`。

CLI、MCP 和 Web 应用使用不同的入口和输出管理。改输出位置时，请分别核对各入口的默认路径，不要假设它们共享同一配置。

## 模型后端

`LLMBackend` 定义模型调用接口。`PydanticAIBackend` 负责 API 密钥服务；`CawBackend` 通过本机 `claude` 或 `codex` 命令行使用订阅，并通过 MCP 暴露 CodeWiki 工具。

## 添加生成参数

要新增用户可配置的参数，按数据流逐处接线：

1. 在 `cli/models/config.py` 增加字段及序列化、反序列化逻辑。
2. 在 `cli/commands/generate.py` 增加单次运行参数；需要保存为默认值时，也在 `cli/commands/config.py` 增加设置入口。
3. 通过 `cli/adapters/doc_generator.py` 把值传给后端 `src/config.py`。
4. 在真正消费该值的位置使用它，例如文件筛选器或提示词模板。
5. 为默认值、命令行覆盖、持久化读写和实际行为补测试。

仓库团队约定写在 `.codewiki/prompts.md`。受支持字段和优先级见[团队提示词教程](settings-and-prompts.md)。修改时要注意：团队文件只覆盖它实际写出的提示字段，不应覆盖并行度等机器运行参数。

## 添加一种语言

每种语言有独立 Tree-sitter 分析器。可参考 `codewiki/src/be/dependency_analyzer/analyzers/scala.py`，并同步更新分发、扩展名列表和测试。

1. 在 `pyproject.toml` 和 `requirements.txt` 添加 Tree-sitter 语法包。
2. 创建 `analyzers/<lang>.py`，实现组件和关系提取：一个 `TreeSitter<Lang>Analyzer` 类加一个模块级 `analyze_<lang>_file(...)` 函数。
3. 在 `analysis/call_graph_analyzer.py` 增加该语言的分析分支。
4. 在 `analysis/analysis_service.py`、`utils/patterns.py`、`ast_parser.py`、`cli/utils/validation.py` 和 `cli/utils/repo_validator.py` 注册语言及扩展名。
5. 新增 `tests/test_<lang>_analyzer.py`，用小型源码样例验证组件和依赖边。可参考 `tests/test_scala_analyzer.py`、`tests/test_ruby_analyzer.py`、`tests/test_rust_analyzer.py` 和 `tests/test_dart_analyzer.py`。
6. 在 README 的语言列表中添加该语言。
6. 在 README 支持语言列表中补上该语言。

## 测试与代码检查

```bash
pytest -q -o addopts=""                  # 运行全部测试
pytest tests/test_updater_orchestrator.py -q
pytest --cov=codewiki tests/               # 带覆盖率
ruff check codewiki tests
ruff format --check codewiki tests
```

CI 使用 Python 3.12 运行测试、Ruff 检查和格式检查。提交前可启用本仓库的 Git Hook：

```bash
git config core.hooksPath .githooks
```

Hook 会检查已暂存的 Python 文件；本地修复后重新暂存再提交。CI 仍会运行同一组检查。

## 发布

1. 同步更新 `codewiki/__init__.py` 和 `pyproject.toml` 中的版本号。
2. 在 `CHANGELOG.md` 的 `[Unreleased]` 下记录变更。
3. 创建 `vX.Y.Z` 标签和 GitHub Release，并附上对应变更记录。

## 排查问题

| 现象 | 检查方式 |
| --- | --- |
| Tree-sitter 解析失败 | 检查文件是否为 UTF-8，并确认对应语法包已安装 |
| 模型服务连接失败 | 运行 `codewiki config validate`；订阅模式还要确认 `claude` 或 `codex` 已登录且在 `PATH` 中 |
| 单个模块提示过长 | 降低 `--max-token-per-module` 或 `--max-token-per-leaf-module`，或使用 `--include`、`--focus` 缩小范围 |
| 增量更新变成全量生成 | 查看输出目录的 `update_record.json` 中的回退原因，检查 `tau_full` 或 `tau_tree` |

## 贡献代码

Fork 仓库并创建分支，修改代码时补充或更新测试，然后运行测试和 Ruff 检查。提交 Pull Request 时写清改了什么以及原因。问题和建议可提交到 [GitHub Issues](https://github.com/FSoft-AI4Code/CodeWiki/issues)。