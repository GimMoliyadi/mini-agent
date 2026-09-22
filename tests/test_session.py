"""Local tests for canonical Session persistence and restore boundaries."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
import acceptance  # noqa: E402
import session  # noqa: E402


def tool_history(content: str = "全文内容") -> list[dict]:
    arguments = json.dumps({"path": "agent_notes.md", "content": content}, ensure_ascii=False)
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "save this"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_write_1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": arguments},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_write_1", "content": "已写入 agent_notes.md"},
        {"role": "assistant", "content": "已完成"},
    ]


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_dir = session.SESSIONS_DIR
        session.SESSIONS_DIR = Path(self.temp_dir.name)

    def tearDown(self):
        session.SESSIONS_DIR = self.original_dir
        self.temp_dir.cleanup()

    def save(self, messages):
        record = session.create_session("test-model", messages, "WRITE_ONLY")
        path = session.save_session(record)
        return record, path

    def test_create_session_file(self):
        record, path = self.save([{"role": "system", "content": "system"}])
        self.assertTrue(path.is_file())
        self.assertEqual(path.stem, record["session_id"])

    def test_ordinary_messages_round_trip_exactly(self):
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，有什么可以帮你？"},
        ]
        record, _ = self.save(messages)
        restored = session.load_session(record["session_id"])
        self.assertEqual(restored["messages"], messages)

    def test_tool_protocol_round_trip_is_legal(self):
        messages = tool_history()
        record, _ = self.save(messages)
        restored = session.load_session(record["session_id"])
        pending = {call["id"] for call in restored["messages"][2]["tool_calls"]}
        for message in restored["messages"]:
            if message.get("role") == "tool":
                pending.remove(message["tool_call_id"])
        self.assertFalse(pending)

    def test_full_write_arguments_remain_canonical(self):
        content = "这是必须留在 canonical messages 里的完整写入内容。" * 5
        record, _ = self.save(tool_history(content))
        restored = session.load_session(record["session_id"])
        arguments = restored["messages"][2]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(arguments)["content"], content)

    def test_write_only_compresses_only_outbound_view_after_restore(self):
        content = "完整写入内容" * 20
        record, _ = self.save(tool_history(content))
        restored = session.load_session(record["session_id"])
        view = main.build_model_context(restored["messages"], mode="WRITE_ONLY")
        canonical_args = restored["messages"][2]["tool_calls"][0]["function"]["arguments"]
        view_args = view[2]["tool_calls"][0]["function"]["arguments"]
        self.assertIn(content, canonical_args)
        self.assertNotIn(content, view_args)
        self.assertIn("previous write content omitted", view_args)

    def test_session_file_has_no_api_key(self):
        _, path = self.save([{"role": "system", "content": "system"}])
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn("api_key", raw.lower())
        self.assertNotIn("authorization", raw.lower())

    def test_task_state_and_contract_round_trip(self):
        contract = acceptance.CodingTaskContract.from_dict(
            {
                "task_id": "finish",
                "instruction": "Fix it.",
                "allowed_paths": ["calculator.py"],
                "test_command": {
                    "command": "python",
                    "args": ["-m", "unittest", "test_calculator", "-q"],
                    "cwd": ".",
                },
            }
        )
        state = acceptance.TaskState(
            event_seq=4,
            last_mutation_event_seq=2,
            last_successful_exact_required_test_seq=3,
            finish_attempts=[
                {"event_seq": 4, "summary": "done", "status": "REJECTED", "reasons": ["stale"]}
            ],
            initial_snapshot={"calculator.py": "digest"},
        )
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Fix it."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "finish_rejected",
                        "type": "function",
                        "function": {
                            "name": "finish_task",
                            "arguments": json.dumps({"summary": "done"}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "finish_rejected",
                "content": '{"status":"REJECTED","reasons":["fresh_test_missing"]}',
            },
        ]
        record = session.create_session(
            "test-model",
            messages,
            task_state=state,
            coding_contract=contract,
        )
        main.update_coding_session_record(
            record,
            contract,
            state,
            messages,
            "resumed-model",
            "WRITE_ONLY",
        )
        session.save_session(record)
        restored = session.load_session(record["session_id"])
        restored_state = session.load_task_state(restored)
        restored_contract, resumed_state = main.restore_coding_session(restored)

        self.assertEqual(restored_state.as_dict(), state.as_dict())
        self.assertEqual(restored["coding_contract"], contract.as_dict())
        self.assertEqual(restored["messages"], messages)
        self.assertEqual(restored["model"], "resumed-model")
        self.assertEqual(restored_contract, contract)
        self.assertEqual(resumed_state.as_dict(), state.as_dict())

    def test_coding_contract_without_state_is_rejected_clearly(self):
        contract = acceptance.CodingTaskContract.from_dict(
            {
                "task_id": "finish",
                "instruction": "Fix it.",
                "allowed_paths": ["calculator.py"],
                "test_command": {
                    "command": "python",
                    "args": ["-m", "unittest", "test_calculator", "-q"],
                    "cwd": ".",
                },
            }
        )

        with self.assertRaisesRegex(session.SessionError, "task_state"):
            session.create_session(
                "test-model",
                [{"role": "system", "content": "system"}],
                coding_contract=contract,
            )

    def test_main_resume_passes_coding_state_to_loop_and_persists_it(self):
        contract = acceptance.CodingTaskContract.from_dict(
            {
                "task_id": "finish",
                "instruction": "Fix it.",
                "allowed_paths": ["calculator.py"],
                "test_command": {
                    "command": "python",
                    "args": ["-m", "unittest", "test_calculator", "-q"],
                    "cwd": ".",
                },
            }
        )
        state = acceptance.TaskState(initial_snapshot={})
        record = session.create_session(
            "test-model",
            [{"role": "system", "content": "system"}],
            task_state=state,
            coding_contract=contract,
        )
        client = SimpleNamespace(close=Mock())
        first_reply = SimpleNamespace(
            message=SimpleNamespace(tool_calls=None, content="ack"),
            finish_reason="stop",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        )

        with (
            patch.object(main, "load_config", return_value=SimpleNamespace(model="test-model")),
            patch.object(main, "print_environment"),
            patch.object(main, "load_session", return_value=record),
            patch.object(main, "build_client", return_value=client),
            patch.object(main, "get_approval_mode", return_value="ALLOW"),
            patch.object(main, "get_context_mode", return_value="WRITE_ONLY"),
            patch.object(main, "ask", return_value=first_reply),
            patch.object(main, "log_reply"),
            patch.object(main, "run_agent_loop") as run_loop,
            patch.object(main, "save_session") as save,
            patch("builtins.input", side_effect=["continue", "exit"]),
        ):
            self.assertEqual(main.main(["--resume", record["session_id"]]), 0)

        self.assertEqual(run_loop.call_args.kwargs["contract"], contract)
        self.assertEqual(run_loop.call_args.kwargs["task_state"].as_dict(), state.as_dict())
        self.assertEqual(record["task_state"], state.as_dict())
        save.assert_called_once_with(record)
        client.close.assert_called_once()

    def test_legacy_session_loads_but_cannot_resume_coding_without_state(self):
        session_id = "legacy-session"
        legacy = {
            "session_id": session_id,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "model": "test-model",
            "messages": [{"role": "system", "content": "system"}],
            "version": 1,
        }
        (session.SESSIONS_DIR / f"{session_id}.json").write_text(
            json.dumps(legacy), encoding="utf-8"
        )
        restored = session.load_session(session_id)

        self.assertNotIn("task_state", restored)
        with self.assertRaisesRegex(session.SessionError, "task_state"):
            session.load_task_state(restored)

    def test_missing_session_is_clear(self):
        with self.assertRaisesRegex(session.SessionError, "不存在"):
            session.load_session("missing-session")

    def test_corrupt_json_is_clear(self):
        path = Path(session.SESSIONS_DIR) / "corrupt-session.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(session.SessionError, "损坏"):
            session.load_session("corrupt-session")

    def test_sessions_are_isolated(self):
        first, _ = self.save([{"role": "user", "content": "first"}])
        second, _ = self.save([{"role": "user", "content": "second"}])
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertEqual(session.load_session(first["session_id"])["messages"][0]["content"], "first")
        self.assertEqual(session.load_session(second["session_id"])["messages"][0]["content"], "second")

    def test_main_missing_resume_has_no_traceback(self):
        environment = os.environ.copy()
        result = subprocess.run(
            [sys.executable, "main.py", "--resume", "missing-session"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Session错误", result.stderr)
        self.assertIn("不存在", result.stderr)
        self.assertNotIn("Traceback", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
