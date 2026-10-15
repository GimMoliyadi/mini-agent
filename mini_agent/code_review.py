"""Bounded automatic working-tree review for the existing coding agent.

Perform deterministic syntax/style regression checks and provide a sanitized
patch review packet for the *existing* LangGraph model to assess semantics.
No second reviewer model, no code execution and no changed files.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from langchain.tools import tool

from . import git_workflow
from . import sandbox
from .config import MAX_TOOL_RESULT_CHARS

_MAX_CHANGED = 60
_MAX_CODE_BYTES = 128 * 1024
_MAX_DIFF_CHARS = 8000
_MAX_FINDINGS = 45
_TEST_INDICATORS = ("/test_", "/tests/", "_test.py", ".test.ts", ".spec.ts", ".test.tsx", ".spec.tsx")


def _changes(root: Path) -> list[str]:
    git_workflow._repository_root(root)
    tracked = git_workflow._git(root, [
        "diff", "HEAD", "--name-only", "-z", "--no-renames", "--",
    ]).split(b"\0")
    untracked = git_workflow._git(root, [
        "ls-files", "--others", "--exclude-standard", "-z", "--",
    ]).split(b"\0")
    names = sorted({
        item.decode("utf-8") for item in tracked + untracked if item
    })
    return names


def review_code_changes(*, workspace: Path, paths: list[str] | None = None) -> str:
    """Review current modified and new files with AST/JSON and patch checks."""
    root = workspace.resolve()
    all_names = _changes(root)
    if paths is not None:
        if not isinstance(paths, list) or not 1 <= len(paths) <= _MAX_CHANGED:
            raise ValueError(f"代码审查每批需要 1–{_MAX_CHANGED} 个已修改文件")
        if any(not isinstance(item, str) or item not in all_names for item in paths):
            raise ValueError("代码审查只能选择当前 Git 工作区实际改动的文件")
        if len(set(paths)) != len(paths):
            raise ValueError("代码审查文件不可重复")
        names = sorted(paths)
    else:
        names = all_names
    checked: list[str] = []
    findings: list[dict[str, Any]] = []
    truncated = len(names) > _MAX_CHANGED
    code_changed = False
    tests_changed = False

    def note(path: str, line: int, level: str, code: str, message: str) -> None:
        if len(findings) < _MAX_FINDINGS:
            findings.append({
                "file": path, "line": line, "severity": level,
                "code": code, "message": message,
            })

    for relative in names[:_MAX_CHANGED]:
        try:
            path = sandbox.resolve_path(relative, root)
        except (OSError, ValueError):
            continue
        if not path.exists() or not path.is_file():
            continue
        if any(relative.endswith(ext) for ext in (".py", ".js", ".jsx", ".ts", ".tsx")):
            code_changed = True
        if relative.startswith(("tests/", "test/")) or any(
            mark in "/" + relative for mark in _TEST_INDICATORS
        ):
            tests_changed = True
        if path.suffix.lower() not in {".py", ".json", ".js", ".ts", ".tsx", ".jsx"}:
            continue
        try:
            data = sandbox.read_bounded_bytes(path, _MAX_CODE_BYTES)
            contents = data.decode("utf-8-sig")
        except (OSError, ValueError, UnicodeDecodeError):
            truncated = True
            continue
        checked.append(relative)
        if path.suffix == ".py":
            try:
                parsed = ast.parse(contents, filename=relative)
            except SyntaxError as exc:
                note(relative, exc.lineno or 1, "error", "PY_SYNTAX", exc.msg)
                continue
            for node in ast.walk(parsed):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    note(relative, node.lineno, "warning", "BARE_EXCEPT",
                         "Bare except may hide unexpected failures")
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for default in (*node.args.defaults, *node.args.kw_defaults):
                        if isinstance(default, (ast.List, ast.Set, ast.Dict)):
                            note(relative, node.lineno, "warning", "MUTABLE_DEFAULT",
                                 f"{node.name} uses a mutable default argument")
        elif path.suffix == ".json":
            try:
                json.loads(contents)
            except json.JSONDecodeError as exc:
                note(relative, exc.lineno, "error", "INVALID_JSON", exc.msg)

    if code_changed and not tests_changed:
        note(".", 0, "info", "TEST_GAP",
             "Code changed but no test file is in this worktree diff; review coverage")

    raw = git_workflow._git(root, [
        "diff", "HEAD", "--no-ext-diff", "--no-textconv", "--unified=3", "--",
        *names[:_MAX_CHANGED],
    ]).decode("utf-8", errors="replace")
    # Changes to untracked files aren't in git diff HEAD; AST checks still cover them.
    raw = sandbox.redact_text(raw)
    if len(raw) > _MAX_DIFF_CHARS:
        raw = raw[:_MAX_DIFF_CHARS] + "\n[patch truncated]"
        truncated = True
    result: dict[str, Any] = {
        "all_changed_files": all_names,
        "changed_files": names[:_MAX_CHANGED],
        "files_inspected": checked, "findings": findings,
        "diff_excerpt": raw,
        "truncated": truncated or len(findings) >= _MAX_FINDINGS,
        "review_instruction": "Inspect findings and diff_excerpt for likely bugs, unintended "
                              "behavior changes and missing tests. All automated findings are "
                              "heuristics, not proof of defects. Existing test commands must run.",
    }
    encoded = json.dumps(result, ensure_ascii=False)
    while len(encoded) > MAX_TOOL_RESULT_CHARS and raw:
        raw = raw[: max(0, len(raw) - 1000)]
        result["diff_excerpt"] = raw + "\n[review diff truncated]"
        result["truncated"] = True
        encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded) > MAX_TOOL_RESULT_CHARS:
        result["diff_excerpt"] = ""
        result["truncated"] = True
        encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded) > MAX_TOOL_RESULT_CHARS:
        raise ValueError("代码审查摘要过大，请拆分更小的变更")
    return sandbox.redact_text(encoded)


def build_code_review_tools(workspace: Path) -> list[Any]:
    root = workspace.resolve()

    @tool
    def review_code_changes_tool(paths: list[str] | None = None) -> str:
        """Review current Git working-tree changes for syntax, JSON and risky patterns.

        Return changed paths, Python syntax errors, mutable defaults, bare
        except, JSON parse errors, missing test changes and bounded git diff.
        Supply paths to inspect a smaller batch of currently changed files when
        the all-file diff is truncated. Returned all_changed_files is the full
        coverage target; every changed file must be reviewed before finish.
        Inspect returned patch to perform semantic review using the existing
        coding model; this does not claim to replace a human code review.
        """
        return review_code_changes(workspace=root, paths=paths)

    review_code_changes_tool.name = "review_code_changes"
    review_code_changes_tool.metadata = {"risk": "read", "mutation": False}
    return [review_code_changes_tool]
