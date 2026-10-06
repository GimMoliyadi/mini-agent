"""Deterministic acceptance for structured coding-task contracts.

The verifier is deliberately independent from the LLM loop and session state:
it runs the contract's test only through an explicitly supplied runner, then
compares workspace snapshots and returns an explainable acceptance result.
"""

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable

import tools
from .result import ToolResult


_GENERATED_FILE_SUFFIXES = {".pyc", ".pyo"}
_PYTEST_CACHE_FILES = {
    (".gitignore",),
    ("README.md",),
    ("CACHEDIR.TAG",),
    ("v", "cache", "nodeids"),
    ("v", "cache", "lastfailed"),
    ("v", "cache", "stepwise"),
}
_TEST_STATUSES = {"PASS", "FAIL", "UNKNOWN"}
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_UNITTEST_COUNT = re.compile(r"^Ran (\d+) tests? in .+$", re.MULTILINE)
_UNITTEST_OUTCOME = re.compile(r"^(OK|FAILED)(?: \(([^\n]*)\))?$", re.MULTILINE)
_PYTEST_SUMMARY = re.compile(
    r"^(?:=+\s*)?((?:\d+ (?:passed|failed|skipped|xfailed|xpassed|errors?|deselected|warnings?)(?:, )?)+)"
    r" in \d+(?:\.\d+)?(?:s| seconds?)(?:\s*=+)?$",
    re.MULTILINE,
)
_PYTEST_COUNTS = re.compile(r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?|deselected|warnings?)")


class TaskStatus(str, Enum):
    """The small, persisted lifecycle for one Coding Task."""

    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    LIMIT_REACHED = "LIMIT_REACHED"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


@dataclass
class TaskState:
    """Persisted finish facts with a test-time snapshot and logical clock."""

    status: TaskStatus = TaskStatus.RUNNING
    event_seq: int = 0
    last_mutation_event_seq: int | None = None
    last_successful_exact_required_test_seq: int | None = None
    finish_message: str | None = None
    last_finish_rejection: dict[str, Any] | None = None
    unresolved_runtime_error: str | None = None
    finish_attempts: list[dict[str, Any]] = field(default_factory=list)
    initial_snapshot: dict[str, str] = field(default_factory=dict)
    verified_snapshot: dict[str, str] | None = None
    last_test_status: str | None = None
    last_test_count: int | None = None

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
            "verified_snapshot": (
                dict(self.verified_snapshot) if self.verified_snapshot is not None else None
            ),
            "last_test_status": self.last_test_status,
            "last_test_count": self.last_test_count,
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
            **_restore_test_state(value),
        )


def _restore_test_state(value: dict[str, Any]) -> dict[str, Any]:
    snapshot = value.get("verified_snapshot")
    if snapshot is not None and (
        not isinstance(snapshot, dict)
        or any(not isinstance(path, str) or not isinstance(digest, str)
               for path, digest in snapshot.items())
    ):
        raise ValueError("task_state.verified_snapshot 必须是字符串映射或 null")
    status = value.get("last_test_status")
    if status is not None and (not isinstance(status, str) or status not in _TEST_STATUSES):
        raise ValueError("task_state.last_test_status 必须是 PASS、FAIL、UNKNOWN 或 null")
    count = value.get("last_test_count")
    if count is not None and (
        isinstance(count, bool) or not isinstance(count, int) or count < 0
    ):
        raise ValueError("task_state.last_test_count 必须是非负整数或 null")
    return {
        "verified_snapshot": dict(snapshot) if snapshot is not None else None,
        "last_test_status": status,
        "last_test_count": count,
    }


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
    def from_dict(cls, value: Any) -> "TestCommand":
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
    if path.parent.name == "__pycache__":
        return path.suffix.casefold() in _GENERATED_FILE_SUFFIXES
    for index, part in enumerate(path.parts[:-1]):
        if part == ".pytest_cache":
            return path.parts[index + 1:] in _PYTEST_CACHE_FILES
    return False


MAX_SNAPSHOT_ENTRIES = 20_000
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
SNAPSHOT_CHUNK_BYTES = 64 * 1024


def _snapshot_paths(root: Path):
    from runtime_guards import BudgetExceeded, get_current_budget

    pending = [root]
    entries_seen = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                budget = get_current_budget()
                if budget is not None:
                    budget.check()
                entries_seen += 1
                if entries_seen > MAX_SNAPSHOT_ENTRIES:
                    raise BudgetExceeded("工作区目录条目超过快照上限，请缩小工作区。")
                path = Path(entry.path)
                details = path.stat(follow_symlinks=False)
                reparse = bool(getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
                if entry.is_symlink() or reparse:
                    yield path, details, True
                elif stat.S_ISDIR(details.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(details.st_mode):
                    yield path, details, False


def _snapshot_file(path: Path, expected, remaining: int) -> tuple[str, int]:
    from runtime_guards import BudgetExceeded, get_current_budget

    if expected.st_size > remaining:
        raise BudgetExceeded("工作区文件总量超过快照字节预算，请缩小工作区。")
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise OSError(f"快照期间文件身份发生变化：{path.name}")
        while True:
            budget = get_current_budget()
            if budget is not None:
                budget.check()
            chunk = handle.read(SNAPSHOT_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > remaining:
                raise BudgetExceeded("工作区快照超过字节预算。")
            digest.update(chunk)
        final = os.fstat(handle.fileno())
        if (final.st_size, final.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise OSError(f"快照期间文件内容发生变化：{path.name}")
    return digest.hexdigest(), size


def snapshot_workspace(workspace: str | Path) -> dict[str, str]:
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"workspace 不是目录：{workspace}")
    snapshot = {}
    remaining = MAX_SNAPSHOT_BYTES
    for path, details, is_link in _snapshot_paths(root):
        relative_path = path.relative_to(root)
        if _is_generated_artifact(relative_path):
            continue
        if is_link:
            encoded = ("link:" + os.readlink(path)).encode("utf-8", errors="surrogatepass")
            digest = hashlib.sha256(encoded).hexdigest()
        elif details.st_nlink > 1:
            raise PermissionError(f"编码验收不接受共享硬链接，请先使用独立副本：{relative_path.as_posix()}")
        else:
            digest, consumed = _snapshot_file(path, details, remaining)
            remaining -= consumed
        snapshot[relative_path.as_posix()] = digest
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


def _required_test_rejections(state: TaskState, snapshot: dict[str, str]) -> list[str]:
    reasons: list[str] = []
    test_seq = state.last_successful_exact_required_test_seq
    if state.last_test_status == "UNKNOWN":
        reasons.append("required_test_result_unknown")
    if (
        test_seq is None or state.last_test_status != "PASS"
        or state.last_test_count is None or state.last_test_count <= 0
    ):
        reasons.append("successful_exact_required_test_missing")
    if state.verified_snapshot is None:
        reasons.append("successful_exact_required_test_snapshot_missing")
    if (
        state.last_mutation_event_seq is not None
        and (test_seq is None or test_seq <= state.last_mutation_event_seq)
    ) or (state.verified_snapshot is not None and state.verified_snapshot != snapshot):
        reasons.append("successful_exact_required_test_stale")
    return reasons


def evaluate_finish_request(
    contract: CodingTaskContract | None,
    task_state: TaskState,
    workspace: str | Path,
) -> FinishGateResult:
    """Read current finish facts without executing commands or changing files."""
    if contract is None:
        return FinishGateResult(False, ("coding_contract_missing",))

    current_snapshot = snapshot_workspace(workspace)
    changed = changed_files(task_state.initial_snapshot, current_snapshot)
    unexpected = tuple(path for path in changed if path not in contract.allowed_paths)
    reasons = [f"unexpected_change:{path}" for path in unexpected]
    if contract.require_test_pass:
        reasons.extend(_required_test_rejections(task_state, current_snapshot))
    if task_state.status is TaskStatus.CANCELLED:
        reasons.append("task_cancelled")
    if task_state.unresolved_runtime_error:
        reasons.append("unresolved_runtime_error")
    return FinishGateResult(
        not reasons, tuple(reasons), tuple(changed), unexpected, contract.test_command
    )


def _parse_exit_code(result: str) -> int | None:
    header = result.split("STDOUT:", 1)[0]
    values = re.findall(r"^Exit code: (-?\d+)\s*$", header, re.MULTILINE)
    return int(values[0]) if len(values) == 1 else None


def _unittest_summary(output: str) -> dict[str, Any] | None:
    counts = list(_UNITTEST_COUNT.finditer(output))
    outcomes = list(_UNITTEST_OUTCOME.finditer(output))
    # Python 3.13 reports an empty suite separately and exits nonzero.
    if counts and int(counts[-1].group(1)) == 0:
        tail = output[counts[-1].end():]
        if re.search(r"^NO TESTS RAN\s*$", tail, re.MULTILINE):
            return {"test_count": 0, "collected_count": 0,
                    "skipped_count": 0, "summary_passed": False}
    if not counts or not outcomes or outcomes[-1].start() < counts[-1].end():
        return None
    collected = int(counts[-1].group(1))
    details = outcomes[-1].group(2) or ""
    skip_match = re.search(r"\bskipped=(\d+)\b", details)
    skipped = int(skip_match.group(1)) if skip_match else 0
    if skipped > collected:
        return None
    return {
        "test_count": collected - skipped,
        "collected_count": collected,
        "skipped_count": skipped,
        "summary_passed": outcomes[-1].group(1) == "OK",
    }


def _pytest_summary(output: str) -> dict[str, Any] | None:
    summaries = list(_PYTEST_SUMMARY.finditer(output))
    if not summaries:
        if re.search(r"^(?:=+\s*)?no tests ran in .+?(?:\s*=+)?$", output, re.MULTILINE):
            return {"test_count": 0, "collected_count": 0,
                    "skipped_count": 0, "summary_passed": True}
        return None
    counts = {name: int(count) for count, name in _PYTEST_COUNTS.findall(summaries[-1].group(1))}
    executed = sum(counts.get(name, 0) for name in ("passed", "failed", "xfailed", "xpassed", "error", "errors"))
    skipped = counts.get("skipped", 0)
    return {
        "test_count": executed,
        "collected_count": executed + skipped,
        "skipped_count": skipped,
        "summary_passed": not any(counts.get(name, 0) for name in ("failed", "error", "errors")),
    }


def parse_test_result(result: str) -> dict[str, Any]:
    """Return status, exit_code and executed/collected/skipped test counts."""
    output = _ANSI_ESCAPE.sub("", result).replace("\r\n", "\n").replace("\r", "\n")
    exit_code = result.metadata.get("returncode") if isinstance(result, ToolResult) else _parse_exit_code(output)
    summary = _unittest_summary(output) or _pytest_summary(output)
    parsed = {
        "status": "UNKNOWN",
        "exit_code": exit_code,
        "test_count": summary["test_count"] if summary else None,
        "collected_count": summary["collected_count"] if summary else None,
        "skipped_count": summary["skipped_count"] if summary else None,
        "reason": None,
    }
    header = output.split("STDOUT:", 1)[0]
    timed_out = result.metadata.get("timed_out", False) if isinstance(result, ToolResult) else "Timed out: true" in header.splitlines()
    limited = result.metadata.get("output_limit_exceeded", False) if isinstance(result, ToolResult) else "Output limit exceeded: true" in header.splitlines()
    if timed_out:
        parsed["reason"] = "test_command_timed_out"
    elif limited:
        parsed["reason"] = "test_output_limit_exceeded"
    elif exit_code is None:
        parsed["reason"] = "test_exit_code_missing"
    elif summary is not None and summary["test_count"] == 0:
        parsed["reason"] = "all_tests_skipped" if summary["skipped_count"] else "no_tests_executed"
    elif exit_code != 0 or (summary is not None and not summary["summary_passed"]):
        parsed.update(status="FAIL", reason="test_command_failed")
    elif summary is None:
        parsed["reason"] = "test_summary_missing"
    else:
        parsed["status"] = "PASS"
    return parsed


def record_required_test(
    state: TaskState,
    result: str,
    workspace: str | Path,
    event_seq: int,
) -> None:
    if isinstance(event_seq, bool) or not isinstance(event_seq, int) or not 0 <= event_seq <= state.event_seq:
        raise ValueError("测试事件序号必须是非负整数且不能超过 task_state.event_seq")
    parsed = parse_test_result(result)
    state.last_test_status = parsed["status"]
    state.last_test_count = parsed["test_count"]
    state.last_successful_exact_required_test_seq = None
    state.verified_snapshot = None
    if parsed["status"] == "PASS":
        state.last_test_status = "UNKNOWN"
        state.verified_snapshot = snapshot_workspace(workspace)
        state.last_successful_exact_required_test_seq = event_seq
        state.last_test_status = "PASS"


def _required_test_is_fresh(state: TaskState, snapshot: dict[str, str]) -> bool:
    test_seq = state.last_successful_exact_required_test_seq
    mutation_seq = state.last_mutation_event_seq
    return (
        state.last_test_status == "PASS"
        and state.last_test_count is not None
        and state.last_test_count > 0
        and test_seq is not None
        and (mutation_seq is None or test_seq > mutation_seq)
        and state.verified_snapshot is not None
        and state.verified_snapshot == snapshot
    )


def _verification_blocker(
    state: TaskState | None,
    runtime_exception: str | None,
    command_runner: Callable[..., str] | None,
) -> str | None:
    if state is not None and state.status is TaskStatus.CANCELLED:
        return "task_cancelled"
    if runtime_exception:
        return "runtime_exception"
    if state is not None and (state.unresolved_runtime_error or state.status is TaskStatus.ERROR):
        return "unresolved_runtime_error"
    if command_runner is None:
        return "command_runner_missing"
    return None


def _run_final_test(
    contract: CodingTaskContract,
    root: Path,
    command_runner: Callable[..., str] | None,
    blocker: str | None,
) -> dict[str, Any]:
    if blocker:
        return {"status": "UNKNOWN", "exit_code": None, "test_count": None,
                "collected_count": None, "skipped_count": None, "reason": blocker, "error": None}
    assert command_runner is not None
    try:
        result = command_runner(
            contract.test_command.command,
            list(contract.test_command.args),
            contract.test_command.cwd,
            workspace=root,
        )
        return {**parse_test_result(result), "error": None}
    except Exception as exc:
        from runtime_guards import BudgetExceeded
        if isinstance(exc, BudgetExceeded):
            raise
        return {"status": "UNKNOWN", "exit_code": None, "test_count": None,
                "collected_count": None, "skipped_count": None,
                "reason": "test_execution_error", "error": f"{type(exc).__name__}: {exc}"}


def _interaction_verdict(
    state: TaskState | None, final_answer: bool, max_steps: bool
) -> tuple[bool, list[str]]:
    if state is not None:
        completed = state.status is TaskStatus.FINISHED
        reasons = [] if completed else ["finish_task_not_accepted"]
    else:
        completed = final_answer and not max_steps
        reasons = [] if final_answer else ["agent_final_answer_missing"]
    if max_steps and not completed:
        reasons.append("max_agent_steps_reached")
    return completed, reasons


def _final_test_reasons(test: dict[str, Any], required: bool) -> list[str]:
    reasons: list[str] = []
    if required and test["status"] == "FAIL":
        reasons.append("final_test_failed")
    if test["status"] == "UNKNOWN":
        reasons.append(f"final_test_not_verified: {test['reason']}")
    if test["error"]:
        reasons.append(f"final_test_error: {test['error']}")
    return reasons


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
    command_runner: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Verify with an explicitly authorized runner(command, args, cwd, *, workspace)."""
    root = Path(workspace).resolve()
    blocker = _verification_blocker(task_state, runtime_exception, command_runner)
    final_test = _run_final_test(contract, root, command_runner, blocker)
    final_snapshot = snapshot_workspace(root)
    changed = changed_files(initial_snapshot, final_snapshot)
    unexpected = [path for path in changed if path not in contract.allowed_paths]
    final_test_passed = final_test["status"] == "PASS"
    aborted = blocker is not None and blocker != "command_runner_missing"
    artifact_passed = (
        not unexpected
        and (not contract.require_test_pass or final_test_passed)
        and final_test["error"] is None
        and not aborted
    )
    interaction_completed, reasons = _interaction_verdict(
        task_state, agent_final_answer_present, max_steps_reached
    )
    if runtime_exception:
        reasons.append(f"runtime_exception: {runtime_exception}")
    reasons.extend(f"unexpected file changed: {path}" for path in unexpected)
    reasons.extend(_final_test_reasons(final_test, contract.require_test_pass))
    return {
        "task_id": contract.task_id,
        "artifact_passed": artifact_passed,
        "interaction_completed": interaction_completed,
        "accepted": artifact_passed and interaction_completed and not runtime_exception,
        "changed_files": changed,
        "unexpected_changes": unexpected,
        "agent_ran_required_test": agent_ran_required_test,
        "agent_self_verified": (
            task_state is not None and _required_test_is_fresh(task_state, final_snapshot)
        ),
        "final_test_exit_code": final_test["exit_code"],
        "final_test_passed": final_test_passed,
        "final_test_status": final_test["status"],
        "final_test_count": final_test["test_count"],
        "final_test_collected_count": final_test["collected_count"],
        "final_test_skipped_count": final_test["skipped_count"],
        "final_test_unverified_reason": final_test["reason"] if final_test["status"] == "UNKNOWN" else None,
        "agent_final_answer_present": agent_final_answer_present,
        "task_status": task_state.status.value if task_state is not None else None,
        "max_steps_reached": max_steps_reached,
        "runtime_exception": runtime_exception,
        "reasons": reasons,
    }
