# ruff: noqa: F401
"""Compatibility facade for the canonical mini_agent.tools package.

The registry, schemas, and handlers live under mini_agent.tools. This module
keeps the historical import tools API and its test monkeypatch points.
"""

# TOOL_REGISTRY remains the canonical registry object re-exported below.

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import file_safety
from config import (
    COMMAND_TIMEOUT_SECONDS,
    MAX_COMMAND_OUTPUT_CHARS,
    MAX_READ_RESULT_CHARS,
    MAX_TOOL_RESULT_CHARS,
    PROJECT_ROOT,
    WORKSPACE_DIR,
)
from process_runner import MAX_COMMAND_OUTPUT_BYTES, ProcessResult, run_process

from mini_agent.tools import command as _command
from mini_agent.tools import filesystem as _filesystem
from mini_agent.tools import introspection as _introspection
from mini_agent.tools import registry as _registry
from mini_agent.tools.command import (
    CommandPolicyError,
    _ALLOWED_GIT_COMMANDS,
    _ALLOWED_PYTHON_MODULES,
    _COMMAND_SHELL_SYNTAX,
    _decode_process_output,
    _format_command_result,
    _limit_command_output,
    _redact_process_output,
    _reject_shell_syntax,
    _reject_workspace_escape_tokens,
    run_command,
    validate_run_command_arguments,
)
from mini_agent.tools.filesystem import (
    MAX_DIRECTORY_ENTRIES,
    MAX_SEARCH_ENTRIES,
    MAX_SEARCH_FILES,
    MAX_SEARCH_SCAN_BYTES,
    _SEARCH_CONTEXT_LINES,
    _SEARCH_IGNORED_DIRS,
    _SEARCH_IGNORED_PATHS,
    _SEARCH_MAX_RESULTS,
    _SEARCH_SNIPPET_CHARS,
    _format_read_result,
    _format_search_result,
    _iter_search_files,
    _read_file_range,
    _render_search_matches,
    _search_file_lines,
    _search_match_context,
    _search_path_is_ignored,
    _shorten_search_line,
    _validate_positive_line_argument,
    apply_patch,
    list_files,
    read_file,
    rename_file,
    resolve_inside_workspace,
    search_text,
    write_file,
)
from mini_agent.tools.introspection import (
    _PROJECT_INSPECTION_FILES,
    finish_task,
    inspect_capabilities,
    inspect_project,
)
from mini_agent.tools.registry import (
    AVAILABLE_TOOLS,
    APPLY_PATCH_TOOL,
    FINISH_TASK_TOOL,
    INSPECT_CAPABILITIES_TOOL,
    INSPECT_PROJECT_TOOL,
    LIST_FILES_TOOL,
    READ_FILE_TOOL,
    RENAME_FILE_TOOL,
    RUN_COMMAND_TOOL,
    SEARCH_TEXT_TOOL,
    TOOL_REGISTRY,
    ToolDefinition,
    ToolKind,
    RiskLevel,
    WRITE_FILE_TOOL,
)

# The package registry is the one source of truth. These aliases intentionally
# point at the same mutable objects used by main.py and legacy tests.
