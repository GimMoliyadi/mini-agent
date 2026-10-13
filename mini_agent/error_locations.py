"""Extract actionable in-project error locations from common developer logs.

These hints are intentionally lightweight: stderr/stdout stay the source of
truth, and a matched location is not proof of the root cause.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .sandbox import redact_text

_MAX_LINES = 4000
_MAX_LOCATIONS = 12
_IGNORED = frozenset({".venv", "venv", "node_modules", "__pycache__", ".git"})

# Python traceback: File "src/thing.py", line 23, in caller
_TRACEBACK = re.compile(r'\bFile\s+"(?P<path>[^"]+)",\s+line\s+(?P<line>\d+)')
# TypeScript compiler: src/index.ts(12,5): error TS2322: ...
_TSC = re.compile(
    r'(?P<path>(?:[A-Za-z]:)?[^\s():]+?\.(?:ts|tsx|js|jsx))'
    r'\((?P<line>\d+),(?P<column>\d+)\):\s*(?P<message>.*)'
)
# pytest / Ruff / Node / Vite: src/index.ts:12:5: error ...
_COLON = re.compile(
    r'(?P<path>(?:[A-Za-z]:)?[^\s():]+?\.(?:py|ts|tsx|js|jsx|mjs|cjs))'
    r':(?P<line>\d+)(?::(?P<column>\d+))?(?::\s*(?P<message>.*))?'
)


def _local_path(raw: str, project: Path) -> str | None:
    # A Windows drive on POSIX is not a project-local path.
    if re.match(r"^[A-Za-z]:", raw) and not project.drive:
        return None
    try:
        target = Path(raw.replace("\\", "/"))
        resolved = (target if target.is_absolute() else project / target).resolve()
        relative = resolved.relative_to(project.resolve())
    except (OSError, ValueError):
        return None
    if not relative.parts or any(part in _IGNORED for part in relative.parts):
        return None
    return relative.as_posix()


def extract_failure_locations(
    stdout: str, stderr: str, project: Path,
) -> list[dict[str, Any]]:
    """Return up to 12 distinct code locations from Python and JS/TS failures."""
    locations: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int | None]] = set()
    for index, line in enumerate((stderr + "\n" + stdout).splitlines()):
        if index >= _MAX_LINES or len(locations) >= _MAX_LOCATIONS:
            break
        for pattern, kind in ((_TRACEBACK, "traceback"), (_TSC, "typescript"), (_COLON, "source")):
            match = pattern.search(line)
            if not match:
                continue
            path = _local_path(match.group("path"), project)
            if not path:
                continue
            lineno = int(match.group("line"))
            column_text = match.groupdict().get("column")
            column = int(column_text) if column_text else None
            if not 1 <= lineno <= 10000000 or column is not None and not 1 <= column <= 100000:
                continue
            key = (path, lineno, column)
            if key in seen:
                break
            seen.add(key)
            raw_message = (match.groupdict().get("message") or line.strip())[:220]
            entry: dict[str, Any] = {
                "file": path, "line": lineno, "kind": kind,
                "message": redact_text(raw_message),
            }
            if column is not None:
                entry["column"] = column
            locations.append(entry)
            break
    return locations


def error_location_summary(stdout: str, stderr: str, project: Path) -> tuple[str, list[dict[str, Any]]]:
    """Make both model-readable text and structured artifact metadata."""
    locations = extract_failure_locations(stdout, stderr, project)
    if not locations:
        return "", []
    message = json.dumps(locations, ensure_ascii=False)
    return "\nERROR_LOCATIONS (hints only): " + message, locations
