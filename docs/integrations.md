# 外部知识库与 MCP 工具接入（待 Codex 最终验收）

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
无需额外 MCP 依赖。真实 Obsidian 索引的连接与效果留待 Codex 验证。

## 2. 通用 MCP Client

单独安装可选依赖：

    python -m pip install -e ".[mcp,dev]"

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

代码、配置接线和离线测试已提交到功能分支，但尚未执行完整测试和真实模型验收。
请使用 CODEX_MCP_RAG_QA.md 进行独立验证，发现问题修复后才能称为测试通过。
