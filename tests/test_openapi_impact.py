"""OpenAPI revision diffs and affected frontend call sites."""

import json

from mini_agent.openapi_impact import inspect_openapi_changes


def test_reports_changed_endpoint_and_frontend_callers(tmp_path):
    root = tmp_path / "workspace"
    (root / "backend").mkdir(parents=True)
    (root / "frontend/src").mkdir(parents=True)
    old = {
        "openapi": "3.1.0",
        "paths": {
            "/api/users": {"get": {"responses": {"200": {"description": "list"}}}},
            "/api/legacy": {"delete": {"responses": {"204": {"description": "ok"}}}},
        },
        "components": {"schemas": {"User": {"type": "object", "properties": {"id": {"type": "number"}}}}},
    }
    new = {
        "openapi": "3.1.0",
        "paths": {
            "/api/users": {"get": {"responses": {"200": {"description": "updated list"}}}},
            "/api/new": {"get": {"responses": {"200": {"description": "ok"}}}},
        },
        "components": {"schemas": {"User": {"type": "object", "properties": {"id": {"type": "string"}}}}},
    }
    (root / "backend/openapi.previous.json").write_text(json.dumps(old), encoding="utf-8")
    (root / "backend/openapi.json").write_text(json.dumps(new), encoding="utf-8")
    (root / "frontend/src/client.ts").write_text(
        'fetch("/api/users");\n'
        'axios.delete("/api/legacy");\n'
        'fetch("/api/unchanged");\n',
        encoding="utf-8",
    )
    report = json.loads(inspect_openapi_changes(workspace=root))
    assert report["operation_changes_total"] == 3
    assert report["schema_changes_total"] == 1
    assert report["changed_schema_names"] == ["User"]
    assert {(entry["method"], entry["path"], entry["change"]) for entry in report["operation_changes"]} == {
        ("GET", "/api/users", "modified"),
        ("DELETE", "/api/legacy", "removed"),
        ("GET", "/api/new", "added"),
    }
    assert {(item["method"], item["path"]) for item in report["affected_frontend_calls"]} == {
        ("GET", "/api/users"), ("DELETE", "/api/legacy"),
    }
    assert all(x["file"] == "frontend/src/client.ts" for x in report["affected_frontend_calls"])
