"""Lifecycle-managed FastAPI and Vite development servers.

The standard run_dev_task tool is for finite jobs. This manager holds two
explicitly selected dev servers during one AgentApplication lifetime, and
uses the existing process runner's cross-platform process tree cleanup.
"""

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from typing import Any, Literal

from langchain.tools import tool

from . import dev_environment as dev
from . import process_runner
from . import sandbox

_APP = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")
_MAX_LOG_CHUNKS = 48
_MAX_LOG_CHUNK_CHARS = 400
_MAX_LOG_CHARS = 3600
_SERVICE_NAMES = ("backend", "frontend")


def _port_reachable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.15):
            return True
    except OSError:
        return False


class _Server:
    def __init__(
        self, process: subprocess.Popen[bytes], job: Any, project: Path,
        argv: list[str], port: int,
    ):
        self.process = process
        self.job = job
        self.project = project
        self.argv = argv
        self.port = port
        self.logs: deque[str] = deque(maxlen=_MAX_LOG_CHUNKS)
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        for stream in (process.stdout, process.stderr):
            assert stream is not None
            thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _drain(self, stream: Any) -> None:
        while True:
            try:
                chunk = stream.read(512)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            message = chunk.decode("utf-8", errors="replace")
            with self.lock:
                self.logs.append(message[-_MAX_LOG_CHUNK_CHARS:])

    def tail(self) -> str:
        with self.lock:
            return sandbox.redact_text("".join(self.logs)[-_MAX_LOG_CHARS:])

    def status(self) -> dict[str, Any]:
        return {
            "running": self.process.poll() is None,
            "pid": self.process.pid,
            "exit_code": self.process.poll(),
            "port": self.port,
            "url": f"http://127.0.0.1:{self.port}",
            "port_open": _port_reachable(self.port) if self.process.poll() is None else False,
            "last_output": self.tail(),
        }

    def stop(self) -> None:
        try:
            process_runner._stop_process_tree(self.process, self.job)
        finally:
            for thread in self.threads:
                thread.join(timeout=5)
            process_runner._close_process_handles(self.process)


class DevServerManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.servers: dict[str, _Server] = {}
        self.lock = threading.RLock()

    def start(
        self, name: Literal["backend", "frontend"], path: str, port: int,
        app_import: str = "main:app", script: str = "dev",
    ) -> dict[str, Any]:
        if name not in _SERVICE_NAMES:
            raise ValueError("服务器名只能是 backend 或 frontend")
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("port 须为 1024–65535 的本地开发端口")
        with self.lock:
            existing = self.servers.get(name)
            if existing is not None:
                if existing.process.poll() is None:
                    raise ValueError(f"{name} 已在运行；请先调用 status 或 stop")
                existing.stop()
                del self.servers[name]
            if _port_reachable(port):
                raise ValueError(f"本机端口 {port} 已被占用")
            project = dev._project(self.workspace, path)
            if name == "backend":
                dev._manager(project, "python")
                exe = dev._python_exe(project)
                if not exe.is_file():
                    raise ValueError("后端虚拟环境未准备；请先运行 prepare_dev_environment")
                if not _APP.fullmatch(app_import):
                    raise ValueError("app_import 须为 FastAPI module:app 的导入路径")
                argv = [
                    str(exe), "-m", "uvicorn", app_import,
                    "--host", "127.0.0.1", "--port", str(port),
                ]
            else:
                manager = dev._manager(project, "node")
                scripts = (dev._manifest(project) or {}).get("scripts", {})
                if script != "dev" or script not in scripts or not re.search(
                    r"(^|[\s/])vite(?:\s|$)", scripts[script],
                ):
                    raise ValueError("当前前端服务器管理支持 package.json 的 Vite dev 脚本")
                argv = [
                    manager, "run", script, "--", "--host", "127.0.0.1",
                    "--port", str(port), "--strictPort",
                ]
            process, job = process_runner._start_process(
                argv, project, process_runner.command_environment(self.workspace),
            )
            server = _Server(process, job, project, argv, port)
            self.servers[name] = server
            # Not a readiness guarantee: report listening separately from process creation.
            time.sleep(0.1)
            state = server.status()
            state.update({"name": name, "path": project.relative_to(self.workspace).as_posix()})
            return state

    def status(self, name: Literal["backend", "frontend"] = "backend") -> dict[str, Any]:
        with self.lock:
            server = self.servers.get(name)
            if server is None:
                return {"name": name, "running": False, "managed": False}
            return {"name": name, "managed": True, **server.status()}

    def stop(self, name: Literal["backend", "frontend"]) -> dict[str, Any]:
        with self.lock:
            server = self.servers.pop(name, None)
            if server is None:
                return {"name": name, "stopped": False, "reason": "not_started"}
            server.stop()
            return {"name": name, "stopped": True}

    def close(self) -> None:
        with self.lock:
            for name in list(self.servers):
                self.stop(name)


def build_dev_server_tools(manager: DevServerManager) -> list[Any]:
    @tool
    def dev_server(
        action: Literal["start", "status", "logs", "stop"],
        name: Literal["backend", "frontend"] = "backend",
        path: str = "",
        port: int = 8000,
        app_import: str = "main:app",
        script: str = "dev",
    ) -> str:
        """Start or inspect/stop FastAPI and Vite servers for local browser testing.

        start: backend uses the project .venv and uvicorn; frontend runs the
        declared Vite dev script. status/logs/stop retain state during this
        AgentApplication; processes are always terminated on application exit.
        This tool only manages local development servers.
        """
        if action == "start":
            selected = path or ("backend" if name == "backend" else "frontend")
            result = manager.start(name, selected, port, app_import, script)
        elif action == "stop":
            result = manager.stop(name)
        else:
            result = manager.status(name)
            if action == "logs" and result.get("managed"):
                result["last_output"] = manager.servers[name].tail()
        return sandbox.redact_text(json.dumps(result, ensure_ascii=False))

    dev_server.metadata = {
        "risk": "execute", "mutation": False,
        "workspace_arguments": ("path",),
    }
    return [dev_server]
