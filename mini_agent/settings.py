"""Validated application settings; secrets are never included in repr."""

from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    workspace: Path
    state_dir: Path
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    model: str = ""
    approval_mode: str = "ASK"
    max_model_calls: int = 20
    context_tokens: int = 16000
    request_timeout: float = 120
    trace_path: Path | None = None
    budget_options: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", self.workspace.expanduser().resolve())
        object.__setattr__(self, "state_dir", self.state_dir.expanduser().resolve())
        if not self.workspace.is_dir():
            raise NotADirectoryError(f"工作目录不存在：{self.workspace}")
        if self.approval_mode not in {"ASK", "ALLOW", "DENY"}:
            raise ValueError("TOOL_APPROVAL_MODE 必须是 ASK、ALLOW 或 DENY")
        if self.max_model_calls < 1 or self.context_tokens < 256 or self.request_timeout <= 0:
            raise ValueError("模型轮数、上下文和请求超时必须为正数（上下文至少 256）")

    @classmethod
    def load(cls, workspace: Path, state_dir: Path, *, require_model: bool = True) -> "Settings":
        values = {**dotenv_values(state_dir / ".env", encoding="utf-8-sig"), **os.environ}
        names = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
        if require_model and any(not values.get(name) for name in names):
            raise ValueError("缺少模型配置；请先运行 .\\mini.cmd config")
        trace = values.get("MINI_AGENT_TRACE_JSONL")
        return cls(
            workspace=workspace, state_dir=state_dir,
            api_key=values.get("OPENAI_API_KEY") or "",
            base_url=values.get("OPENAI_BASE_URL") or "",
            model=values.get("OPENAI_MODEL") or "",
            approval_mode=values.get("TOOL_APPROVAL_MODE") or "ASK",
            max_model_calls=int(values.get("MINI_AGENT_MAX_MODEL_CALLS") or "20"),
            context_tokens=int(values.get("MINI_AGENT_CONTEXT_TOKENS") or "16000"),
            request_timeout=float(values.get("MINI_AGENT_REQUEST_TIMEOUT") or "120"),
            trace_path=Path(trace).expanduser().resolve() if trace else None,
            budget_options={key: str(value) for key, value in values.items()
                            if key.startswith("MINI_AGENT_") and value is not None},
        )
