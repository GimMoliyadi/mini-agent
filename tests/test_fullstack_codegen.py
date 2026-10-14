"""FastAPI export and openapi-fetch client scaffolding behavior.

These deterministic fixtures do not require a real backend or npm install.
"""

import json
import os
from pathlib import Path

import pytest

from helpers import ScriptedModel, call
from mini_agent import fullstack_codegen as codegen
from mini_agent.result import ToolResult
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "workspace"
    backend = root / "backend"
    frontend = root / "frontend"
    (backend / "app").mkdir(parents=True)
    (frontend / "src").mkdir(parents=True)
    (backend / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    exe = backend / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"project local Python")
    (frontend / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {"openapi-fetch": "^0.15"}}),
        encoding="utf-8",
    )
    (frontend / "src" / "openapi.d.ts").write_text(
        "export interface paths { '/api/users': { get: {} } }\n", encoding="utf-8",
    )
    return root


def _export_success(argv, project, workspace, *, timeout):
    assert argv[1] == "-c"
    assert argv[-3:-1] == ["app.main", "app"]
    Path(argv[-1]).write_text(json.dumps({
        "openapi": "3.1.0", "info": {"title": "Example API"},
        "paths": {"/api/users": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }), encoding="utf-8")
    return ToolResult("ok", "success", "command_succeeded", {"returncode": 0})


def test_export_fastapi_app_writes_reusable_openapi_json(project, monkeypatch):
    monkeypatch.setattr(codegen.dev, "_execute", _export_success)
    result = codegen.export_fastapi_openapi(
        workspace=project, app_import="app.main:app",
    )
    assert result.status == "success"
    doc = json.loads((project / "backend" / "openapi.json").read_text(encoding="utf-8"))
    assert doc["openapi"] == "3.1.0"
    assert "/api/users" in doc["paths"]


def test_failed_backend_export_preserves_previous_document(project, monkeypatch):
    path = project / "backend" / "openapi.json"
    path.write_text("original", encoding="utf-8")
    monkeypatch.setattr(codegen.dev, "_execute", lambda *a, **k: ToolResult(
        "import failed", "command_failed", "exit_nonzero",
    ))
    result = codegen.export_fastapi_openapi(workspace=project, app_import="app.main:app")
    assert result.status == "command_failed"
    assert path.read_text(encoding="utf-8") == "original"


def test_typed_client_imports_generated_types_and_openapi_fetch(project):
    result = codegen.generate_typed_api_client(
        workspace=project, base_url="/api",
    )
    assert result.status == "success"
    generated = (project / "frontend" / "src" / "api-client.ts").read_text(encoding="utf-8")
    assert 'import createClient from "openapi-fetch";' in generated
    assert 'import type { paths } from "./openapi";' in generated
    assert 'export const api = createClient<paths>({ baseUrl: "/api" });' in generated


def test_types_import_is_relative_to_nested_client_directory(project):
    src = project / "frontend" / "src"
    (src / "api").mkdir()
    codegen.generate_typed_api_client(
        workspace=project, output_path="frontend/src/api/client.ts",
    )
    generated = (src / "api" / "client.ts").read_text(encoding="utf-8")
    assert 'from "../openapi"' in generated


def test_missing_openapi_fetch_dependency_has_useful_error(project):
    package = project / "frontend" / "package.json"
    package.write_text('{"name":"web"}', encoding="utf-8")
    with pytest.raises(ValueError, match="openapi-fetch"):
        codegen.generate_typed_api_client(workspace=project)


def test_agent_can_chain_schema_export_and_typed_client(project, tmp_path, monkeypatch):
    monkeypatch.setattr(codegen.dev, "_execute", _export_success)
    model = ScriptedModel(replies=[
        call("export_fastapi_openapi", backend_path="backend", app_import="app.main:app"),
        call("generate_typed_api_client", frontend_path="frontend",
             types_path="frontend/src/openapi.d.ts", output_path="frontend/src/api-client.ts"),
        "Exported OpenAPI and created the typed frontend API client.",
    ])
    settings = Settings(
        workspace=project, state_dir=tmp_path / "state", approval_mode="ALLOW",
    )
    with AgentApplication(settings, model=model) as app:
        result = app.run("Export FastAPI OpenAPI and create a frontend client")
    assert result["status"] == "completed", result
    assert (project / "backend" / "openapi.json").exists()
    assert (project / "frontend" / "src" / "api-client.ts").exists()
