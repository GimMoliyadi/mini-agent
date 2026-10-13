"""Development server lifecycle (mocked processes; no real servers needed)."""

import io
import json
import os

import pytest

from mini_agent import dev_servers


class FakeProcess:
    def __init__(self):
        self.pid = 12345
        self.returncode = None
        self.stdout = io.BytesIO(b"local dev server started\n")
        self.stderr = io.BytesIO(b"")

    def poll(self):
        return self.returncode


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    backend = root / "backend"
    frontend = root / "frontend"
    backend.mkdir(parents=True)
    frontend.mkdir()
    (backend / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    executable = backend / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"placeholder")
    (frontend / "package.json").write_text(json.dumps({
        "scripts": {"dev": "vite --host localhost"},
    }), encoding="utf-8")
    return root


def test_start_both_servers_and_close_all(workspace, monkeypatch):
    launches = []
    processes = []

    def fake_start(argv, cwd, env):
        launches.append((argv, cwd))
        proc = FakeProcess()
        processes.append(proc)
        return proc, None

    def fake_stop(process, job):
        process.returncode = -1

    monkeypatch.setattr(dev_servers, "_port_reachable", lambda port: False)
    monkeypatch.setattr(dev_servers.process_runner, "_start_process", fake_start)
    monkeypatch.setattr(dev_servers.process_runner, "_stop_process_tree", fake_stop)
    monkeypatch.setattr(dev_servers.process_runner, "_close_process_handles", lambda p: None)
    manager = dev_servers.DevServerManager(workspace)
    try:
        backend = manager.start("backend", "backend", 8000, app_import="app.main:app")
        frontend = manager.start("frontend", "frontend", 5173)
        assert backend["running"] is True
        assert frontend["running"] is True
        assert launches[0][0][1:4] == ["-m", "uvicorn", "app.main:app"]
        assert launches[1][0][-5:] == ["--host", "127.0.0.1", "--port", "5173", "--strictPort"]
        assert "local dev server" in manager.status("backend")["last_output"]
        assert manager.stop("backend")["stopped"]
        assert manager.status("backend")["managed"] is False
    finally:
        manager.close()
    assert all(p.returncode == -1 for p in processes)


def test_rejects_wrong_backend_app_and_frontend_script(workspace, monkeypatch):
    monkeypatch.setattr(dev_servers, "_port_reachable", lambda port: False)
    manager = dev_servers.DevServerManager(workspace)
    with pytest.raises(ValueError, match="app_import"):
        manager.start("backend", "backend", 8000, app_import="not-a-valid-import")
    with pytest.raises(ValueError, match="Vite"):
        manager.start("frontend", "frontend", 5173, script="build")
    with pytest.raises(ValueError, match="1024"):
        manager.start("backend", "backend", 80)
    assert manager.status("frontend")["managed"] is False


def test_dev_server_agent_tool_uses_manager(workspace, monkeypatch):
    manager = dev_servers.DevServerManager(workspace)
    monkeypatch.setattr(manager, "status", lambda name: {
        "name": name, "running": True, "url": "http://127.0.0.1:8000",
    })
    tools = dev_servers.build_dev_server_tools(manager)
    assert len(tools) == 1
    response = json.loads(tools[0].invoke({"action": "status", "name": "backend"}))
    assert response["running"] is True
    assert tools[0].metadata["risk"] == "execute"
