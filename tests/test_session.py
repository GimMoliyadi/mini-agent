"""Local tests for canonical Session persistence and restore boundaries."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
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
