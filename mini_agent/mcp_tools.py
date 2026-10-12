"""Opt-in MCP tool discovery for the synchronous mini-agent CLI.

We reuse langchain-mcp-adapters for protocol transport and schema conversion.
Its tools are async-only; a dedicated event loop allows the existing synchronous
LangGraph application to invoke them without copying the MCP protocol.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import json
from pathlib import Path
import re
from threading import Event, Thread
from uuid import uuid4
from urllib.parse import urlsplit

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from .sandbox import redact_text

_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,39}$")
MAX_CONFIG_BYTES = 64 * 1024
MAX_MCP_RESULT_CHARS = 4_000


def read_mcp_configuration(path: Path) -> tuple[dict, dict[str, set[str]], dict[str, set[str]]]:
    """Validated, explicit allowlist. An absent config means no MCP tools."""
    source = Path(path).expanduser()
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("MCP 配置必须是现有、非链接且不超过 64KiB 的 JSON 文件")
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("servers"), dict) or not data["servers"]:
        raise ValueError("MCP 配置需要非空 servers 对象")
    servers = data["servers"]
    if len(servers) > 8:
        raise ValueError("最多允许 8 个 MCP 服务器")
    for server, options in servers.items():
        if not isinstance(server, str) or not _NAME.fullmatch(server):
            raise ValueError("MCP 服务器名称只能使用短字母数字标识")
        if not isinstance(options, dict) or options.get("transport") not in {"stdio", "http"}:
            raise ValueError("MCP 服务器只支持 stdio 和 streamable HTTP")
        if options["transport"] == "stdio":
            if (not isinstance(options.get("command"), str) or not options["command"]
                    or not isinstance(options.get("args", []), list)
                    or any(not isinstance(arg, str) for arg in options.get("args", []))):
                raise ValueError("stdio MCP 服务器需要 command 和字符串参数数组")
        else:
            url = options.get("url")
            if not isinstance(url, str):
                raise ValueError("HTTP MCP 服务器需要 URL")
            parsed = urlsplit(url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.fragment):
                raise ValueError("MCP HTTP URL 不允许凭据和 fragment")
            if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("外部 MCP HTTP 服务必须使用 HTTPS")
    allowed = data.get("allowed_tools", {})
    readonly = data.get("readonly_tools", {})
    for group in (allowed, readonly):
        if not isinstance(group, dict) or any(
            name not in servers or not isinstance(items, list)
            or any(not isinstance(item, str) or not item for item in items)
            for name, items in group.items()
        ):
            raise ValueError("MCP 工具列表必须按已配置的服务器给出字符串数组")
    if set(readonly) - set(allowed):
        raise ValueError("readonly_tools 只能引用已配置 allowed_tools 的服务器")
    permissions = {name: set(allowed.get(name, [])) for name in servers}
    read_only = {name: set(readonly.get(name, [])) for name in servers}
    for name in servers:
        if not read_only[name].issubset(permissions[name]):
            raise ValueError("readonly_tools 必须属于 allowed_tools")
    if not any(permissions.values()):
        raise ValueError("MCP 必须显式配置 allowed_tools；禁止自动暴露所有远端工具")
    return servers, permissions, read_only


class MCPToolBridge:
    """Session-scoped, bounded sync bridge to official async LangChain adapters."""

    def __init__(self, path: Path, *, timeout: float = 45.0):
        if timeout <= 0:
            raise ValueError("MCP timeout 必须为正数")
        self.path = Path(path)
        self.timeout = timeout
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: Thread | None = None
        self.started = Event()
        self.tools: list[BaseTool] = []

    def _serve(self) -> None:
        assert self.loop is not None
        asyncio.set_event_loop(self.loop)
        self.started.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()
            asyncio.set_event_loop(None)

    def _submit(self, coroutine):
        if self.loop is None or not self.loop.is_running():
            coroutine.close()
            raise RuntimeError("MCP event loop 未启动")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=self.timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise TimeoutError("MCP 工具调用超时") from exc

    async def _discover(self, servers: dict, allowed: dict, readonly: dict):
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient(servers)
        registered: list[BaseTool] = []
        for server in servers:
            if not allowed[server]:
                continue
            upstream_tools = await client.get_tools(server_name=server)
            discovered = {tool.name: tool for tool in upstream_tools}
            missing = allowed[server] - set(discovered)
            if missing:
                raise ValueError(f"MCP 服务器 {server} 缺少指定工具：{', '.join(sorted(missing))}")
            for name in sorted(allowed[server]):
                target = discovered[name]
                exposed_name = f"mcp_{server}_{name}"
                if len(exposed_name) > 64 or not re.fullmatch(r"[a-zA-Z0-9_-]+", exposed_name):
                    raise ValueError(f"模型工具名称无效：{exposed_name}")
                is_read_only = name in readonly[server]

                # Factory needed: the model receives original args_schema, not
                # the injected upstream tool, event loop or transport options.
                def handler_factory(remote_tool, remote_server, remote_name):
                    def call(**kwargs):
                        try:
                            value = self._submit(remote_tool.ainvoke({
                                "type": "tool_call", "name": remote_tool.name,
                                "args": kwargs, "id": uuid4().hex,
                            }))
                            failed = isinstance(value, ToolMessage) and value.status == "error"
                            body = value.content if isinstance(value, ToolMessage) else value
                            msg = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
                            msg = redact_text(msg)
                            if len(msg) > MAX_MCP_RESULT_CHARS:
                                msg = msg[:MAX_MCP_RESULT_CHARS] + "\n[MCP 工具结果已截断]"
                            return msg, {
                                "status": "tool_error" if failed else "success",
                                "code": "mcp_tool_error" if failed else "ok",
                                "metadata": {"server": remote_server, "remote_tool": remote_name},
                            }
                        except Exception as exc:
                            return "MCP 工具调用失败：" + type(exc).__name__, {
                                "status": "tool_error", "code": "mcp_call_failed", "metadata": {},
                            }
                    return call

                registered.append(StructuredTool.from_function(
                    func=handler_factory(target, server, name),
                    name=exposed_name,
                    description=f"[MCP {server}] {target.description or name}",
                    args_schema=target.args_schema,
                    response_format="content_and_artifact",
                    metadata={
                        "risk": "read" if is_read_only else "execute",
                        "mutation": not is_read_only,
                        "workspace_arguments": (),
                    },
                ))
        return registered

    def __enter__(self) -> "MCPToolBridge":
        try:
            servers, allowed, readonly = read_mcp_configuration(self.path)
            self.loop = asyncio.new_event_loop()
            self.thread = Thread(target=self._serve, name="mini-agent-mcp", daemon=True)
            self.thread.start()
            if not self.started.wait(5):
                raise TimeoutError("MCP event loop 无法启动")
            self.tools = self._submit(self._discover(servers, allowed, readonly))
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.tools = []
