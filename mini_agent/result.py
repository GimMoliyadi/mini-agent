"""Structured outcomes with the existing textual tool protocol preserved.

The str subclass keeps existing integrations working; runtime decisions use the
attributes. Only old mock providers and persisted text need the legacy adapter.
"""

from typing import Any, Literal

ResultStatus = Literal[
    "success",
    "policy_rejected",
    "tool_error",
    "command_failed",
    "duplicate_blocked",
    "finish_accepted",
    "finish_rejected",
]


class ToolResult(str):
    status: ResultStatus
    code: str
    metadata: dict[str, Any]

    def __new__(
        cls,
        text: str,
        status: ResultStatus = "success",
        code: str = "ok",
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        value = super().__new__(cls, text)
        value.status = status
        value.code = code
        value.metadata = dict(metadata or {})
        return value

    @property
    def text(self) -> str:
        return str(self)

    def with_text(self, text: str) -> "ToolResult":
        return ToolResult(text, self.status, self.code, self.metadata)


def normalize_result(text: str, tool: str = "") -> ToolResult:
    """Accept historical/mock text at one boundary, never in live consumers."""
    if isinstance(text, ToolResult):
        return text
    if text.startswith("[重复调用被拦截]"):
        return ToolResult(text, "duplicate_blocked", "duplicate")
    if text.startswith("[用户拒绝执行]"):
        return ToolResult(text, "policy_rejected", "approval_denied")
    if text.startswith("[工具失败]"):
        return ToolResult(text, "tool_error", "legacy_tool_error")
    if tool == "run_command":
        header = text.split("STDOUT:", 1)[0].splitlines()
        code = next(
            (line[11:] for line in header if line.startswith("Exit code: ")), ""
        )
        returncode = int(code) if code.lstrip("-").isdigit() else None
        timed_out = "Timed out: true" in header
        limited = "Output limit exceeded: true" in header
        status: ResultStatus = (
            "success"
            if returncode == 0 and not timed_out and not limited
            else "command_failed"
        )
        return ToolResult(
            text,
            status,
            "command_succeeded" if status == "success" else "command_failed",
            {
                "returncode": returncode,
                "timed_out": timed_out,
                "output_limit_exceeded": limited,
            },
        )
    return ToolResult(text)
