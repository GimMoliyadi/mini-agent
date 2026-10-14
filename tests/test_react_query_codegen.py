"""Typed OpenAPI React Query hooks scaffolding."""

import json

import pytest

from mini_agent.react_query_codegen import generate_react_query_hooks


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "workspace"
    src = root / "frontend" / "src"
    src.mkdir(parents=True)
    (root / "frontend" / "package.json").write_text(json.dumps({
        "name": "web",
        "dependencies": {
            "openapi-react-query": "^0.5",
            "openapi-fetch": "^0.15",
            "@tanstack/react-query": "^5.0",
        },
    }), encoding="utf-8")
    (src / "api-client.ts").write_text(
        'import createClient from "openapi-fetch";\n'
        'export const api = createClient();\n',
        encoding="utf-8",
    )
    return root


def test_hooks_use_mature_openapi_react_query_and_existing_client(project):
    result = generate_react_query_hooks(workspace=project)
    assert result.status == "success"
    text = (project / "frontend/src/api-hooks.ts").read_text(encoding="utf-8")
    assert 'import createClient from "openapi-react-query"' in text
    assert 'import { api } from "./api-client"' in text
    assert "export const $api = createClient(api);" in text
    assert "$api.useMutation" in text


def test_nested_hooks_use_relative_client_path(project):
    (project / "frontend/src/hooks").mkdir()
    result = generate_react_query_hooks(
        workspace=project, output_path="frontend/src/hooks/api-hooks.ts",
    )
    assert result.status == "success"
    text = (project / "frontend/src/hooks/api-hooks.ts").read_text(encoding="utf-8")
    assert 'from "../api-client"' in text


def test_missing_dependency_is_actionable(project):
    package = project / "frontend/package.json"
    package.write_text('{"name":"web"}', encoding="utf-8")
    with pytest.raises(ValueError, match="openapi-react-query"):
        generate_react_query_hooks(workspace=project)


def test_cannot_overwrite_client_or_write_outside_frontend(project):
    with pytest.raises(ValueError):
        generate_react_query_hooks(workspace=project, output_path="frontend/src/api-client.ts")
    with pytest.raises(ValueError):
        generate_react_query_hooks(workspace=project, output_path="api-hooks.ts")
