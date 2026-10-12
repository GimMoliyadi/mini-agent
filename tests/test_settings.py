import math
from pathlib import Path

import pytest

from mini_agent.settings import Settings


def test_relative_mcp_config_is_bound_to_resolved_state_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    workspace.mkdir()
    state_dir.mkdir()
    (state_dir / ".env").write_text(
        "OPENAI_API_KEY=test-key\nOPENAI_BASE_URL=http://127.0.0.1:8000/v1\n"
        "OPENAI_MODEL=test-model\nMINI_AGENT_MCP_CONFIG=config/mcp.json\n",
        encoding="utf-8",
    )

    settings = Settings.load(workspace, state_dir)

    assert settings.mcp_config_path == state_dir / "config" / "mcp.json"


@pytest.mark.parametrize("field", ["request_timeout", "rag_timeout", "mcp_timeout"])
def test_non_finite_timeout_is_rejected(tmp_path: Path, field: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="有限的正数"):
        Settings(workspace=workspace, state_dir=tmp_path / "state", **{field: math.nan})

