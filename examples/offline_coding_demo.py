"""A one-minute, provider-free coding-loop demonstration.

The demo uses the existing scripted Agent Loop and independent verifier on a
temporary Phase 22 fixture. It intentionally makes the first coupon fix too
weak, observes the failing test, repairs the implementation, and finishes only
after a fresh passing test.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from eval.phase22_fixtures import (  # noqa: E402
    build_fixture,
    ground_truth_patch,
)
from eval.phase22_harness import run_scripted_case  # noqa: E402


def reply(call_id: str, tool: str, arguments: dict) -> main.ModelReply:
    call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=tool,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )
    return main.ModelReply(
        SimpleNamespace(content=None, tool_calls=[call]),
        "tool_calls",
        None,
        None,
        None,
    )


def run_demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="mini-agent-demo-") as directory:
        workspace = Path(directory) / "workspace"
        build_fixture(workspace)
        path = "src/pricing/coupons.py"
        correct = ground_truth_patch("first_fix_insufficient")[path]
        insufficient = correct.replace(
            "rate = percent / 100 if percent > 1 else percent",
            "rate = percent / 100",
        )
        test = {
            "command": "python",
            "args": [
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_coupons.py",
                "-q",
            ],
            "cwd": ".",
        }
        trace_path = ROOT / "examples" / "example_trace.jsonl"
        trace_path.unlink(missing_ok=True)
        trace_type = main.CodingTaskTrace
        created_trace = None

        def trace_factory():
            nonlocal created_trace
            created_trace = trace_type(jsonl_path=trace_path)
            return created_trace

        with patch.object(main, "CodingTaskTrace", side_effect=trace_factory):
            result = run_scripted_case(
                workspace,
                "first_fix_insufficient",
                [
                    reply("search", "search_text", {"path": ".", "query": "discounted_amount"}),
                    reply("read", "read_file", {"path": path}),
                    reply("weak", "write_file", {"path": path, "content": insufficient}),
                    reply("test-1", "run_command", test),
                    reply("repair", "write_file", {"path": path, "content": correct}),
                    reply("test-2", "run_command", test),
                    reply("finish", "finish_task", {"summary": "Repaired the percentage boundary and verified the test."}),
                ],
            )
        if created_trace is not None and hasattr(created_trace, "record_verifier"):
            created_trace.record_verifier(result["acceptance"])
        metrics = result["metrics"]
        trace = result["trace"]
        return {
            "workflow": [
                event.get("tool")
                for event in trace.get("events", [])
                if event.get("action") == "tool_call"
            ],
            "first_test_failed": metrics.get("required_test_failures_before_success") == 1,
            "recovery": metrics.get("required_test_attempts") == 2,
            "finish_gate": trace.get("finish_successes") == 1,
            "verifier_accepted": result["acceptance"].get("accepted"),
            "changed_files": result["acceptance"].get("changed_files"),
            "note": "Scripted replies only; no provider or API key was used.",
            "trace": str(trace_path.relative_to(ROOT)),
        }


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
