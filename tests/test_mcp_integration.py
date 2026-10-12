"""Real MCP SDK transports and LangGraph policies, with no external account."""

from contextlib import contextmanager, nullcontext
from dataclasses import replace
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("langchain_mcp_adapters", reason="Install .[mcp] for real protocol tests")

from mini_agent.mcp_tools import MCPToolBridge
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings
from mini_agent.verifier import CodingTaskContract, TestCommand as RequiredCommand
from helpers import ScriptedModel, call

SERVER = Path(__file__).parent / "fixtures" / "mcp_server.py"


def write_config(tmp_path, servers=None, readonly=True):
    names = ["add", "fail", "slow"]
    config = {
        "servers": servers or {"math": {"transport": "stdio", "command": sys.executable,
                                         "args": [str(SERVER)]}},
        "allowed_tools": {"math": names},
        "readonly_tools": {"math": names if readonly else []},
    }
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def invoke(tool, **args):
    return tool.invoke({"type": "tool_call", "name": tool.name, "args": args, "id": "acceptance"})


@contextmanager
def http_server():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen([sys.executable, str(SERVER), "streamable-http", str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("MCP HTTP server exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("MCP HTTP server did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_real_discovery_schema_result_error_and_restart(tmp_path, monkeypatch, transport):
    monkeypatch.setenv("NO_PROXY", "*")
    with http_server() if transport == "http" else nullcontext() as url:
        servers = {"math": {"transport": "http", "url": url}} if url else None
        path = write_config(tmp_path, servers)
        bridge = MCPToolBridge(path, timeout=10)
        for _ in range(2):
            with bridge:
                tools = {tool.name: tool for tool in bridge.tools}
                assert set(tools) == {"mcp_math_add", "mcp_math_fail", "mcp_math_slow"}
                assert set(tools["mcp_math_add"].args) == {"a", "b"}
                result = invoke(tools["mcp_math_add"], a=19, b=23)
                assert result.artifact["status"] == "success"
                assert '42' in result.content
                assert result.artifact["metadata"]["remote_artifact"]
                failed = invoke(tools["mcp_math_fail"])
                assert failed.artifact["status"] == "tool_error"
                assert "non-zero" in failed.content
                invalid = invoke(tools["mcp_math_add"], a="bad", b=2)
                assert invalid.artifact["status"] == "tool_error"
            assert not bridge.thread.is_alive()


def test_real_timeout_cancels_transport_and_next_call_recovers(tmp_path):
    path = write_config(tmp_path)
    with MCPToolBridge(path, timeout=10) as bridge:
        tools = {tool.name: tool for tool in bridge.tools}
        bridge.timeout = 5
        started = tmp_path / "slow-started"
        response = invoke(tools["mcp_math_slow"], delay=30, started_path=str(started))
        assert response.artifact["code"] == "mcp_timeout"
        assert started.read_text(encoding="utf-8") == "started"
        bridge.timeout = 10
        assert invoke(tools["mcp_math_add"], a=1, b=2).artifact["status"] == "success"
    assert not bridge.thread.is_alive()


def test_two_servers_exposing_the_same_tool_are_namespaced(tmp_path):
    path = write_config(tmp_path)
    config = json.loads(path.read_text(encoding="utf-8"))
    config["servers"]["second"] = config["servers"]["math"]
    config["allowed_tools"]["second"] = ["add"]
    config["readonly_tools"]["second"] = ["add"]
    path.write_text(json.dumps(config), encoding="utf-8")
    with MCPToolBridge(path, timeout=10) as bridge:
        tools = {tool.name: tool for tool in bridge.tools}
        assert {"mcp_math_add", "mcp_second_add"}.issubset(tools)
        assert invoke(tools["mcp_second_add"], a=2, b=3).content.startswith("{")


def test_real_mcp_errors_reach_agent_and_readonly_needs_no_approval(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    settings = Settings(workspace, tmp_path / "state", approval_mode="ASK",
                        mcp_config_path=write_config(tmp_path))
    model = ScriptedModel(replies=[call("mcp_math_add", a="bad", b=3), call("mcp_math_add", a=2, b=3),
                                  "The corrected calculation is 5."])
    with AgentApplication(settings, model=model) as app:
        result = app.run("Exercise a remote error and recover")
        messages = app.graph.get_state(app.config(result["session_id"])).values["messages"]
    assert result["status"] == "completed"
    assert not result["interrupts"]
    assert [m.status for m in messages if m.type == "tool"] == ["error", "success"]


def test_real_mcp_side_effect_approval_deny_and_contract(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    settings = Settings(workspace, tmp_path / "state", approval_mode="ASK",
                        mcp_config_path=write_config(tmp_path, readonly=False))
    with AgentApplication(settings, model=ScriptedModel(replies=[call("mcp_math_add", a=2, b=3)])) as app:
        paused = app.run("Call external tool")
    assert paused["status"] == "interrupted"
    with AgentApplication(settings, model=ScriptedModel(replies=["Approved result: 5"])) as app:
        resumed = app.run(None, thread_id=paused["session_id"], decisions=[{"type": "approve"}])
    assert resumed["status"] == "completed"
    denied = replace(settings, approval_mode="DENY")
    with AgentApplication(denied, model=ScriptedModel(replies=[call("mcp_math_add", a=2, b=3), "Denied"])) as app:
        result = app.run("Call external tool")
    assert result["status"] == "incomplete"
    assert any(e.get("status") == "error" for e in result["trace"] if e["event"] == "tool")
    contract = CodingTaskContract("external", "Do not allow unknown mutation paths", ("code.py",),
                                  RequiredCommand("python", ("-m", "unittest", "-q")))
    with AgentApplication(replace(settings, approval_mode="ALLOW"), model=ScriptedModel(replies=[
        call("mcp_math_add", a=2, b=3), "Cannot perform external mutation", RuntimeError("stop")
    ])) as app:
        result = app.run(contract.instruction, contract=contract)
        messages = app.graph.get_state(app.config(result["session_id"])).values["messages"]
    assert any(m.type == "tool" and m.name == "mcp_math_add" and m.status == "error" for m in messages)
