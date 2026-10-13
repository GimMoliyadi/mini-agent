"""Mock GitHub networking while exercising real local Git and LangGraph boundaries."""

import json
import subprocess

import pytest

from helpers import ScriptedModel, call
from mini_agent import github_delivery
from mini_agent.github_delivery import build_github_tools
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Local Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "sample.py").write_text("print('ready')\n", encoding="utf-8")
    git(root, "add", "sample.py")
    git(root, "commit", "-qm", "feat: initial files")
    git(root, "checkout", "-qb", "feat/approved-feature")
    git(root, "remote", "add", "origin", "https://github.com/example/repo.git")
    return root


def config(root, tmp_path, mode="ALLOW", remote="example/repo"):
    return Settings(
        workspace=root, state_dir=tmp_path / "state",
        approval_mode=mode, github_repo=remote,
        github_token="local-test-placeholder-token",
    )


def test_remote_delivery_requires_explicit_configuration(repository, tmp_path):
    assert build_github_tools(Settings(workspace=repository, state_dir=tmp_path / "disabled")) == []
    assert build_github_tools(config(repository, tmp_path, remote="")) == []
    assert "local-test-placeholder-token" not in repr(config(repository, tmp_path))


def test_push_create_draft_pr_and_read_checks(repository, tmp_path, monkeypatch):
    operations = []
    head = git(repository, "rev-parse", "HEAD")

    def push(root, token, branch):
        operations.append(("push", branch))
        assert root == repository and token == "local-test-placeholder-token"

    def gh(root, token, argv):
        operations.append(tuple(argv))
        assert token == "local-test-placeholder-token"
        if argv[0] == "api":
            return json.dumps({"object": {"sha": head}})
        if argv[:2] == ["pr", "list"]:
            return "[]"
        if argv[:2] == ["pr", "create"]:
            return "https://github.com/example/repo/pull/4\n"
        if argv[:2] == ["pr", "view"]:
            return json.dumps({
                "number": 4, "url": "https://github.com/example/repo/pull/4",
                "state": "OPEN", "headRefName": "feat/approved-feature",
                "baseRefName": "develop",
                "statusCheckRollup": [
                    {"name": "unit-tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
                ],
            })
        raise AssertionError(argv)

    monkeypatch.setattr(github_delivery, "_push", push)
    monkeypatch.setattr(github_delivery, "_gh", gh)
    model = ScriptedModel(replies=[
        call("github_push_branch"),
        call("github_create_draft_pr", title="feat: published with approval",
             body="Reviewed feature, test results and deployment constraints.", base_branch="develop"),
        call("github_pr_status"),
        "Push and draft PR completed.",
    ])
    with AgentApplication(config(repository, tmp_path), model=model) as app:
        result = app.run("Deliver the completed feature")
    assert result["status"] == "completed", result
    assert operations[0] == ("push", "feat/approved-feature")
    assert any(a[:2] == ("pr", "create") for a in operations if len(a) >= 2)
    assert any(a[:2] == ("pr", "view") for a in operations if len(a) >= 2)
    assert "local-test-placeholder-token" not in json.dumps(result)


def test_origin_mismatch_and_default_branch_never_push(repository, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(github_delivery, "_push", lambda *args: sent.append(args))
    git(repository, "remote", "set-url", "origin", "https://github.com/other/repo.git")
    with AgentApplication(config(repository, tmp_path), model=ScriptedModel(replies=[
        call("github_push_branch"), "Wrong origin",
    ])) as app:
        result = app.run("Try pushing")
    assert result["status"] == "incomplete"
    assert not sent
    git(repository, "remote", "set-url", "origin", "https://github.com/example/repo.git")
    git(repository, "checkout", "-qb", "main")
    with AgentApplication(config(repository, tmp_path), model=ScriptedModel(replies=[
        call("github_push_branch"), "Default branch",
    ])) as app:
        result = app.run("Try pushing")
    assert result["status"] == "incomplete"
    assert not sent


def test_dirty_worktree_blocks_push(repository, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(github_delivery, "_push", lambda *args: sent.append(args))
    (repository / "sample.py").write_text("print('dirty')\n", encoding="utf-8")
    with AgentApplication(config(repository, tmp_path), model=ScriptedModel(replies=[
        call("github_push_branch"), "Changes must be committed first.",
    ])) as app:
        result = app.run("Push changes")
    assert result["status"] == "incomplete"
    assert not sent


def test_rejected_approval_does_not_push(repository, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(github_delivery, "_push", lambda *args: sent.append(args))
    with AgentApplication(config(repository, tmp_path, mode="ASK"), model=ScriptedModel(replies=[
        call("github_push_branch"), "Approval was denied.",
    ])) as app:
        paused = app.run("Publish approved project")
        assert paused["status"] == "interrupted"
        assert not sent
        resumed = app.run(None, thread_id=paused["session_id"], decisions=[{"type": "reject"}])
        assert resumed["status"] != "completed"
    assert not sent


def test_pr_requires_remote_head_to_match(repository, tmp_path, monkeypatch):
    calls = []
    def gh(root, token, argv):
        calls.append(argv)
        return json.dumps({"object": {"sha": "0" * 40}})
    monkeypatch.setattr(github_delivery, "_gh", gh)
    with AgentApplication(config(repository, tmp_path), model=ScriptedModel(replies=[
        call("github_create_draft_pr", title="feat: reviewed work",
             body="Implementation and unit tests completed.", base_branch="develop"),
        "Remote branch is not pushed.",
    ])) as app:
        result = app.run("Create PR")
    assert result["status"] == "incomplete"
    assert len(calls) == 1 and calls[0][0] == "api"


def test_existing_open_pr_is_idempotent(repository, tmp_path, monkeypatch):
    calls = []
    head = git(repository, "rev-parse", "HEAD")
    def gh(root, token, argv):
        calls.append(argv)
        if argv[0] == "api":
            return json.dumps({"object": {"sha": head}})
        return json.dumps([{
            "url": "https://github.com/example/repo/pull/9", "number": 9,
            "baseRefName": "develop",
        }])
    monkeypatch.setattr(github_delivery, "_gh", gh)
    with AgentApplication(config(repository, tmp_path), model=ScriptedModel(replies=[
        call("github_create_draft_pr", title="feat: reviewed work",
             body="The development work is complete.", base_branch="develop"),
        "Existing PR reused.",
    ])) as app:
        result = app.run("Create PR")
    assert result["status"] == "completed", result
    assert len(calls) == 2


def test_remote_write_tools_must_not_share_tool_batch(repository, tmp_path):
    first = call("github_push_branch")
    first.tool_calls.append({
        "name": "github_create_draft_pr",
        "args": {"title": "feat: add feature", "body": "This is a tested feature", "base_branch": "develop"},
        "id": "unsafe-parallel", "type": "tool_call",
    })
    with AgentApplication(config(repository, tmp_path), model=ScriptedModel(replies=[first])) as app:
        result = app.run("Push and open PR in parallel")
    assert result["status"] == "failed"
    assert "单独" in result["error"]
