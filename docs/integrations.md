# 外部知识库与 MCP 工具接入

本轮增加可选的外部工具适配，未配置时继续使用原有 LangGraph Agent。
没有开发第二套 Agent Loop，也不合并两个仓库的数据库。

## 1. 已有 rag-chain 的 HTTP 接入

RAG 项目： https://github.com/GimMoliyadi/rag-chain

在 rag-chain 自己的虚拟环境中运行：

    uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

在 mini-agent 的私有状态 .env 中设置：

    RAG_CHAIN_URL=http://127.0.0.1:8000
    RAG_CHAIN_TIMEOUT=20

Agent 会增加只读工具 search_knowledge(query, top_k=5)，直接调用已有 GET /api/search。
保留原文片段、文档标题、文件路径、章节及相似度，**不触发 RAG 内部的问答 LLM**。
未命中时返回无证据提示；检索失败保留结构化错误码。
不会执行 ingest，也不会写入 Obsidian Vault、Chroma 或 SQLite。
检索请求限制长度、结果条数、响应大小和工具输出长度。
服务地址只从用户配置加载，不能由模型调用时改写；本机支持 HTTP，远端要求 HTTPS。
无需额外 MCP 依赖。本轮用独立测试数据验收 HTTP/Chroma 链路；
私人 Obsidian 索引的检索质量不属于本轮测试范围。

## 2. 通用 MCP Client

单独安装可选依赖：

    python -m pip install -c requirements.lock -e ".[mcp,dev]"

在 .env 指定私有配置路径：

    MINI_AGENT_MCP_CONFIG=C:/your-private-state/mcp.json
    MINI_AGENT_MCP_TIMEOUT=45

MCP 配置文件示例（替换 Python 和脚本路径，不要把带有真实密钥的配置上传到 GitHub）：

~~~json
{
  "servers": {
    "notes": {
      "transport": "stdio",
      "command": "C:/path/to/rag-chain/.venv/Scripts/python.exe",
      "args": ["C:/path/to/rag-chain/mcp_server.py"]
    }
  },
  "allowed_tools": {
    "notes": ["search", "ask"]
  },
  "readonly_tools": {
    "notes": ["search", "ask"]
  }
}
~~~

支持多个服务器（最多 8 个），可采用 stdio 或 Streamable HTTP，HTTP transport
使用 {"transport": "http", "url": "https://.../mcp"} 形式。
显式 allowed_tools 是必需的，模型只会看到允许列表内的工具。
模型端工具名使用 mcp_<server>_<tool>，避免多服务器重名。

使用官方 langchain-mcp-adapters 的 MultiServerMCPClient 执行 MCP 发现及调用；
MCP 工具为异步，现有 CLI 是同步，因此通过每个应用生命周期的后台事件循环
调用 async 工具，不重新实现 MCP 协议层。首次启动需要进行工具发现，
缺失 server、工具不存在或连接失败会显式报错。

readonly_tools 只为用户明确确认只读的工具配置，必须是 allowed_tools 子集。
其余 MCP 工具默认 execute 级风险，ASK 下需审批；由于没有声明本地修改路径，
Coding Contract 默认拒绝此类外部副作用工具。配置只读不等于能够审计远端代码，
只能连接受信任的服务器。

同一 RAG 系统建议选择 HTTP 搜索或 MCP 两种接入中的一种，避免注册重复功能。
RAG HTTP 搜索更轻量，MCP 更适合演示标准 MCP 工具接入。

## 3. 使用边界

- 外部检索内容和 MCP 返回值都是资料，不能当作用户授权。
- 私人 Obsidian 片段可能进入配置的模型 Provider，上线前确认隐私边界。
- 内建本地工具的 workspace 限制不会自动约束第三方 MCP Server 的权限。
- 未实现异步 Web UI，也未承诺外部 MCP 写入操作的 exactly-once 语义。
- MCP 连接配置和会话状态应保存在私有状态目录，严禁上传凭据到仓库。

## 4. 验证状态

2026-10-12 已执行独立工程验收。真实 MCP SDK 服务覆盖 stdio 和 Streamable HTTP；
RAG 使用独立测试数据验证真实 TCP HTTP、上游应用契约和来源传递；
真实模型完成仓库分析、代码修复、知识辅助开发、跨应用恢复四项任务。
详细范围和数据见 [验收报告](evaluation/MCP_RAG_QA_20261012.md)。

默认 MCP 每次工具调用建立独立会话。需要远端跨调用状态的服务，应在后续版本
明确设计和验证持久会话；当前不能用事件循环生命周期代替远端会话状态保证。

## 5. 可复现演示

先安装开发和 MCP 依赖，在一个终端启动独立、无私人内容的 RAG 测试库：

```powershell
python tests/fixtures/rag_http_server.py --port 8765 --db .state/rag-fixture.sqlite3
```

这套服务使用 SQLite FTS5/BM25 检索两个 synthetic 文档片段，HTTP 契约与
rag-chain 一致。`stable_unique` 的规则是过滤 None、去重并保留首次出现顺序。
它是可重复的适配器演示服务，不代表真实嵌入模型的检索质量。

另一终端执行真实模型验收（会使用所配置 Provider 的额度）：

```powershell
python examples/live_integration_acceptance.py --config-dir C:/private/model-state --rag-url http://127.0.0.1:8765 --output .state/live-integrations.json
```

`--config-dir` 指向已有模型 `.env` 的私有目录。Agent 只处理临时创建的工作区，
不向报告写入密钥；MCP SDK 子进程由该示例自动启动、发现和关闭。
使用 `--only rag-mcp-local-development` 可单独验证联合开发场景。
报告包含真实模型/工具调用数、Provider token 用量、测试与验收结果、引用来源。
未提供价格表时不估算金额，缺失 usage 会单独记录。

无需模型的协议回归：

```powershell
python -m pytest -q tests/test_mcp_bridge.py tests/test_mcp_integration.py tests/test_rag_chain_adapter.py tests/test_rag_chain_integration.py
python scripts/check.py
python scripts/check.py --packaging-only
```

测试服务只读取和写入自建临时数据；不连接私人 Vault。上游真实应用的独立索引
验证方式与嵌入替身范围见验收报告。
