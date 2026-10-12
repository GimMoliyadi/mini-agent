"""Offline MCP tests: fake async adapter, real sync bridge and tool contracts."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from mini_agent.mcp_tools import MCPToolBridge, read_mcp_configuration


class AddArgs(BaseModel):
    a: int
    b: int


async def remote_add(a: int, b: int) -> str:
    return str(a + b)


def mocked_adapter():
    pkg = ModuleType("langchain_mcp_adapters")
    pkg.__path__ = []
    client_module = ModuleType("langchain_mcp_adapters.client")
    remote = StructuredTool.from_function(
        name="add", description="add two integers",
        coroutine=remote_add, args_schema=AddArgs,
    )

    class FakeClient:
        def __init__(self, servers):
            self.servers = servers

        async def get_tools(self, *, server_name=None):
            return [remote] if server_name == "math" else []

    client_module.MultiServerMCPClient = FakeClient
    return {"langchain_mcp_adapters": pkg, "langchain_mcp_adapters.client": client_module}


class MCPBridgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "mcp.json"

    def write_config(self, readonly=True):
        config = {
            "servers": {
                "math": {
                    "transport": "stdio",
                    "command": "python",
                    "args": ["math_server.py"],
                },
            },
            "allowed_tools": {"math": ["add"]},
        }
        if readonly:
            config["readonly_tools"] = {"math": ["add"]}
        self.path.write_text(json.dumps(config), encoding="utf-8")

    def test_explicit_allowlist_and_readonly_subset(self):
        self.write_config()
        servers, allowed, readonly = read_mcp_configuration(self.path)
        self.assertIn("math", servers)
        self.assertEqual(allowed["math"], {"add"})
        self.assertEqual(readonly["math"], {"add"})
        bad = json.loads(self.path.read_text(encoding="utf-8"))
        bad["allowed_tools"] = {"math": []}
        self.path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_mcp_configuration(self.path)
        bad["allowed_tools"] = {"math": ["add"]}
        bad["readonly_tools"] = {"math": ["delete"]}
        self.path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_mcp_configuration(self.path)

    def test_unknown_server_and_insecure_http_are_rejected(self):
        self.write_config()
        bad = json.loads(self.path.read_text(encoding="utf-8"))
        bad["servers"]["math"] = {
            "transport": "http", "url": "http://external.example.com/mcp",
        }
        self.path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_mcp_configuration(self.path)
        bad["servers"]["math"]["url"] = "http://127.0.0.1:9090/mcp"
        bad["allowed_tools"] = {"other": ["add"]}
        self.path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_mcp_configuration(self.path)

    def test_async_mcp_tool_runs_in_current_sync_cli_contract(self):
        self.write_config(readonly=True)
        with patch.dict(sys.modules, mocked_adapter()):
            with MCPToolBridge(self.path, timeout=5) as bridge:
                self.assertEqual(len(bridge.tools), 1)
                tool = bridge.tools[0]
                self.assertEqual(tool.name, "mcp_math_add")
                self.assertEqual(tool.metadata["risk"], "read")
                self.assertFalse(tool.metadata["mutation"])
                self.assertEqual(set(tool.args), {"a", "b"})
                response = tool.invoke({
                    "type": "tool_call", "name": tool.name,
                    "args": {"a": 2, "b": 3}, "id": "mcp-call-1",
                })
                self.assertIsInstance(response, ToolMessage)
                self.assertEqual(response.content, "5")
                self.assertEqual(response.artifact["status"], "success")
        self.assertFalse(bridge.thread.is_alive())

    def test_external_tool_default_requires_approval(self):
        self.write_config(readonly=False)
        with patch.dict(sys.modules, mocked_adapter()):
            with MCPToolBridge(self.path, timeout=5) as bridge:
                tool = bridge.tools[0]
                self.assertEqual(tool.metadata["risk"], "execute")
                self.assertTrue(tool.metadata["mutation"])

    def test_missing_remote_tool_fails_explicitly(self):
        self.write_config()
        config = json.loads(self.path.read_text(encoding="utf-8"))
        config["allowed_tools"]["math"] = ["missing"]
        config["readonly_tools"] = {"math": []}
        self.path.write_text(json.dumps(config), encoding="utf-8")
        with patch.dict(sys.modules, mocked_adapter()):
            with self.assertRaisesRegex(ValueError, "缺少指定工具"):
                with MCPToolBridge(self.path, timeout=5):
                    pass


if __name__ == "__main__":
    unittest.main()
