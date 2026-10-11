# Codex 最终调试与验收交接（mini-agent v2）

## 项目和工作分支

- GitHub：`GimMoliyadi/mini-agent`
- 基线分支：`refactor/langgraph`（尚未合并 `main`）
- 本轮功能分支：`feat/developer-workflows-20261011`
- PR：<https://github.com/GimMoliyadi/mini-agent/pull/2>
- 目的：在成熟 LangChain / LangGraph coding agent 底座上扩展实用功能，**不重新实现模型—工具循环**。

## 请 Codex 执行（允许修复测试暴露的问题）

你是本 PR 的独立 QA、调试及回归验收工程师。请在独立工作区检出 `feat/developer-workflows-20261011`，完成以下事项。不要把未经检验的功能视为通过，也不要删除/削弱失败测试。

1. 静态审计：检查 `mini_agent/tools/command.py`、`mini_agent/tools/navigation.py`、`mini_agent/tools/registry.py`、`mini_agent/process_runner.py` 与新测试、`requirements.lock`。核对 LangChain StructuredTool schema、权限元数据和工作区绑定。
2. 安装并复现（建议 Python 3.11，新虚拟环境）：
   ```bash
   python -m pip install -c requirements.lock -e ".[dev]" wheel
   python scripts/check.py
   python scripts/check.py --packaging-only
   python scripts/verify_quickstart.py
   ```
   使用当前仓库 README 里提供的参数修正 Quick Start 入口，不能把未执行步骤写为通过。
3. 单独验证新能力：`find_files`、`inspect_python_symbols`、`python script.py`、`python -m ruff/mypy/compileall/build`、`git ls-files`，分别测试成功路径、无结果路径、错误文件、不合法模式、受限路径、软链接逃逸、嵌套函数、异常语法、截断与输出上限、Windows/Linux 差异。
4. 确认工具修改文件、命令调用、审批、Coding Contract 的工作流程没有退化：只读新工具无副作用；现有 `HumanInTheLoopMiddleware` 默认 `ASK` 仍要求写/执行审批；`Command(resume=...)` 不重复执行已完成文件修改；失败不会被当作成功。
5. 核对跨平台 CI：Windows + Linux × Python 3.11 / 3.13 四组状态。涉及 Windows 专用 ctypes 类型注解的修复不能破坏 Windows 进程树清理。
6. 对发现的真实 bug 编写先失败后通过的回归测试；在本分支直接修复，确保新旧测试全通过。不要通过跳过/删除既有测试来制造通过结果。
7. 如果具备经用户授权的有效模型配置，另在临时、无隐私文件的工作区运行真实多步 Coding Agent 任务，并记录完整结果；**没有该配置则明确标注未执行，不索要或输出 Key**。
8. 在 PR 中报告最终提交、每条验证命令的结果、失败原因与修复、仍未完成的限制。不要自动将此 PR 或基线 v2 合并到 `main`。

## 本轮功能的验收判断

- Agent 可以递归按模式定位代码文件，返回相对路径和匹配数量；
- 可以静态解析 Python 文件的类、函数、方法及精确行号，不执行目标代码；
- 可以在受信工作区中运行脚本、常见静态检查/测试/构建命令；
- 不允许 `python -c`、`pip install` 或未授权的目录外执行；
- 两组新 LangChain StructuredTool 的工作区边界生效；
- 跨平台 CI 全部通过才建议把 PR 从草稿改为待审核；
- 单次真实模型烟雾测试不能替代多任务成功率评测。

最终产出：**修复后的分支与 PR、可追溯测试报告、明确的剩余问题和下一轮优先级**。