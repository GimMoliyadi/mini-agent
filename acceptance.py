"""Deterministic acceptance for structured coding-task contracts.

The verifier is deliberately independent from the LLM loop and session state:
it compares workspace snapshots, validates and runs the contract's fixed test
command, then returns an explainable acceptance result.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

import tools


_GENERATED_FILE_SUFFIXES = {".pyc", ".pyo"}


def _normalise_relative_path(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")
    normalised = value.replace("\\", "/").strip()
    while normalised.startswith("./"):
        normalised = normalised[2:]
    path = Path(normalised)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} 必须是工作目录内的相对路径：{value!r}")
    return Path(*path.parts).as_posix()


@dataclass(frozen=True)
class TestCommand:
    """The fixed command a verifier is allowed to run."""

    command: str
    args: tuple[str, ...] = ()
    cwd: str = "."

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TestCommand":
        if not isinstance(value, dict):
            raise ValueError("test_command 必须是对象")
        command = value.get("command")
        args = value.get("args", [])
        cwd = value.get("cwd", ".")
        if not isinstance(command, str) or not command:
            raise ValueError("test_command.command 必须是非空字符串")
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise ValueError("test_command.args 必须是字符串数组")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("test_command.cwd 必须是非空字符串")

        command_value = cls(command, tuple(args), cwd)
        tools.validate_run_command_arguments(
            {
                "command": command_value.command,
                "args": list(command_value.args),
                "cwd": command_value.cwd,
            }
        )
        return command_value

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "args": list(self.args),
            "cwd": self.cwd,
        }


@dataclass(frozen=True)
class CodingTaskContract:
    """Machine-readable facts used to judge one coding task."""

    task_id: str
    instruction: str
    allowed_paths: tuple[str, ...]
    test_command: TestCommand
    require_test_pass: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CodingTaskContract":
        if not isinstance(value, dict):
            raise ValueError("contract 必须是对象")

        task_id = value.get("task_id")
        instruction = value.get("instruction")
        allowed_paths = value.get("allowed_paths")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id 必须是非空字符串")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction 必须是非空字符串")
        if not isinstance(allowed_paths, list):
            raise ValueError("allowed_paths 必须是字符串数组")

        normalised_paths = tuple(
            _normalise_relative_path(path, "allowed_paths") for path in allowed_paths
        )
        if len(set(normalised_paths)) != len(normalised_paths):
            raise ValueError("allowed_paths 不能包含重复路径")

        require_test_pass = value.get("require_test_pass", True)
        if not isinstance(require_test_pass, bool):
            raise ValueError("require_test_pass 必须是布尔值")

        return cls(
            task_id=task_id.strip(),
            instruction=instruction,
            allowed_paths=normalised_paths,
            test_command=TestCommand.from_dict(value.get("test_command")),
            require_test_pass=require_test_pass,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "instruction": self.instruction,
            "allowed_paths": list(self.allowed_paths),
            "test_command": self.test_command.as_dict(),
            "require_test_pass": self.require_test_pass,
        }


def load_contract(path: str | Path) -> CodingTaskContract:
    """Load and validate a JSON contract file."""
    contract_path = Path(path)
    with contract_path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return CodingTaskContract.from_dict(value)


def _is_generated_artifact(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix.casefold() in _GENERATED_FILE_SUFFIXES


def snapshot_workspace(workspace: str | Path) -> dict[str, str]:
    """Return relative-file-to-SHA256 state for a workspace.

    Python bytecode is a test-runtime artifact rather than an agent source
    change, so it is excluded. Ordinary files, including new and deleted files,
    remain visible to the diff.
    """
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"workspace 不是目录：{workspace}")

    snapshot = {}
    for path in root.rglob("*"):
        if not path.is_file() or _is_generated_artifact(path):
            continue
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Return added, deleted, or content-modified files in stable order."""
    return sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def _parse_exit_code(result: str) -> int | None:
    for line in result.splitlines():
        if not line.startswith("Exit code: "):
            continue
        value = line.removeprefix("Exit code: ").strip()
        try:
            return int(value)
        except ValueError:
            return None
    return None


def verify_contract(
    contract: CodingTaskContract,
    workspace: str | Path,
    initial_snapshot: dict[str, str],
    *,
    agent_final_answer_present: bool,
    agent_ran_required_test: bool,
    max_steps_reached: bool = False,
    runtime_exception: str | None = None,
) -> dict[str, Any]:
    """Verify the final workspace without calling an LLM or reading a session."""
    root = Path(workspace).resolve()
    final_snapshot = snapshot_workspace(root)
    changed = changed_files(initial_snapshot, final_snapshot)
    unexpected = [path for path in changed if path not in contract.allowed_paths]

    final_test_exit_code: int | None = None
    final_test_passed = False
    test_error: str | None = None
    try:
        test_result = tools.run_command(
            contract.test_command.command,
            list(contract.test_command.args),
            contract.test_command.cwd,
            workspace=root,
        )
        final_test_exit_code = _parse_exit_code(test_result)
        final_test_passed = final_test_exit_code == 0
    except (OSError, TypeError, ValueError) as exc:
        test_error = f"{type(exc).__name__}: {exc}"

    reasons = []
    if not agent_final_answer_present:
        reasons.append("agent_final_answer_missing")
    if max_steps_reached:
        reasons.append("max_agent_steps_reached")
    if runtime_exception:
        reasons.append(f"runtime_exception: {runtime_exception}")
    reasons.extend(f"unexpected file changed: {path}" for path in unexpected)
    if contract.require_test_pass and not final_test_passed:
        reasons.append("final_test_failed")
    if test_error:
        reasons.append(f"final_test_error: {test_error}")

    return {
        "task_id": contract.task_id,
        "accepted": not reasons,
        "changed_files": changed,
        "unexpected_changes": unexpected,
        "agent_ran_required_test": agent_ran_required_test,
        "final_test_exit_code": final_test_exit_code,
        "final_test_passed": final_test_passed,
        "agent_final_answer_present": agent_final_answer_present,
        "max_steps_reached": max_steps_reached,
        "runtime_exception": runtime_exception,
        "reasons": reasons,
    }
