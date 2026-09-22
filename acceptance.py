"""Deterministic acceptance for structured coding-task contracts.

The verifier is deliberately independent from the LLM loop and session state:
it compares workspace snapshots, validates and runs the contract's fixed test
command, then returns an explainable acceptance result.
"""

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any

import tools


_GENERATED_FILE_SUFFIXES = {".pyc", ".pyo"}
_GENERATED_DIRECTORY_NAMES = {"__pycache__", ".pytest_cache"}


class TaskStatus(str, Enum):
    """The small, persisted lifecycle for one Coding Task."""

    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    LIMIT_REACHED = "LIMIT_REACHED"
    ERROR = "ERROR"


@dataclass
class TaskState:
    """Deterministic state used by the Coding Task finish protocol.

    ``event_seq`` is a task-local logical clock.  It intentionally avoids
    wall-clock timestamps so a successful required test is fresh exactly when
    its event sequence is newer than the last workspace mutation.
    """

    status: TaskStatus = TaskStatus.RUNNING
    event_seq: int = 0
    last_mutation_event_seq: int | None = None
    last_successful_exact_required_test_seq: int | None = None
    finish_message: str | None = None
    last_finish_rejection: dict[str, Any] | None = None
    unresolved_runtime_error: str | None = None
    finish_attempts: list[dict[str, Any]] = field(default_factory=list)
    initial_snapshot: dict[str, str] = field(default_factory=dict)

    def next_event(self) -> int:
        self.event_seq += 1
        return self.event_seq

    def record_finish_attempt(
        self, summary: str, accepted: bool, reasons: list[str]
    ) -> None:
        attempt = {
            "event_seq": self.event_seq,
            "summary": summary,
            "status": "FINISHED" if accepted else "REJECTED",
            "reasons": list(reasons),
        }
        self.finish_attempts.append(attempt)
        if accepted:
            self.status = TaskStatus.FINISHED
            self.finish_message = summary
        else:
            self.last_finish_rejection = attempt

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "event_seq": self.event_seq,
            "last_mutation_event_seq": self.last_mutation_event_seq,
            "last_successful_exact_required_test_seq": (
                self.last_successful_exact_required_test_seq
            ),
            "finish_message": self.finish_message,
            "last_finish_rejection": self.last_finish_rejection,
            "unresolved_runtime_error": self.unresolved_runtime_error,
            "finish_attempts": list(self.finish_attempts),
            "initial_snapshot": dict(self.initial_snapshot),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskState":
        if not isinstance(value, dict):
            raise ValueError("task_state 必须是对象")

        try:
            status = TaskStatus(value.get("status", TaskStatus.RUNNING.value))
        except ValueError as exc:
            raise ValueError("task_state.status 不受支持") from exc

        def optional_sequence(name: str) -> int | None:
            sequence = value.get(name)
            if sequence is None:
                return None
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise ValueError(f"task_state.{name} 必须是非负整数或 null")
            return sequence

        event_seq = value.get("event_seq", 0)
        if isinstance(event_seq, bool) or not isinstance(event_seq, int) or event_seq < 0:
            raise ValueError("task_state.event_seq 必须是非负整数")

        mutation_seq = optional_sequence("last_mutation_event_seq")
        test_seq = optional_sequence("last_successful_exact_required_test_seq")
        if any(sequence is not None and sequence > event_seq for sequence in (mutation_seq, test_seq)):
            raise ValueError("task_state 事件序号不能超过 event_seq")

        snapshot = value.get("initial_snapshot", {})
        if not isinstance(snapshot, dict) or any(
            not isinstance(path, str) or not isinstance(digest, str)
            for path, digest in snapshot.items()
        ):
            raise ValueError("task_state.initial_snapshot 必须是字符串映射")

        finish_message = value.get("finish_message")
        runtime_error = value.get("unresolved_runtime_error")
        if finish_message is not None and not isinstance(finish_message, str):
            raise ValueError("task_state.finish_message 必须是字符串或 null")
        if runtime_error is not None and not isinstance(runtime_error, str):
            raise ValueError("task_state.unresolved_runtime_error 必须是字符串或 null")

        rejection = value.get("last_finish_rejection")
        if rejection is not None and not isinstance(rejection, dict):
            raise ValueError("task_state.last_finish_rejection 必须是对象或 null")
        attempts = value.get("finish_attempts", [])
        if not isinstance(attempts, list) or any(not isinstance(item, dict) for item in attempts):
            raise ValueError("task_state.finish_attempts 必须是对象数组")

        return cls(
            status=status,
            event_seq=event_seq,
            last_mutation_event_seq=mutation_seq,
            last_successful_exact_required_test_seq=test_seq,
            finish_message=finish_message,
            last_finish_rejection=dict(rejection) if rejection is not None else None,
            unresolved_runtime_error=runtime_error,
            finish_attempts=[dict(item) for item in attempts],
            initial_snapshot=dict(snapshot),
        )


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


def coding_task_guidance(contract: CodingTaskContract) -> str:
    """Return short model guidance derived from the contract, not its verifier."""
    test = " ".join((contract.test_command.command, *contract.test_command.args))
    allowed = ", ".join(contract.allowed_paths) or "（无文件修改）"
    return (
        "Coding Task 收口规则：目标文件是 "
        f"{allowed}；必需测试命令是 `{test}`（cwd={contract.test_command.cwd}）。"
        "当代码改动已完成、该测试成功、没有新错误且没有未满足要求时，"
        "不要重复读取、写入或测试；调用 finish_task(summary=...) 请求完成，"
        "并在 summary 中简要说明改动和测试结果。普通 Final Answer 不会完成 Coding Task。"
    )


def load_contract(path: str | Path) -> CodingTaskContract:
    """Load and validate a JSON contract file."""
    contract_path = Path(path)
    with contract_path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return CodingTaskContract.from_dict(value)


def _is_generated_artifact(path: Path) -> bool:
    return (
        any(part in _GENERATED_DIRECTORY_NAMES for part in path.parts)
        or path.suffix.casefold() in _GENERATED_FILE_SUFFIXES
    )


def snapshot_workspace(workspace: str | Path) -> dict[str, str]:
    """Return relative-file-to-SHA256 state for a workspace.

    Bytecode and test-runner caches are runtime artifacts rather than agent
    source changes, so they are excluded. The ignore list stays deliberately
    short and directory-anchored: any file outside it, including new and
    deleted files, remains visible to the diff.
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


@dataclass(frozen=True)
class FinishGateResult:
    """A deterministic decision for one ``finish_task`` request."""

    accepted: bool
    reasons: tuple[str, ...]
    changed_files: tuple[str, ...] = ()
    unexpected_changes: tuple[str, ...] = ()
    required_test: TestCommand | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "FINISHED" if self.accepted else "REJECTED",
            "reasons": list(self.reasons),
        }
        if not self.accepted and self.required_test is not None:
            result["required_test"] = self.required_test.as_dict()
        return result


def evaluate_finish_request(
    contract: CodingTaskContract | None,
    task_state: TaskState,
    workspace: str | Path,
) -> FinishGateResult:
    """Allow FINISHED only when the current structured Coding Task facts pass.

    This gate only reads current state and snapshots.  It never invokes a
    model, changes files, or runs a test command.
    """
    if contract is None:
        return FinishGateResult(False, ("coding_contract_missing",))

    current_snapshot = snapshot_workspace(workspace)
    changed = changed_files(task_state.initial_snapshot, current_snapshot)
    unexpected = tuple(path for path in changed if path not in contract.allowed_paths)
    reasons: list[str] = []

    if unexpected:
        reasons.extend(f"unexpected_change:{path}" for path in unexpected)

    test_seq = task_state.last_successful_exact_required_test_seq
    if contract.require_test_pass and test_seq is None:
        reasons.append("successful_exact_required_test_missing")

    mutation_seq = task_state.last_mutation_event_seq
    if (
        contract.require_test_pass
        and mutation_seq is not None
        and (test_seq is None or test_seq <= mutation_seq)
    ):
        reasons.append("successful_exact_required_test_stale")

    if task_state.unresolved_runtime_error:
        reasons.append("unresolved_runtime_error")

    return FinishGateResult(
        not reasons,
        tuple(reasons),
        tuple(changed),
        unexpected,
        contract.test_command,
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
    task_state: TaskState | None = None,
    agent_final_answer_present: bool = False,
    agent_ran_required_test: bool = False,
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

    artifact_passed = not unexpected and (
        not contract.require_test_pass or final_test_passed
    ) and test_error is None

    # Only a run that actually drove the finish protocol can be judged on
    # finish_task. Callers without a TaskState never enforced it, so they keep
    # the plain Final Answer rule instead of being blamed for skipping a tool
    # they were never told to call.
    reasons: list[str] = []
    if task_state is not None:
        interaction_completed = task_state.status is TaskStatus.FINISHED
        if not interaction_completed:
            reasons.append("finish_task_not_accepted")
    else:
        interaction_completed = agent_final_answer_present and not max_steps_reached
        if not agent_final_answer_present:
            reasons.append("agent_final_answer_missing")

    agent_self_verified = (
        task_state is not None
        and task_state.last_successful_exact_required_test_seq is not None
        and (
            task_state.last_mutation_event_seq is None
            or task_state.last_successful_exact_required_test_seq
            > task_state.last_mutation_event_seq
        )
    )

    if max_steps_reached and not interaction_completed:
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
        "artifact_passed": artifact_passed,
        "interaction_completed": interaction_completed,
        "accepted": artifact_passed and interaction_completed and not runtime_exception,
        "changed_files": changed,
        "unexpected_changes": unexpected,
        "agent_ran_required_test": agent_ran_required_test,
        "agent_self_verified": agent_self_verified,
        "final_test_exit_code": final_test_exit_code,
        "final_test_passed": final_test_passed,
        "agent_final_answer_present": agent_final_answer_present,
        "task_status": task_state.status.value if task_state is not None else None,
        "max_steps_reached": max_steps_reached,
        "runtime_exception": runtime_exception,
        "reasons": reasons,
    }
