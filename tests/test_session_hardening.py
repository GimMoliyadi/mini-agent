import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import session
import session_cli


class SessionHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.directory = self.root / "state" / "sessions"
        self.store_patch = patch.object(session, "SESSIONS_DIR", self.directory)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.record = session.create_session(
            "offline", [{"role": "system", "content": "offline system"}],
            workspace=self.workspace,
        )

    def save(self):
        return session.save_session(self.record)

    def test_invalid_json_arguments_are_preserved_as_error_history(self):
        self.record["messages"] += [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "broken", "type": "function", "function": {"name": "read_file", "arguments": "{"}}]},
            {"role": "tool", "tool_call_id": "broken", "content": "[工具失败] JSON 参数错误"},
            {"role": "assistant", "content": "本次没有读取文件。"},
        ]
        self.save()
        loaded = session.load_session(self.record["session_id"], workspace=self.workspace)
        self.assertEqual(loaded["messages"], self.record["messages"])

    def test_invalid_content_type_and_dangling_tool_calls_are_rejected(self):
        invalid_messages = [
            [{"role": "system", "content": 1}],
            [{"role": "assistant", "content": None, "tool_calls": [{"id": "pending", "function": {"name": "read_file", "arguments": "{}"}}]}],
            [{"role": "tool", "tool_call_id": "unknown", "content": "no source"}],
        ]
        for messages in invalid_messages:
            with self.subTest(messages=messages), self.assertRaises(session.SessionError):
                session.create_session("offline", messages, workspace=self.workspace)

    def test_workspace_mismatch_never_silently_rebinds(self):
        self.save()
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(session.SessionError, "不匹配"):
            session.load_session(self.record["session_id"], workspace=other, allow_legacy_workspace=True)
        self.assertEqual(session.load_session(self.record["session_id"])["workspace"], str(self.workspace.resolve()))

    def test_legacy_versions_require_explicit_workspace_adoption(self):
        for version in (1, 2):
            with self.subTest(version=version):
                legacy = copy.deepcopy(self.record)
                legacy["version"] = version
                legacy.pop("workspace")
                self.directory.mkdir(parents=True, exist_ok=True)
                target = session._path_for(legacy["session_id"])
                target.write_text(json.dumps(legacy), encoding="utf-8")
                self.assertEqual(session.load_session(legacy["session_id"])["version"], version)
                with self.assertRaisesRegex(session.SessionError, "adopt-workspace"):
                    session.load_session(legacy["session_id"], workspace=self.workspace)
                adopted = session.load_session(legacy["session_id"], workspace=self.workspace, allow_legacy_workspace=True)
                self.assertEqual(adopted["version"], session.SESSION_VERSION)
                self.assertEqual(adopted["workspace"], str(self.workspace.resolve()))
                self.assertEqual(json.loads(target.read_text())["version"], version)

    def test_stale_revision_cannot_overwrite_newer_conversation(self):
        self.save()
        first = session.load_session(self.record["session_id"])
        stale = session.load_session(self.record["session_id"])
        first["messages"].append({"role": "user", "content": "newer message"})
        session.save_session(first)
        stale["messages"].append({"role": "user", "content": "stale message"})
        with self.assertRaisesRegex(session.SessionError, "其他进程"):
            session.save_session(stale)
        current = session.load_session(self.record["session_id"])
        self.assertEqual(current["messages"][-1]["content"], "newer message")

    def test_concurrent_task_lock_is_rejected_and_released(self):
        with session.task_lock(self.record["session_id"]):
            with self.assertRaises(session.SessionError):
                with session.task_lock(self.record["session_id"]):
                    self.fail("same session must not run concurrently")
        with session.task_lock(self.record["session_id"]):
            pass

    def test_encoding_failure_preserves_previous_session_bytes(self):
        target = self.save()
        original = target.read_bytes()
        self.record["messages"].append({"role": "user", "content": chr(0xD800)})
        with self.assertRaises(session.SessionError):
            session.save_session(self.record)
        self.assertEqual(target.read_bytes(), original)

    def test_record_id_must_match_storage_filename(self):
        target = self.save()
        changed = json.loads(target.read_text(encoding="utf-8"))
        changed["session_id"] = "another-session"
        target.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(session.SessionError, "文件名"):
            session.load_session(self.record["session_id"])

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(session.SessionError):
            session.load_session("../outside")
        with self.assertRaises(session.SessionError):
            session_cli.delete_session("../outside", confirmed=True)

    def test_export_redacts_credentials_without_mutating_canonical_record(self):
        secret = "private-test-credential-12345678"
        self.record["messages"].append({"role": "user", "content": "API_KEY=" + secret})
        stored = self.save()
        target = self.root / "export.json"
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
            result = session_cli.export_session(self.record["session_id"], target)
        self.assertEqual(result["status"], "exported")
        self.assertNotIn(secret, target.read_text(encoding="utf-8"))
        self.assertIn(secret, stored.read_text(encoding="utf-8"))
        self.assertTrue(json.loads(target.read_text(encoding="utf-8"))["export_redacted"])

    def test_export_overwrite_requires_explicit_confirmation(self):
        self.save()
        target = self.root / "export.json"
        target.write_text("original", encoding="utf-8")
        with self.assertRaises(session.SessionError):
            session_cli.export_session(self.record["session_id"], target)
        self.assertEqual(target.read_text(), "original")
        session_cli.export_session(self.record["session_id"], target, overwrite=True)
        self.assertTrue(json.loads(target.read_text())["export_redacted"])

    def test_export_cannot_overwrite_active_storage(self):
        stored = self.save()
        original = stored.read_bytes()
        with self.assertRaises(session.SessionError):
            session_cli.export_session(self.record["session_id"], stored, overwrite=True)
        self.assertEqual(stored.read_bytes(), original)

    def test_delete_requires_confirmation_and_keeps_workspace(self):
        stored = self.save()
        note = self.workspace / "keep.txt"
        note.write_text("keep", encoding="utf-8")
        with self.assertRaises(session.SessionError):
            session_cli.delete_session(self.record["session_id"])
        self.assertTrue(stored.is_file())
        result = session_cli.delete_session(self.record["session_id"], confirmed=True)
        self.assertEqual(result["status"], "deleted")
        self.assertFalse(stored.exists())
        self.assertEqual(note.read_text(), "keep")

    def test_corrupt_record_is_listed_without_exposing_contents(self):
        stored = self.save()
        stored.write_text("not JSON", encoding="utf-8")
        rows = session_cli.list_metadata()
        self.assertEqual(rows[0]["status"], "corrupt")
        self.assertNotIn("not JSON", rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
