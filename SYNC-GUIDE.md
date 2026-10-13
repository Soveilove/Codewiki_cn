# 仓库同步指南

本仓库是 CodeWiki 的个人维护分支，与上游仓库并行。任何人接手维护前，先读本文；**同步上游前，必须按第 3.4 节的检查清单逐项过**——本章内容来自实际合并中踩过的坑。

## 1. 远程仓库职责

| 远程 | 地址 | 职责 |
|---|---|---|
| `origin` | 个人 GitHub 仓库 | 个人维护主线。所有个人开发、提交、推送都在这里 |
| `upstream` | `https://github.com/FSoft-AI4Code/CodeWiki.git`（原作者） | 只读。只 `fetch`，**永不 push、永不直接在其上开发** |

本地可能还配置有其他远程（私人备份等），不参与本流程，也不要向其中推送公司代码。

## 2. 日常开发纪律

- **分支模型**：单 `master` 主线，不建长期功能分支，小改动直接提交。
- **提交身份**：本仓库 local 应固定个人 GitHub 身份。注意全局 `~/.gitconfig` 可能有 `includeIf` 规则覆盖身份——**新克隆或换机器后，第一件事是 `git config --local --get user.email` 确认是个人邮箱**。身份推错后清理需要重写历史，代价很高。
- **提交前必过**（与 CI 同标准）：
  - 全量测试：`pytest tests/ -q`，必须全绿（基线随新测试增长，约 683 passed / 3 skipped）
  - `ruff check` 与 `ruff format --check`
  - 仓库 `.githooks/pre-commit` 即此标准，建议 `git config core.hooksPath .githooks` 启用
- **行尾**：仓库有 `.gitattributes`（`* text=auto eol=lf`），库内一律 LF；不要用会全局改写行尾的工具，避免 CRLF 警告和跨平台 diff 污染。
- **虚拟环境**：`.venv` 由 uv 管理（无 pip 模块），装包用 `uv pip install --python .venv\Scripts\python.exe <spec>`。

## 3. 同步上游（merge upstream）标准流程

### 3.1 拉取与评估

```
git fetch upstream
git log --oneline master..upstream/main      # 上游新增提交
git diff --stat master...upstream/main       # 变更文件分布
```

**是否值得吸收：**

- 新语言分析器、解析修复：通常直接吸收，多为纯新增文件，与本地改动零冲突。
- 提示词模板（`prompt_template.py`）改动：谨慎。本地对证据组装、依赖预算预取、直写门控有深度改造；上游的增量段可以并入，但**不要**接受对整体结构的重排。
- 文档类（README、CHANGELOG、guides）：仓库已中文化。上游英文更新只取实质内容、译成中文，**不要把英文冲突块留在中文区**。
- 依赖版本钉扎变化：看清原因（可能是新 grammar 的 ABI 要求），本地 venv 必须对齐，见 3.4 第 2 步。

### 3.2 合并

```
git merge upstream/main -m "merge: 吸收 upstream <简述>"
```

保留合并历史，不要 rebase 或 squash 上游提交。

### 3.3 冲突解决模式

- `prompt_template.py`：保留本地“模块自有文件优先 + 依赖预算预取”结构；上游新增的语言专属提示段（如 `DART_FLUTTER_NOTE`）以增量方式并入 `_assemble`。若冲突两侧出现同一定义（如 `artifact_section`），保留本地位置、删除上游重复块——本地常把定义上移过。
- `guides/development.md` 等指南：保留中文流程，把上游新增的步骤细节译入中文。
- `CHANGELOG.md`：在中文对应分区补条目（吸收的功能写“新增”，行为修正写“修复”），删除英文冲突块。
- `ast_parser.py`、`analysis.py` 等被本地改过的文件：冲突解决后**逐行读一遍**，自动合并可能把两侧修改拼成病态代码。

### 3.4 合并后验证清单（必做，按顺序）

1. **先跑全量测试再提交。** 自动合并可能拼出语法错误——曾出现 `ast_parser.py` 合并后重复 `language=` 关键字参数导致 SyntaxError，只有测试能抓。测试全绿前不要 `git commit`。
2. **检查依赖版本钉扎并本地对齐。** 上游可能为兼容性收窄版本（例如 `tree-sitter-language-pack>=0.8.0,<0.9.0` 是 Dart grammar 的 ABI 要求）。本地 venv 版本不对齐的典型症状是：新语言测试 **assertion 失败**而非语法错误。对齐后重跑新语言测试。
3. **ruff check + format。** 合并会把两侧行拼在一起造成排版错位（曾影响 `analysis.py`、`call_graph_analyzer.py`），`ruff format` 修复后重跑相关测试。注意只格式化本次合并实际变更的文件，不要全仓 format 制造噪音 diff。
4. **抽查本地改造点仍在。** 例如 `prompt_template.py` 的 `deferred_source_ids` / `DIRECT_WRITE_NOTE`、`generation_runtime.py` 的 `shutil.copy2` 备份、`wiki_doctor.py`。用 `git diff upstream/main -- <文件>` 确认魔改没被上游版本覆盖。
5. 以个人 GitHub 身份提交，`git push origin master`。

### 3.5 出问题如何回退

- 合并后未推送：`git reset --hard <merge 前的提交>`，工作区改动先 `git stash`。
- 已推送：`git revert -m 1 <merge 提交>` 生成反向提交。**不要 force push 共享主线。**

## 4. 常见问题

- **换行符警告**（`LF will be replaced by CRLF`）：确认 `.gitattributes` 未被删、仓库级没有覆盖 `core.autocrlf`；本仓库一律 LF。
- **IDE 类型警告**：类型检查配置在 `pyproject.toml`（mypy 与 basedpyright 段）。本仓库不做完整标注，`reportUnknown*` 噪音族已关闭；不要为了消警告批量加 `Any` 或 `# type: ignore`。
- **提交身份错误**：见第 2 节，先查 local 身份再提交；已推送的错身份提交不要单独重写，统一在下轮同步时评估。
- **本地测试与 CI 结果不一致**：先核对依赖版本（第 3.4 节第 2 步）和 Python 版本（CI 用 3.12）。
