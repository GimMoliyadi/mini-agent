"""Constrained local Git review and commit tools for trusted coding workspaces.

No shell, no network/push, no branch switching. The caller explicitly chooses
files; unrelated staged changes and repos outside the workspace are rejected.
This is a developer convenience, not an operating-system sandbox.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from langchain.tools import ToolRuntime, tool

from .process_runner import MAX_COMMAND_OUTPUT_BYTES, run_process
from .result import ToolResult
from .sandbox import redact_text, resolve_path
from .state import TaskState
from .verification import verification_facts
from .verifier import CodingTaskContract, evaluate_finish_request

_MAX_COMMIT_FILES = 30
_MAX_MESSAGE = 160
_MAX_SUMMARY = 6000


def _git(root: Path, args: list[str]) -> bytes:
    """Run a bounded process in the fixed root with isolated Git config."""
    result = run_process(
        ["git", "--no-pager", "-c", "core.fsmonitor=false", *args],
        root,
        workspace=root,
        timeout_seconds=45,
        output_limit_bytes=MAX_COMMAND_OUTPUT_BYTES,
    )
    if result.timed_out or result.output_limit_exceeded or result.returncode != 0:
        reason = ("timeout" if result.timed_out else "output limit"
                  if result.output_limit_exceeded else f"exit {result.returncode}")
        detail = redact_text(result.stderr.decode("utf-8", errors="replace"))[:500]
        raise ValueError(f"Git 操作失败（{reason}）：{detail}")
    return result.stdout


def _repository_root(root: Path) -> None:
    actual = Path(_git(root, ["rev-parse", "--show-toplevel"]).decode("utf-8").strip()).resolve()
    if actual != root:
        raise PermissionError("Git 仓库根目录必须与当前受信 workspace 完全一致")
    bare = _git(root, ["rev-parse", "--is-bare-repository"]).strip()
    if bare != b"false":
        raise PermissionError("仅支持普通 Git 工作树")


def _paths(root: Path, paths: list[str]) -> list[str]:
    if not isinstance(paths, list) or not 1 <= len(paths) <= _MAX_COMMIT_FILES:
        raise ValueError(f"请明确指定 1 到 {_MAX_COMMIT_FILES} 个文件")
    selected = []
    for path in paths:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("提交路径必须是非空相对文件路径")
        value = path.replace("\\", "/")
        if value.startswith(("/", "./")) or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("提交路径只能是规范化的相对文件路径")
        target = resolve_path(value, root)
        normalized = target.relative_to(root).as_posix()
        if normalized != value:
            raise ValueError(f"提交路径存在歧义：{path}")
        if (root / value).is_symlink():
            raise PermissionError("不可通过 Git 提交符号链接")
        if target.exists() and not target.is_file():
            raise ValueError(f"只能提交文件，不能提交目录：{value}")
        if not target.exists():
            # Deletions of tracked files are legitimate, arbitrary missing paths are not.
            tracked = _git(root, ["ls-files", "--error-unmatch", "--", value])
            if not tracked:
                raise ValueError(f"文件不存在且不受 Git 跟踪：{value}")
        selected.append(normalized)
    if len(set(selected)) != len(selected):
        raise ValueError("待提交文件路径不可重复")
    return selected


def _git_names(output: bytes) -> set[str]:
    return {name.decode("utf-8", errors="strict") for name in output.split(b"\x00") if name}


def _validate_contract(root: Path, paths: list[str], runtime: ToolRuntime[Any, TaskState]) -> None:
    data = runtime.state.get("contract")
    if not data:
        return
    contract = CodingTaskContract.from_dict(data)
    if any(path not in contract.allowed_paths for path in paths):
        raise PermissionError("提交文件不在 Coding Contract.allowed_paths 内")
    facts = verification_facts(dict(runtime.state))
    result = evaluate_finish_request(contract, facts, root)
    if not result.accepted:
        raise PermissionError("指定测试尚未通过或验收证据已过期：" + ", ".join(result.reasons))
    if set(paths) != set(result.changed_files):
        raise PermissionError("提交文件必须与 Coding Contract 的实际改动集合一致")


def _readable(root: Path, args: list[str]) -> str:
    return redact_text(_git(root, args).decode("utf-8", errors="replace"))


def build_git_tools(workspace: Path) -> list[Any]:
    """Register Git operations against one fixed, trusted workspace."""
    root = Path(workspace).resolve()

    @tool
    def git_worktree_review() -> str:
        """Inspect current branch, local changed files and diff summary without editing anything."""
        _repository_root(root)
        branch = _readable(root, ["branch", "--show-current"]).strip() or "(detached)"
        status = _readable(root, ["status", "--short", "--untracked-files=normal"])
        diff = _readable(root, ["diff", "--no-ext-diff", "--no-textconv", "--stat"])
        staged = _readable(root, ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--stat"])
        content = f"branch: {branch}\nchanges:\n{status or '(clean)'}\nunstaged diff:\n{diff or '(none)'}\nstaged diff:\n{staged or '(none)'}"
        if len(content) > _MAX_SUMMARY:
            return content[:_MAX_SUMMARY] + "\n[status summary truncated]"
        return content

    @tool
    def git_commit_files(
        paths: list[str],
        message: str,
        runtime: ToolRuntime[Any, TaskState],
    ) -> str:
        """Commit only explicit, verified file paths in the trusted local Git workspace.

        No push or remote action. In Coding Contract tasks the exact required
        tests must already have passed after the last file change. Call this
        separately from all other tools so approval and checkpoint are clear.
        """
        _repository_root(root)
        selected = _paths(root, paths)
        if (not isinstance(message, str) or not 6 <= len(message.strip()) <= _MAX_MESSAGE
                or "\n" in message or "\r" in message or "\x00" in message):
            raise ValueError(f"提交信息须为 6 到 {_MAX_MESSAGE} 字符的单行文本")
        _validate_contract(root, selected, runtime)
        # Keep human-staged work untouched, and never include it in a generated commit.
        pre_staged = _git_names(_git(root, ["diff", "--cached", "--name-only", "-z"]))
        if pre_staged:
            raise PermissionError("暂存区已有用户改动，请先人工处理后再让 Agent 提交")
        # git add stages only the explicit validated files, including tracked deletions.
        _git(root, ["add", "--", *selected])
        staged = _git_names(_git(root, ["diff", "--cached", "--name-only", "-z"]))
        if staged != set(selected):
            raise ValueError("暂存变更与指定文件清单不一致；已停止提交，请检查暂存区")
        # Disabling hooks prevents repository-provided hooks from running unexpected
        # executables. No GPG signing or editor subprocess can be launched.
        with TemporaryDirectory(prefix="mini-agent-git-hooks-") as empty_hooks:
            _git(root, [
                "-c", f"core.hooksPath={empty_hooks}",
                "-c", "commit.gpgsign=false",
                "commit", "--no-verify", "-m", message.strip(),
            ])
        commit_sha = _readable(root, ["rev-parse", "HEAD"]).strip()
        # The commit is local only. Future PR/push work uses separately approved tools.
        return ToolResult(
            f"本地 Git commit 已创建：{commit_sha}\n文件：{', '.join(selected)}\n尚未推送到远程。",
            metadata={"commit": commit_sha, "files": selected, "pushed": False},
        )

    git_worktree_review.metadata = {"risk": "read", "mutation": False}
    # The Git index/ref is a side effect, but not a tracked-workspace file
    # mutation: the file journal and Coding Contract snapshots omit .git.
    git_commit_files.metadata = {"risk": "execute", "mutation": False, "git_commit": True}
    return [git_worktree_review, git_commit_files]
