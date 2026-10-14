"""Real FastAPI + React/Vite + OpenAPI + Chromium acceptance.

Opt in with MINI_AGENT_RUN_REAL_WEB_ACCEPTANCE=1. This intentionally installs
third-party npm packages, launches real local servers and opens real Chromium.
It is not part of the hermetic/offline default regression.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from helpers import ScriptedModel, call
from mini_agent.browser_tools import BrowserSession
from mini_agent.dev_servers import DevServerManager
from mini_agent.fullstack_checks import run_fullstack_checks
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings
from mini_agent import dev_environment as dev


pytestmark = pytest.mark.skipif(
    os.environ.get("MINI_AGENT_RUN_REAL_WEB_ACCEPTANCE") != "1",
    reason="Enable with MINI_AGENT_RUN_REAL_WEB_ACCEPTANCE=1 and install browser/web dependencies",
)


_BACKEND = """from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Mini Agent Real Web Acceptance")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

class Greeting(BaseModel):
    message: str

@app.get("/api/greeting", response_model=Greeting)
def greeting(name: str = "Visitor") -> Greeting:
    return Greeting(message=f"Hello, {name}!")
"""

_BACKEND_TEST = """from fastapi.testclient import TestClient
from main import app

def test_real_greeting():
    response = TestClient(app).get("/api/greeting", params={"name": "Agent"})
    assert response.status_code == 200
    assert response.json() == {"message": "Hello, Agent!"}
"""

_FRONTEND = """import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { $api } from "./api-hooks";

function App() {
  const [name, setName] = useState("Visitor");
  const { data, error, isLoading } = $api.useQuery(
    "get",
    "/api/greeting",
    { params: { query: { name } } },
  );
  return (
    <main>
      <h1>Real Web Acceptance</h1>
      <button id="change-name" onClick={() => setName("Agent")}>Greet Agent</button>
      <p id="answer">{isLoading ? "Loading..." : error ? "Request failed" : data?.message ?? "No data"}</p>
    </main>
  );
}
const queryClient = new QueryClient();
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}><App /></QueryClientProvider>
  </React.StrictMode>,
);
"""


def _prepare_workspace(root: Path) -> None:
    backend = root / "backend"
    frontend = root / "frontend"
    (frontend / "src").mkdir(parents=True)
    backend.mkdir()
    (backend / "requirements.txt").write_text(
        "fastapi\nuvicorn\npytest\nhttpx\n", encoding="utf-8",
    )
    (backend / "main.py").write_text(_BACKEND, encoding="utf-8")
    (backend / "test_main.py").write_text(_BACKEND_TEST, encoding="utf-8")
    package = {
        "name": "mini-agent-real-web-acceptance",
        "private": True,
        "version": "1.0.0",
        "type": "module",
        "scripts": {
            "dev": "vite",
            "build": "tsc --noEmit && vite build",
            "typecheck": "tsc --noEmit",
        },
        "dependencies": {
            "@tanstack/react-query": "^5.0.0",
            "openapi-fetch": "^0.17.0",
            "openapi-react-query": "^0.5.0",
            "react": "^19.0.0",
            "react-dom": "^19.0.0",
        },
        "devDependencies": {
            "@types/react": "^19.0.0",
            "@types/react-dom": "^19.0.0",
            "@vitejs/plugin-react": "^4.0.0",
            "openapi-typescript": "^7.0.0",
            "typescript": "^5.0.0",
            "vite": "^6.0.0",
        },
    }
    (frontend / "package.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (frontend / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {
            "target": "ES2022", "lib": ["ES2022", "DOM", "DOM.Iterable"],
            "module": "ESNext", "moduleResolution": "Bundler",
            "jsx": "react-jsx", "strict": True, "noEmit": True,
            "skipLibCheck": True, "esModuleInterop": True,
            "types": ["vite/client"],
        },
        "include": ["src"],
    }, indent=2), encoding="utf-8")
    (frontend / "index.html").write_text(
        '<!doctype html><html><head><meta charset="utf-8"><title>Agent Acceptance</title></head>'
        '<body><div id="root"></div><script type="module" src="/src/main.tsx"></script></body></html>',
        encoding="utf-8",
    )
    (frontend / "src" / "main.tsx").write_text(_FRONTEND, encoding="utf-8")
    (root / "screenshots").mkdir()


def _install_dependencies(root: Path) -> None:
    # --system-site-packages reuses the installed CI FastAPI, pytest and uvicorn;
    # this is still a real project-specific .venv executable.
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages",
         str(root / "backend" / ".venv")],
        check=True, timeout=45,
    )
    subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund"],
        cwd=root / "frontend", check=True, timeout=180,
    )


def _wait_for_server(manager: DevServerManager, name: str) -> None:
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        status = manager.status(name)  # type: ignore[arg-type]
        if status.get("port_open"):
            return
        if not status.get("running"):
            raise AssertionError(f"{name} exited before becoming ready: {status}")
        time.sleep(0.25)
    raise AssertionError(f"{name} did not become ready: {manager.status(name)}")  # type: ignore[arg-type]


def _wait_for_text(browser: BrowserSession, expected: str) -> None:
    deadline = time.monotonic() + 25
    observed = ""
    while time.monotonic() < deadline:
        observed = browser.invoke("inspect", "", "#answer", "")["text"]
        if observed == expected:
            return
        time.sleep(0.4)
    raise AssertionError(f"Expected {expected!r}, got {observed!r}; "
                         f"diagnostics: {browser.diagnostic_snapshot()}")


def test_actual_fastapi_react_openapi_and_chromium_flow(tmp_path):
    root = tmp_path / "real-web-workspace"
    root.mkdir()
    _prepare_workspace(root)
    _install_dependencies(root)

    settings = Settings(
        workspace=root, state_dir=tmp_path / "agent-state",
        approval_mode="ALLOW", max_model_calls=25,
    )
    # LangGraph's real tool loop executes the actual export/TS/client/hook tools;
    # only its LLM messages are deterministic scripted responses.
    model = ScriptedModel(replies=[
        call("export_fastapi_openapi", backend_path="backend",
             app_import="main:app", output_path="backend/openapi.json"),
        call("inspect_openapi_contract", schema_path="backend/openapi.json"),
        call("generate_openapi_types", schema_path="backend/openapi.json",
             frontend_path="frontend", output_path="frontend/src/openapi.d.ts"),
        call("generate_typed_api_client", frontend_path="frontend",
             types_path="frontend/src/openapi.d.ts",
             output_path="frontend/src/api-client.ts",
             base_url="http://127.0.0.1:8000"),
        call("generate_react_query_hooks", frontend_path="frontend",
             client_path="frontend/src/api-client.ts",
             output_path="frontend/src/api-hooks.ts"),
        "Created actual OpenAPI declarations and typed React Query integration.",
    ])
    with AgentApplication(settings, model=model) as app:
        result = app.run("Set up typed FastAPI-to-React API bindings")
    assert result["status"] == "completed", result
    expected_files = (
        "backend/openapi.json",
        "frontend/src/openapi.d.ts",
        "frontend/src/api-client.ts",
        "frontend/src/api-hooks.ts",
    )
    for filename in expected_files:
        assert (root / filename).is_file(), filename

    checks = run_fullstack_checks(
        workspace=root, backend_path="backend", frontend_path="frontend",
        backend_task="test", frontend_task="typecheck",
    )
    assert checks.status == "success", checks.text
    assert checks.metadata["both_passed"], checks.text
    build = dev._execute(
        ["npm", "run", "build"], root / "frontend", root, timeout=120,
    )
    assert build.status == "success", build.text

    servers = DevServerManager(root)
    browser = BrowserSession(root)
    try:
        backend_started = servers.start("backend", "backend", 8000, app_import="main:app")
        assert backend_started["running"], backend_started
        frontend_started = servers.start("frontend", "frontend", 5173)
        assert frontend_started["running"], frontend_started
        _wait_for_server(servers, "backend")
        _wait_for_server(servers, "frontend")
        page = browser.invoke("open", "http://127.0.0.1:5173", "body", "")
        assert page["title"] == "Agent Acceptance"
        _wait_for_text(browser, "Hello, Visitor!")
        browser.invoke("click", "", "#change-name", "")
        _wait_for_text(browser, "Hello, Agent!")
        diagnostics = browser.diagnostic_snapshot()
        assert not [event for event in diagnostics["events"]
                    if event["kind"] in {
                        "console_error", "javascript_exception",
                        "request_failed", "http_error",
                    }], diagnostics
        screenshot = browser.capture("screenshots/acceptance.png")
        assert screenshot["bytes"] > 100
        assert (root / screenshot["output"]).read_bytes().startswith(b"\x89PNG")
    finally:
        browser.shutdown()
        servers.close()
    assert not servers.servers
