"""One-call Python-backend + Node-frontend checks using existing dev tools."""

import json
import os

import pytest

from helpers import ScriptedModel, call
from mini_agent import fullstack_checks
from mini_agent.result import ToolResult
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


@pytest.fixture
def joint_workspace(tmp_path):
    root = tmp_path / "workspace"
    backend = root / "backend"
    frontend = root / "frontend"
    backend.mkdir(parents=True)
    frontend.mkdir()
    (backend / "requirements.txt").write_text("", encoding="utf-8")
    executable = backend / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fake project interpreter")
    (backend / "tests").mkdir()
    (backend / "tests" / "test_sample.py").write_text(
        "def test_sample(): assert 2 + 2 == 4\n", encoding="utf-8",
    )
    (frontend / "package.json").write_text(json.dumps({
        "scripts": {"test": "vitest run", "build": "vite build"},
    }), encoding="utf-8")
    return root


def test_two_projects_are_checked_with_expected_commands(joint_workspace, monkeypatch):
    seen = []

    def fake_execute(argv, project, workspace, *, timeout):
        seen.append((argv, project))
        return ToolResult("exit_code=0", "success", "command_succeeded", {"returncode": 0})

    monkeypatch.setattr(fullstack_checks.dev, "_execute", fake_execute)
    result = fullstack_checks.run_fullstack_checks(
        workspace=joint_workspace, backend_task="test", frontend_task="build",
        backend_test_files=["tests/test_sample.py"],
    )
    assert result.status == "success"
    assert result.metadata["both_passed"] is True
    assert seen[0][0][1:] == ["-m", "pytest", "-q", "--", "tests/test_sample.py"]
    assert seen[1] == (["npm", "run", "build"], joint_workspace / "frontend")
    assert "[BACKEND]" in result.text
    assert "[FRONTEND]" in result.text


def test_backend_failure_does_not_skip_frontend_and_reports_both(
    joint_workspace, monkeypatch,
):
    seen = []

    def fake_execute(argv, project, workspace, *, timeout):
        seen.append(project.name)
        if project.name == "backend":
            return ToolResult("src/api.py:5: AssertionError", "command_failed", "exit_nonzero", {
                "returncode": 1, "diagnostics": [{"file": "src/api.py", "line": 5}],
            })
        return ToolResult("frontend built", "success", "command_succeeded", {"returncode": 0})

    monkeypatch.setattr(fullstack_checks.dev, "_execute", fake_execute)
    result = fullstack_checks.run_fullstack_checks(
        workspace=joint_workspace, backend_task="unittest", frontend_task="test",
    )
    assert seen == ["backend", "frontend"]
    assert result.status == "command_failed"
    assert result.metadata["both_passed"] is False
    assert result.metadata["backend"]["metadata"]["diagnostics"][0]["line"] == 5
    assert result.metadata["frontend"]["status"] == "success"


def test_unprepared_projects_or_missing_frontend_scripts_get_actionable_errors(joint_workspace):
    exe = fullstack_checks.dev._python_exe(joint_workspace / "backend")
    exe.unlink()
    with pytest.raises(ValueError, match="prepare_dev_environment"):
        fullstack_checks.run_fullstack_checks(workspace=joint_workspace)
    exe.write_bytes(b"fake")
    with pytest.raises(ValueError, match="package.json"):
        fullstack_checks.run_fullstack_checks(
            workspace=joint_workspace, frontend_task="lint",
        )


def test_registered_tool_works_inside_langgraph_agent(joint_workspace, tmp_path, monkeypatch):
    def fake_execute(argv, project, workspace, *, timeout):
        return ToolResult("ok", "success", "command_succeeded", {"returncode": 0})

    monkeypatch.setattr(fullstack_checks.dev, "_execute", fake_execute)
    model = ScriptedModel(replies=[
        call("run_fullstack_checks", backend_path="backend", frontend_path="frontend",
             backend_task="unittest", frontend_task="build"),
        "Both project checks passed.",
    ])
    settings = Settings(
        workspace=joint_workspace, state_dir=tmp_path / "state", approval_mode="ALLOW",
    )
    with AgentApplication(settings, model=model) as app:
        result = app.run("Run both backend tests and frontend build")
    assert result["status"] == "completed", result
    assert any(e["event"] == "tool" and e["name"] == "run_fullstack_checks"
               and e["status"] == "success" for e in result["trace"])
