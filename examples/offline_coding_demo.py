"""Run the real LangGraph/tools/verifier with scripted model replies, without a provider."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from helpers import ScriptedModel, call  # noqa: E402
from mini_agent.runtime import AgentApplication  # noqa: E402
from mini_agent.settings import Settings  # noqa: E402
from mini_agent.verifier import CodingTaskContract, TestCommand  # noqa: E402


def run_demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="mini-agent-demo-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        (workspace / "test_calculator.py").write_text(
            "import unittest\nfrom calculator import add\n"
            "class Addition(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n", encoding="utf-8",
        )
        contract = CodingTaskContract(
            "offline-demo", "Fix addition and verify", ("calculator.py",),
            TestCommand("python", ("-m", "unittest", "test_calculator", "-q")),
        )
        test = call("run_command", command="python", args=list(contract.test_command.args))
        model = ScriptedModel(replies=[
            call("read_file", path="calculator.py"), test,
            call("apply_patch", path="calculator.py", old_text="a - b", new_text="a + b"),
            test, "Fixed addition and verified its test.",
        ])
        settings = Settings(workspace=workspace, state_dir=root / "state", approval_mode="ALLOW")
        with AgentApplication(settings, model=model) as app:
            result = app.run(contract.instruction, contract=contract)
        commands = [event for event in result["trace"] if event.get("name") == "run_command"]
        return {
            "status": result["status"],
            "workflow": [event["name"] for event in result["trace"] if event["event"] == "tool"],
            "first_test_failed": commands[0]["status"] == "error",
            "repaired_test_passed": commands[-1]["status"] == "success",
            "verification": result["verification"],
            "note": "Scripted model only; real graph, files, subprocess and independent verifier.",
        }


if __name__ == "__main__":
    output = run_demo()
    print(json.dumps(output, ensure_ascii=False, indent=2))
    raise SystemExit(0 if output["status"] == "completed" else 1)
