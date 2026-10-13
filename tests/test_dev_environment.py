"""Project execution: real Python environment and bounded npm/pnpm task integration."""

import json
import sys

import pytest

from helpers import ScriptedModel, call
from mini_agent import dev_environment
from mini_agent.dev_environment import build_dev_environment_tools
from mini_agent.process_runner import ProcessResult
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


def config(root, tmp_path, approval="ALLOW"):
    return Settings(workspace=root, state_dir=tmp_path / "state", approval_mode=approval)


@pytest.fixture
def python_project(tmp_path):
    folder = tmp_path / "python_workspace"
    folder.mkdir()
    project = folder / "api"
    project.mkdir()
    (project / "requirements.txt").write_text("", encoding="utf-8")
    (project / "test_calculator.py").write_text(
        "import unittest\nclass Smoke(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(2 + 3, 5)\n",
        encoding="utf-8",
    )
    return folder


@pytest.fixture
def node_project(tmp_path):
    root = tmp_path / "node_workspace"
    root.mkdir()
    frontend = root / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0",
                    "scripts": {"test": "node test.js", "build": "node build.js"}}),
        encoding="utf-8",
    )
    (frontend / "package-lock.json").write_text(
        '{"lockfileVersion":3,"packages":{}}', encoding="utf-8",
    )
    return root


def test_identify_mixed_language_and_subproject_manifests(python_project, node_project):
    python_info = dev_environment._detect(python_project / "api")
    assert python_info["python"] is True
    assert "requirements.txt" in python_info["python_manifests"]
    node_info = dev_environment._detect(node_project / "frontend")
    assert node_info["node_manager"] == "npm"
    assert node_info["node_scripts"] == ["build", "test"]
    assert node_info["node_lockfiles"] == ["package-lock.json"]


def test_python_project_creates_real_venv_and_runs_unittest(python_project, tmp_path):
    model = ScriptedModel(replies=[
        call("inspect_dev_environment", path="api"),
        call("prepare_dev_environment", path="api", language="python"),
        call("run_dev_task", path="api", language="python", task="unittest"),
        "Project environment prepared and 1 unit test succeeded.",
    ])
    with AgentApplication(config(python_project, tmp_path), model=model) as app:
        result = app.run("Prepare and test the api project")
    assert result["status"] == "completed", f"error={result.get('error')!r}; trace={result.get('trace')!r}"
    assert (python_project / "api" / ".venv" / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )).is_file()
    assert (python_project / "api" / "requirements.txt").is_file()
    assert any(x["event"] == "tool" and x["name"] == "run_dev_task" and x["status"] == "success"
               for x in result["trace"])


def test_node_scripts_use_manifest_and_real_process_runner(node_project, monkeypatch):
    seen = []
    def fake_runner(argv, cwd, **kwargs):
        seen.append((argv, cwd))
        return ProcessResult(returncode=0, stdout=b"completed\n", stderr=b"")
    monkeypatch.setattr(dev_environment, "run_process", fake_runner)
    envtools = {x.name: x for x in build_dev_environment_tools(node_project)}
    installed = envtools["prepare_dev_environment"].invoke({"path": "frontend", "language": "node"})
    ran = envtools["run_dev_task"].invoke({"path": "frontend", "language": "node", "task": "build"})
    assert "exit_code=0" in installed and "exit_code=0" in ran
    assert seen[0][0] == ["npm", "ci", "--no-audit", "--no-fund"]
    assert seen[1][0] == ["npm", "run", "build"]
    assert seen[0][1] == node_project / "frontend"


def test_custom_script_rejects_undeclared_name_and_path_escape(node_project, tmp_path):
    model = ScriptedModel(replies=[
        call("run_dev_task", path="frontend", language="node", task="script", script="unknown"),
        "The requested script is not declared.",
    ])
    with AgentApplication(config(node_project, tmp_path), model=model) as app:
        result = app.run("Try a missing npm script")
    assert result["status"] == "incomplete"
    with pytest.raises(PermissionError):
        dev_environment._project(node_project, "../outside")


def test_multi_language_auto_requires_explicit_choice(tmp_path):
    root = tmp_path / "mixed"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='mix'\nversion='0.1'\n", encoding="utf-8")
    (root / "package.json").write_text('{"scripts":{"test":"node test.js"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="混合语言"):
        dev_environment._manager(root, "auto")
    assert dev_environment._manager(root, "python") == "python"
    assert dev_environment._manager(root, "node") == "npm"


def test_node_pnpm_lockfile_selects_pnpm(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "package.json").write_text('{"scripts":{"build":"vite build"}}', encoding="utf-8")
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    commands = []
    def fake_process(argv, cwd, **kwargs):
        commands.append(argv)
        return ProcessResult(returncode=0, stdout=b"ok", stderr=b"")
    monkeypatch.setattr(dev_environment, "run_process", fake_process)
    tool = next(x for x in build_dev_environment_tools(root) if x.name == "prepare_dev_environment")
    tool.invoke({"language": "node"})
    assert commands == [["pnpm", "install", "--frozen-lockfile"]]


def test_failed_project_test_is_reported_to_agent_as_error(node_project, tmp_path, monkeypatch):
    def failed(argv, cwd, **kwargs):
        return ProcessResult(returncode=1, stdout=b"", stderr=b"test failed\n")
    monkeypatch.setattr(dev_environment, "run_process", failed)
    model = ScriptedModel(replies=[
        call("run_dev_task", path="frontend", language="node", task="test"),
        "Failed test requires repair.",
    ])
    with AgentApplication(config(node_project, tmp_path), model=model) as app:
        result = app.run("Run test")
    assert result["status"] == "incomplete"
    assert any(e["event"] == "tool" and e["name"] == "run_dev_task" and e["status"] == "error"
               for e in result["trace"])


def test_dependency_setup_requires_approval(node_project, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(dev_environment, "run_process", lambda *a, **k: called.append(a))
    model = ScriptedModel(replies=[call("prepare_dev_environment", path="frontend", language="node")])
    with AgentApplication(config(node_project, tmp_path, approval="ASK"), model=model) as app:
        result = app.run("Install frontend dependencies")
    assert result["status"] == "interrupted"
    assert not called


def test_node_install_failure_stays_a_failed_tool(node_project, tmp_path, monkeypatch):
    def failed(argv, cwd, **kwargs):
        return ProcessResult(returncode=5, stdout=b"", stderr=b"registry unavailable")
    monkeypatch.setattr(dev_environment, "run_process", failed)
    model = ScriptedModel(replies=[
        call("prepare_dev_environment", path="frontend", language="node"),
        "Dependency install failed.",
    ])
    with AgentApplication(config(node_project, tmp_path), model=model) as app:
        result = app.run("Prepare dependencies")
    assert result["status"] == "incomplete"


def test_agent_python_interpreter_is_not_used_for_project_scripts(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "requirements.txt").write_text("", encoding="utf-8")
    (root / "runner.py").write_text("print('hello')\n", encoding="utf-8")
    env = dev_environment._python_exe(root)
    env.parent.mkdir(parents=True)
    env.write_bytes(b"fake local interpreter")
    recorded = []
    def fake_process(argv, cwd, **kwargs):
        recorded.append(argv)
        return ProcessResult(returncode=0, stdout=b"ok", stderr=b"")
    monkeypatch.setattr(dev_environment, "run_process", fake_process)
    tool = next(x for x in build_dev_environment_tools(root) if x.name == "run_dev_task")
    tool.invoke({"task": "script", "language": "python", "script": "runner.py"})
    assert recorded[0][0] == str(env)
    assert recorded[0][1] == str(root / "runner.py")


def test_targeted_unittest_executes_selected_file_without_full_discovery(python_project, tmp_path):
    project = python_project / "api"
    (project / "test_unrelated_failure.py").write_text(
        "import unittest\nclass Unrelated(unittest.TestCase):\n"
        "    def test_fails(self):\n        self.fail('unrelated suite failed')\n",
        encoding="utf-8",
    )
    model = ScriptedModel(replies=[
        call("prepare_dev_environment", path="api", language="python"),
        call("run_dev_task", path="api", language="python", task="unittest",
             test_files=["test_calculator.py"]),
        "The selected unittest passed.",
    ])
    with AgentApplication(config(python_project, tmp_path), model=model) as app:
        result = app.run("Run only the requested calculator unittest")
    assert result["status"] == "completed", result
    assert any(x["event"] == "tool" and x["name"] == "run_dev_task"
               and x["status"] == "success" for x in result["trace"])


def test_targeted_pytest_uses_explicit_arg_separator(node_project, tmp_path, monkeypatch):
    root = tmp_path / "pyroot"
    root.mkdir()
    (root / "requirements.txt").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    interpreter = dev_environment._python_exe(root)
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"fake project-local python")
    seen = []
    def record(argv, cwd, **kwargs):
        seen.append((argv, cwd))
        return ProcessResult(returncode=0, stdout=b"1 passed", stderr=b"")
    monkeypatch.setattr(dev_environment, "run_process", record)
    tool = next(x for x in build_dev_environment_tools(root) if x.name == "run_dev_task")
    tool.invoke({"language": "python", "task": "test",
                 "test_files": ["tests/test_ok.py"]})
    assert seen == [([str(interpreter), "-m", "pytest", "-q", "--", "tests/test_ok.py"], root)]
    tool.invoke({"language": "python", "task": "unittest"})
    assert seen[1][0] == [str(interpreter), "-m", "unittest", "discover"]


def test_targeted_tests_fail_closed_for_invalid_paths(python_project):
    project = python_project / "api"
    (project / "notes.txt").write_text("not a test", encoding="utf-8")
    pick = dev_environment._test_file_targets
    assert pick(project, ["test_calculator.py"]) == ["test_calculator.py"]
    for names in [
        ["test_calculator.py", "test_calculator.py"],
        ["no_such_test.py"],
        ["notes.txt"],
        ["../test_calculator.py"],
        ["test_calculator.py"] * 17,
        [""],
    ]:
        with pytest.raises((ValueError, PermissionError)):
            pick(project, names)
    try:
        (project / "test_link.py").symlink_to(project / "test_calculator.py")
    except (OSError, NotImplementedError):
        # Windows runners may not permit symlink creation.
        return
    with pytest.raises(PermissionError):
        pick(project, ["test_link.py"])


def test_targeted_tests_rejected_for_build_and_node_without_running(
    python_project, node_project, tmp_path, monkeypatch,
):
    called = []
    monkeypatch.setattr(dev_environment, "run_process", lambda *a, **k: called.append(a))
    py_tool = next(x for x in build_dev_environment_tools(python_project) if x.name == "run_dev_task")
    with pytest.raises(ValueError, match="test_files"):
        py_tool.invoke({"path": "api", "language": "python", "task": "build",
                        "test_files": ["test_calculator.py"]})
    node_tool = next(x for x in build_dev_environment_tools(node_project) if x.name == "run_dev_task")
    with pytest.raises(ValueError, match="test_files"):
        node_tool.invoke({"path": "frontend", "language": "node", "task": "test",
                          "test_files": ["test_calculator.py"]})
    assert called == []
