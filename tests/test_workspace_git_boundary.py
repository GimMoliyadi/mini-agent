"""Workflow warnings must obey the same Git root boundary as code review."""

import subprocess

import pytest

from helpers import ScriptedModel, call
from mini_agent import git_workflow
from mini_agent.code_review import review_code_changes
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


def _system_after_start(workspace, tmp_path):
    model = ScriptedModel(replies=[
        call(
            "start_development_workflow",
            goal="Fix order checkout calculation",
            profile="general",
        ),
        "The workflow is not finished.",
    ])
    settings = Settings(
        workspace=workspace,
        state_dir=tmp_path / "state",
        approval_mode="ALLOW",
    )
    with AgentApplication(settings, model=model) as app:
        app.run("Fix order checkout calculation using a development workflow")
    assert len(model.observed) >= 2
    return "\n".join(
        str(message.content)
        for message in model.observed[1]
        if message.type == "system"
    )


def _init_git(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)


def test_git_ancestor_does_not_enable_review(tmp_path):
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    _init_git(ancestor)
    workspace = ancestor / "plain_project"
    workspace.mkdir()

    guidance = _system_after_start(workspace, tmp_path)
    assert "不是 Git 仓库根目录" in guidance
    assert "review_code_changes" in guidance
    assert "不要自动执行 git init" in guidance

    with pytest.raises(PermissionError, match="必须与当前受信 workspace 完全一致"):
        review_code_changes(workspace=workspace)


def test_exact_git_root_does_not_warn(tmp_path):
    workspace = tmp_path / "actual_project"
    workspace.mkdir()
    _init_git(workspace)

    guidance = _system_after_start(workspace, tmp_path)
    assert "当前启用开发工作流" in guidance
    assert "不是 Git 仓库根目录" not in guidance


def test_git_unavailable_reports_review_blocker(tmp_path, monkeypatch):
    workspace = tmp_path / "plain_project"
    workspace.mkdir()

    def missing_git(_root):
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(git_workflow, "_repository_root", missing_git)
    guidance = _system_after_start(workspace, tmp_path)
    assert "不是 Git 仓库根目录" in guidance
    assert "不能声称审查通过" in guidance
