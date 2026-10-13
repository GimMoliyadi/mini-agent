"""Static FastAPI/JS interface mapping and integration with the real agent graph."""

import json

import pytest

from helpers import ScriptedModel, call
from mini_agent.fullstack import inspect_fullstack_interfaces
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


@pytest.fixture
def fullstack_repo(tmp_path):
    root = tmp_path / "workspace"
    (root / "backend" / "api").mkdir(parents=True)
    (root / "frontend" / "src").mkdir(parents=True)
    (root / "backend" / "api" / "users.py").write_text(
        'from fastapi import APIRouter, FastAPI\n'
        'router = APIRouter(prefix="/api")\n'
        '@router.get("/users")\n'
        'def list_users():\n    return []\n\n'
        '@router.post("/users")\n'
        'def create_user():\n    return {}\n\n'
        '@router.get("/users/{user_id}")\n'
        'def detail(user_id: int):\n    return user_id\n\n'
        '@router.api_route("/users/{user_id}", methods=["PATCH"])\n'
        'def update_user(user_id: int):\n    return user_id\n\n'
        'app = FastAPI()\n'
        '@app.get("/health")\n'
        'def health():\n    return {"ok": True}\n',
        encoding="utf-8",
    )
    (root / "frontend" / "src" / "users.ts").write_text(
        'fetch("/api/users")\n'
        'fetch("/api/users", { method: "POST", body: "{}" })\n'
        'axios.get("/api/users/42")\n'
        'client.patch("/api/users/42")\n'
        'fetch("/api/missing")\n'
        'fetch("/api/users?sort=name")\n',
        encoding="utf-8",
    )
    return root


def _inspect(root):
    return json.loads(inspect_fullstack_interfaces(
        backend_path="backend", frontend_path="frontend", workspace=root,
    ))


def test_matches_fastapi_methods_paths_and_dynamic_route_parameters(fullstack_repo):
    result = _inspect(fullstack_repo)
    assert result["route_count"] == 5
    assert result["frontend_call_count"] == 6
    assert result["unmatched_frontend_calls"] == 1
    assert result["truncated"] is False
    assert [(r["method"], r["path"]) for r in result["routes"]] == [
        ("GET", "/api/users"),
        ("POST", "/api/users"),
        ("GET", "/api/users/{user_id}"),
        ("PATCH", "/api/users/{user_id}"),
        ("GET", "/health"),
    ]
    missing = [c for c in result["frontend_calls"] if c["status"] == "unmatched"]
    assert [(item["method"], item["path"]) for item in missing] == [
        ("GET", "/api/missing"),
    ]
    assert all(item["backend_matches"] for item in result["frontend_calls"]
               if item["status"] == "matched")


def test_http_method_mismatch_is_reported_as_unmatched(fullstack_repo):
    frontend = fullstack_repo / "frontend" / "src" / "users.ts"
    frontend.write_text(
        frontend.read_text(encoding="utf-8") + 'axios.delete("/api/users")\n',
        encoding="utf-8",
    )
    result = _inspect(fullstack_repo)
    unmatched = result["frontend_calls"]
    assert ("DELETE", "/api/users") in {
        (item["method"], item["path"]) for item in unmatched
        if item["status"] == "unmatched"
    }


def test_nonliteral_and_generated_dependencies_not_reported(fullstack_repo):
    ignored = fullstack_repo / "frontend" / "node_modules" / "sdk"
    ignored.mkdir(parents=True)
    (ignored / "client.ts").write_text(
        'fetch("/api/mystery")\n', encoding="utf-8",
    )
    frontend = fullstack_repo / "frontend" / "src" / "users.ts"
    frontend.write_text(
        frontend.read_text(encoding="utf-8")
        + "fetch(baseUrl + '/api/dynamic')\n",
        encoding="utf-8",
    )
    result = _inspect(fullstack_repo)
    assert result["frontend_call_count"] == 6
    assert not any(c["path"] == "/api/mystery" for c in result["frontend_calls"])


def test_scanner_rejects_missing_directories_and_escaping_workspace(fullstack_repo):
    with pytest.raises(NotADirectoryError):
        inspect_fullstack_interfaces("missing", "frontend", workspace=fullstack_repo)
    with pytest.raises(PermissionError):
        inspect_fullstack_interfaces("../outside", "frontend", workspace=fullstack_repo)


def test_real_agent_uses_fullstack_map_then_repairs_client_route(fullstack_repo, tmp_path):
    frontend = fullstack_repo / "frontend" / "src" / "users.ts"
    model = ScriptedModel(replies=[
        call("inspect_fullstack_interfaces", backend_path="backend", frontend_path="frontend"),
        call("apply_patch", path="frontend/src/users.ts",
             old_text='fetch("/api/missing")', new_text='fetch("/api/users")'),
        call("inspect_fullstack_interfaces", backend_path="backend", frontend_path="frontend"),
        "Updated the frontend request to match the backend route.",
    ])
    settings = Settings(
        workspace=fullstack_repo, state_dir=tmp_path / "state", approval_mode="ALLOW",
    )
    with AgentApplication(settings, model=model) as app:
        result = app.run("Align the frontend users API request with FastAPI routes")
    assert result["status"] == "completed", result
    assert 'fetch("/api/missing")' not in frontend.read_text(encoding="utf-8")
    assert _inspect(fullstack_repo)["unmatched_frontend_calls"] == 0
    assert sum(e["event"] == "tool" and e["name"] == "inspect_fullstack_interfaces"
               and e["status"] == "success" for e in result["trace"]) == 2
