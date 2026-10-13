"""OpenAPI contract inspection and project-installed TypeScript generation tests."""

import json
from pathlib import Path

import pytest

from helpers import ScriptedModel, call
from mini_agent import openapi_contract as oc
from mini_agent.result import ToolResult
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings
from mini_agent.verifier import CodingTaskContract, TestCommand as RequiredTestCommand


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "workspace"
    (root / "backend").mkdir(parents=True)
    (root / "frontend" / "src").mkdir(parents=True)
    contract = {
        "openapi": "3.1.0",
        "info": {"title": "Sample User API", "version": "1.0"},
        "paths": {
            "/api/users": {
                "get": {"operationId": "getUsers", "responses": {"200": {"description": "OK"}}},
                "post": {
                    "operationId": "createUser", "requestBody": {"content": {"application/json": {}}},
                    "responses": {"201": {"description": "Created"}},
                },
            },
            "/api/users/{id}": {
                "get": {"operationId": "getUser", "responses": {"200": {"description": "OK"}}},
            },
        },
        "components": {"schemas": {
            "User": {"type": "object", "properties": {"id": {"type": "integer"}}},
            "CreateUser": {"type": "object", "properties": {"name": {"type": "string"}}},
        }},
    }
    (root / "backend" / "openapi.json").write_text(json.dumps(contract), encoding="utf-8")
    (root / "frontend" / "package.json").write_text(json.dumps({
        "name": "web",
        "devDependencies": {"openapi-typescript": "^7.0.0", "typescript": "^5.0.0"},
        "scripts": {"typecheck": "tsc --noEmit", "build": "vite build"},
    }), encoding="utf-8")
    cli = root / "frontend" / "node_modules" / "openapi-typescript" / "bin" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("// mock local binary\n", encoding="utf-8")
    return root


def _fake_types(argv, cwd, workspace, *, timeout):
    assert argv[0] == "node"
    assert argv[1].endswith("openapi-typescript/bin/cli.js") or argv[1].endswith(
        r"openapi-typescript\bin\cli.js"
    )
    assert argv[2].endswith("openapi.json")
    assert argv[3] == "-o"
    Path(argv[4]).write_text(
        "export interface paths { '/api/users': {get: unknown} }\n",
        encoding="utf-8",
    )
    return ToolResult("generated", "success", "command_succeeded", {"returncode": 0})


def _settings(project, tmp_path, mode="ALLOW"):
    return Settings(workspace=project, state_dir=tmp_path / "state", approval_mode=mode)


def test_inspector_summarizes_paths_methods_and_component_names(project):
    report = json.loads(oc.inspect_openapi_contract(workspace=project))
    assert report["title"] == "Sample User API"
    assert report["openapi_version"] == "3.1.0"
    assert report["operation_count"] == 3
    assert report["component_schemas"] == ["CreateUser", "User"]
    operations = {(item["method"], item["path"]): item for item in report["operations"]}
    assert ("GET", "/api/users") in operations
    assert operations["POST", "/api/users"]["request_body"] is True
    assert operations["POST", "/api/users"]["response_codes"] == ["201"]


def test_generates_types_using_frontend_installed_openapi_typescript(project, monkeypatch):
    monkeypatch.setattr(oc.dev, "_execute", _fake_types)
    result = oc.generate_openapi_types(
        "backend/openapi.json", "frontend", "frontend/src/openapi.d.ts",
        workspace=project,
    )
    assert result.status == "success"
    assert result.metadata["output"] == "frontend/src/openapi.d.ts"
    assert "export interface paths" in (project / "frontend/src/openapi.d.ts").read_text(
        encoding="utf-8",
    )
    assert list((project / "frontend/src").glob(".mini-agent-types-*")) == []


def test_generation_failure_keeps_existing_output(project, monkeypatch):
    output = project / "frontend/src/openapi.d.ts"
    output.write_text("original types\n", encoding="utf-8")
    def failed(*args, **kwargs):
        return ToolResult("CLI failed", "command_failed", "exit_nonzero", {"returncode": 1})
    monkeypatch.setattr(oc.dev, "_execute", failed)
    result = oc.generate_openapi_types(
        "backend/openapi.json", "frontend", "frontend/src/openapi.d.ts",
        workspace=project,
    )
    assert result.status == "command_failed"
    assert output.read_text(encoding="utf-8") == "original types\n"


def test_missing_generator_reports_install_workflow(project):
    cli = project / "frontend/node_modules/openapi-typescript/bin/cli.js"
    cli.unlink()
    with pytest.raises(ValueError, match="prepare_dev_environment"):
        oc.generate_openapi_types(
            "backend/openapi.json", "frontend", "frontend/src/openapi.d.ts",
            workspace=project,
        )


def test_rejects_swagger2_and_non_openapi_json(project):
    spec = project / "backend/openapi.json"
    spec.write_text('{"swagger": "2.0", "paths": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="OpenAPI"):
        oc.inspect_openapi_contract(workspace=project)
    spec.write_text('{"openapi": "2.0", "paths": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="3.x"):
        oc.inspect_openapi_contract(workspace=project)


def test_inspection_and_generation_are_real_agent_tools(project, tmp_path, monkeypatch):
    monkeypatch.setattr(oc.dev, "_execute", _fake_types)
    model = ScriptedModel(replies=[
        call("inspect_openapi_contract", schema_path="backend/openapi.json"),
        call("generate_openapi_types", schema_path="backend/openapi.json",
             frontend_path="frontend", output_path="frontend/src/openapi.d.ts"),
        "Generated TypeScript declarations from the OpenAPI contract.",
    ])
    with AgentApplication(_settings(project, tmp_path), model=model) as app:
        result = app.run("Inspect the API contract and produce frontend TypeScript declarations")
    assert result["status"] == "completed", result
    assert (project / "frontend/src/openapi.d.ts").is_file()
    assert any(e["event"] == "tool" and e["name"] == "generate_openapi_types"
               and e["status"] == "success" for e in result["trace"])


def test_generation_requires_single_approval(project, tmp_path, monkeypatch):
    monkeypatch.setattr(oc.dev, "_execute", _fake_types)
    model = ScriptedModel(replies=[
        call("generate_openapi_types", schema_path="backend/openapi.json",
             frontend_path="frontend", output_path="frontend/src/openapi.d.ts"),
        "Done.",
    ])
    with AgentApplication(_settings(project, tmp_path, "ASK"), model=model) as app:
        paused = app.run("Generate TypeScript declarations")
        assert paused["status"] == "interrupted", paused
        assert not (project / "frontend/src/openapi.d.ts").exists()
        complete = app.run(None, thread_id=paused["session_id"], decisions=[{"type": "approve"}])
    assert complete["status"] == "completed", complete
    assert (project / "frontend/src/openapi.d.ts").exists()


def test_generation_obeys_coding_contract_allowed_paths(project, tmp_path, monkeypatch):
    monkeypatch.setattr(oc.dev, "_execute", _fake_types)
    contract = CodingTaskContract(
        "generate-types", "Only change the API backend", ("backend/openapi.json",),
        RequiredTestCommand("python", ("-m", "unittest", "discover", "-s", "tests", "-q")),
    )
    model = ScriptedModel(replies=[
        call("generate_openapi_types", schema_path="backend/openapi.json",
             frontend_path="frontend", output_path="frontend/src/openapi.d.ts"),
        "Cannot modify paths outside the contract.",
    ])
    with AgentApplication(_settings(project, tmp_path), model=model) as app:
        result = app.run("Generate API types", contract=contract)
    assert result["status"] != "completed"
    assert not (project / "frontend/src/openapi.d.ts").exists()
