# Codex 独立验证清单：MCP + RAG 接入

目标仓库：GimMoliyadi/mini-agent
工作分支：feat/mcp-rag-integration-20261012
基线分支：feat/developer-workflows-20261011

本轮 GitHub 直接写入了功能代码、测试和文档，**未运行完整测试或真实模型实验**。
请先在隔离工作区检出该功能分支，在没有用户私人笔记/密钥的环境下独立测试。
除非用户明确授权，不要合并到 refactor/langgraph 或 main。

## 第一组：静态与安装

1. 使用 Python 3.11 的全新虚拟环境，安装：
   python -m pip install -c requirements.lock -e ".[dev,mcp]" wheel
2. 运行 python -m pytest -q，python -m ruff check .，python -m mypy。
3. 执行 python scripts/check.py、python scripts/check.py --packaging-only 和现有 Quick Start。
4. 如可选 MCP 依赖与 requirements.lock 版本冲突，找出最小兼容版本组并固定，
   不能通过删除依赖或跳过新测试来假装完成。

## 第二组：RAG adapter

1. 检查 mini_agent/rag_chain.py、Settings.load、AgentApplication.__enter__。
2. 构造本地 mock /api/search，覆盖成功、空结果、503、无效 JSON、
   过大返回、超时和断连；断言结构化工具错误不会被当作成功。
3. 确认 query/top_k 的 Pydantic schema 真实传入，URL 不暴露给模型。
4. 对照 rag-chain 的实际 API（GET /api/search）字段，对比引用来源是否准确。
5. 在无私人数据的临时 RAG 库上联调 GET /api/search，不得直接读取用户的 Vault。
6. 验证没有 RAG_CHAIN_URL 时不注册搜索工具且旧行为保持不变。

## 第三组：MCP adapter

1. 阅读官方 langchain-mcp-adapters 当前稳定版本 API，核查
   MultiServerMCPClient.get_tools(server_name=...)、args_schema、ToolMessage 的实际行为。
2. 使用临时本地 MCP Server 验证 stdio 工具发现、schema、同步 invoke、
   ToolMessage 错误传播、关闭和重启；再验证 Streamable HTTP（如环境允许）。
3. 检查 sync bridge 的后台 asyncio loop 启动竞态、超时取消、线程退出和资源清理。
4. 检查 readonly_tools allowlist 与默认 execute 风险：
   只读工具不触发审批；有副作用工具 ASK 触发 HITL；
   DENY 拒绝；Coding Contract 不允许无变更路径的 MCP 写工具。
5. 验证不同服务器的同名工具不会冲突、缺失工具失败、重复本地工具名失败。
6. 验证工具只看到明确 allowed_tools 中的条目，未列出的不得暴露。
7. 不要在测试中连接未知公网 MCP，不要保存或打印任何真实认证信息。

## 第四组：端到端/回归

1. 原有文件、命令、验收、审批、恢复、checkpoint 都要回归；
   不得为了通过本轮测试删掉旧测试。
2. 通过 mock 模型模拟 Agent 同时调用 knowledge search 和本地文件工具，
   核实工具结果返回后仍能完成 LangChain Agent Loop。
3. 检查异常和错误的状态转换，实际工具失败不能最终被误报为 successful。
4. MCP/RAG 均为 opt-in。默认关闭时旧 CI 和 Quick Start 不应受影响。
5. 用户授权且具备测试模型配额时，再运行一次受控端到端模型用例并记录调用次数、
   时间与成本；没授权就不要代替用户花费模型额度。

## 完成要求

- 对每一个真实失败写回归测试，再最小修改修复。
- 报告 CI Windows/Linux × Python 3.11/3.13 结果，不把 CI 绿色当作真实 MCP 服务验收。
- 输出修改提交、实际运行的命令、通过/失败/跳过数量以及尚未验证范围。
- 保持一个 Agent Loop（LangChain.create_agent）；不要自研 MCP 协议层。
- 不自动 merge PR，也不重写 rag-chain 的核心逻辑。
