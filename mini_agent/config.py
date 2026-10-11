"""Static tool limits; application configuration is loaded once by Settings."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = Path(os.environ.get("AGENT_WORKSPACE", PROJECT_ROOT / "demo_workspace")).resolve()
MAX_TOOL_RESULT_CHARS = 4000
MAX_READ_RESULT_CHARS = MAX_TOOL_RESULT_CHARS - 512
COMMAND_TIMEOUT_SECONDS = 30
MAX_COMMAND_OUTPUT_CHARS = 1500
