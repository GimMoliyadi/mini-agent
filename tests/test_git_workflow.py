"""End-to-end Git delivery tests against temporary real Git repositories."""

import subprocess

import pytest

from helpers import ScriptedModel, call
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings
from mini_agent.verifier import CodingTaskContract, TestCommand as RequiredCommand


def _git(root, *args):
    completed = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Local Test")
    _git(path, "config", "user.email", "local@example.invalid")
    (path / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (path / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\n"
        "class Tests(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n", encoding="utf-8",
    )
    (path / "unrelated.txt").write_text("unchanged\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "initial commit")
    return path


def _settings(repo, tmp_path, approval="ALLOW"):
    return Settings(workspace=repo, state_dir=tmp_path / "state", approval_mode=approval)


def test_code_fix_then_verified_local_git_commit(repo, tmp_path):
    settings = _settings(repo, tmp_path)
    contract = CodingTaskContract(
        "fix-add", "Fix addition and commit", ("calculator.py",),
        RequiredCommand("python", ("-m", "unittest", "test_calculator", "-q")),
    )
    old_head = _git(repo, "rev-parse", "HEAD")
    model = ScriptedModel(replies=[
        call("git_worktree_review"),
        call("apply_patch", path="calculator.py", old_text="a - b", new_text="a + b"),
        call("run_command", command="python", args=["-m", "unittest", "test_calculator", "-q"]),
        call("git_commit_files", paths=["calculator.py"], message="fix: correct addition"),
        "Fixed addition, tested and committed locally.",
    ])
    with AgentApplication(settings, model=model) as app:
        result = app.run(contract.instruction, contract=contract)
    assert result["status"] == "completed", result
    assert result["verification"]["accepted"]
    new_head = _git(repo, "rev-parse", "HEAD")
    assert new_head != old_head
    assert _git(repo, "show", "--format=", "--name-only", "HEAD") == "calculator.py"
    assert _git(repo, "status", "--porcelain") == ""
    assert "fix: correct addition" in _git(repo, "log", "-1", "--format=%s")


def test_preexisting_staged_work_is_never_committed(repo, tmp_path):
    (repo / "unrelated.txt").write_text("user staged change\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    old_head = _git(repo, "rev-parse", "HEAD")
    with AgentApplication(_settings(repo, tmp_path), model=ScriptedModel(replies=[
        call("git_commit_files", paths=["calculator.py"], message="fix: add checks"),
        "Cannot commit due to staged user changes.",
    ])) as app:
        result = app.run("Try local commit")
    assert result["status"] == "incomplete", result
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert _git(repo, "diff", "--cached", "--name-only") == "unrelated.txt"


def test_contract_requires_fresh_tests_before_git_commit(repo, tmp_path):
    contract = CodingTaskContract(
        "no-tests", "Fix and commit", ("calculator.py",),
        RequiredCommand("python", ("-m", "unittest", "test_calculator", "-q")),
    )
    head = _git(repo, "rev-parse", "HEAD")
    with AgentApplication(_settings(repo, tmp_path), model=ScriptedModel(replies=[
        call("apply_patch", path="calculator.py", old_text="a - b", new_text="a + b"),
        call("git_commit_files", paths=["calculator.py"], message="fix: correct addition"),
        "I cannot commit without passing tests.",
    ])) as app:
        result = app.run(contract.instruction, contract=contract)
    assert result["status"] != "completed", result
    assert _git(repo, "rev-parse", "HEAD") == head


def test_approval_interruption_before_git_commit(repo, tmp_path):
    old_head = _git(repo, "rev-parse", "HEAD")
    (repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    settings = _settings(repo, tmp_path, approval="ASK")
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("git_commit_files", paths=["calculator.py"], message="fix: correct addition"),
        "I will not commit after approval was rejected.",
    ])) as app:
        paused = app.run("Commit calculator fix")
        assert paused["status"] == "interrupted", paused
        assert _git(repo, "rev-parse", "HEAD") == old_head
        denied = app.run(None, thread_id=paused["session_id"], decisions=[{"type": "reject"}])
        assert denied["status"] != "completed", denied
    assert _git(repo, "rev-parse", "HEAD") == old_head


def test_git_commit_rejects_path_escape_and_unrelated_contract_changes(repo, tmp_path):
    head = _git(repo, "rev-parse", "HEAD")
    with AgentApplication(_settings(repo, tmp_path), model=ScriptedModel(replies=[
        call("git_commit_files", paths=["../escape.txt"], message="feat: do not escape"),
        "Rejected unsafe path.",
    ])) as app:
        result = app.run("Try to commit outside workspace")
    assert result["status"] == "incomplete", result
    assert _git(repo, "rev-parse", "HEAD") == head


def test_repo_must_match_workspace_root(repo, tmp_path):
    (repo / "nested").mkdir()
    with AgentApplication(_settings(repo / "nested", tmp_path), model=ScriptedModel(replies=[
        call("git_worktree_review"), "Not a separate repository.",
    ])) as app:
        result = app.run("Read repo")
    assert result["status"] == "incomplete", result
