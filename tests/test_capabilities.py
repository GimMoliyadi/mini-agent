import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import acceptance
import main
from recovery import Recovery
import tools


ROOT = Path(__file__).resolve().parents[1]


class CapabilityTests(unittest.TestCase):
    def snapshot(self, **context):
        return json.loads(tools.inspect_capabilities(runtime_context={"session_active": False, **context}))

    def test_registry_sync_and_absent_tools(self):
        self.assertIn("inspect_capabilities", tools.TOOL_REGISTRY)
        snapshot = self.snapshot()
        names = {item["name"] for item in snapshot["callable_tools"]}
        self.assertEqual(names, set(tools.TOOL_REGISTRY))
        self.assertFalse({"rename_file", "move_file"} & names)
        for item in snapshot["callable_tools"]:
            definition = tools.TOOL_REGISTRY[item["name"]]
            self.assertEqual(item["risk_level"], definition.risk_level.value)
            self.assertEqual(item["tool_kind"], definition.tool_kind.value)

        temporary = tools.ToolDefinition(
            name="temporary_probe",
            schema={"function": {"description": "temporary test tool"}},
            handler=lambda: "ok",
            risk_level=tools.RiskLevel.READ_ONLY,
        )
        with patch.dict(tools.TOOL_REGISTRY, {"temporary_probe": temporary}):
            self.assertIn("temporary_probe", {
                item["name"] for item in self.snapshot()["callable_tools"]
            })

    def test_availability_and_runtime_features(self):
        without = {item["name"]: item for item in self.snapshot()["callable_tools"]}
        self.assertFalse(without["finish_task"]["currently_available"])
        state = acceptance.TaskState(initial_snapshot={})
        contract = object()
        with_contract = {item["name"]: item for item in self.snapshot(
            contract=contract, task_state=state)["callable_tools"]}
        self.assertTrue(with_contract["finish_task"]["currently_available"])
        features = self.snapshot()["runtime_features"]
        self.assertTrue(features["Session Persistence / Resume"]["supported"])
        self.assertFalse(features["Session Persistence / Resume"]["active"])
        self.assertTrue(features["stage-aware recovery"]["supported"])
        self.assertFalse(features["stage-aware recovery"]["active"])
        self.assertTrue(self.snapshot(session_active=True)["runtime_features"]["Session Persistence / Resume"]["active"])
        configured = self.snapshot(contract=contract, task_state=state,
                                   recovery=Recovery(), verifier_enabled=True)["runtime_features"]
        self.assertTrue(configured["Independent Verifier"]["active"])
        self.assertEqual(configured["stage-aware recovery"]["current_state"], "REPAIR_NEEDED")

    def test_run_command_policy_and_secret_exclusion(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "phase25-canary-secret", "TOOL_APPROVAL_MODE": "ALLOW"}):
            snapshot = self.snapshot()
        encoded = json.dumps(snapshot)
        self.assertNotIn("phase25-canary-secret", encoded)
        self.assertNotIn("OPENAI_API_KEY", encoded)
        command = next(item for item in snapshot["callable_tools"] if item["name"] == "run_command")
        self.assertIn("controlled, no shell", command["command_policy"]["execution"])
        self.assertEqual(command["command_policy"]["python_modules"], sorted(tools._ALLOWED_PYTHON_MODULES))
        with self.assertRaises(tools.CommandPolicyError):
            tools.validate_run_command_arguments({"command": "powershell", "args": []})

    def test_tool_call_uses_live_contract_and_plain_chat_unchanged(self):
        call = SimpleNamespace(id="inspect", function=SimpleNamespace(name="inspect_capabilities", arguments="{}"))
        reply = SimpleNamespace(tool_calls=[call], content=None)
        messages = []
        main.run_tool_round(messages, reply, set(), main.always_allow)
        result = json.loads(messages[-1]["content"])
        self.assertFalse(next(item for item in result["callable_tools"] if item["name"] == "finish_task")["currently_available"])
        self.assertEqual(result["runtime_features"]["Coding Contract"]["active"], False)
        self.assertLessEqual(len(messages[-1]["content"]), main.MAX_TOOL_RESULT_CHARS)

    @unittest.skipUnless(os.name == "nt", "Windows launcher")
    def test_launcher_reaches_formal_interactive_entry_from_another_directory(self):
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(["cmd", "/c", str(ROOT / "agent.cmd"), "--help"],
                                    cwd=cwd, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Run the interactive Mini Agent", result.stdout)
        self.assertIn("--resume", result.stdout)


if __name__ == "__main__":
    unittest.main()
