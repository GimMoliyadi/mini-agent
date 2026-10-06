"""文件策略、审批差异和本轮撤销的隔离回归。"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import file_safety
import tools


class FileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.store = self.base / "changes"
        self.workspace_patch = patch.object(tools, "WORKSPACE_DIR", self.workspace)
        self.workspace_patch.start()

    def tearDown(self):
        self.workspace_patch.stop()
        self.temporary.cleanup()

    def test_sensitive_files_are_denied_for_reads_writes_patches_and_renames(self):
        names = [".env", ".env.local", "id_rsa", "private.pem", "sessions/history.json", "changes/run.json", ".changes/run.json"]
        for name in names:
            target = self.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("dummy_secret=fictional-value", encoding="utf-8")
            with self.subTest(name=name):
                for operation in (
                    lambda: tools.read_file(name), lambda: tools.write_file(name, "replacement"),
                    lambda: tools.apply_patch(name, "fictional", "new"),
                    lambda: tools.rename_file(name, "public.txt"),
                    lambda: file_safety.capture_precondition("write_file", {"path": name, "content": "x"}, self.workspace),
                ):
                    with self.assertRaises(PermissionError):
                        operation()
                self.assertEqual(target.read_text(encoding="utf-8"), "dummy_secret=fictional-value")
        tools.write_file("safe.txt", "needle")
        with self.assertRaises(PermissionError):
            tools.rename_file("safe.txt", ".env")
        listing = tools.list_files()
        self.assertNotIn("sessions", listing)
        self.assertNotIn(".env", listing)
        search = tools.search_text("fictional-value")
        self.assertIn("matches_total: 0", search)
        with self.assertRaises(PermissionError):
            tools.search_text("dummy", "sessions")

    def test_environment_examples_are_readable(self):
        for name in (".env.example", ".env.sample"):
            (self.workspace / name).write_text("MODEL=example", encoding="utf-8")
            self.assertIn("MODEL=example", tools.read_file(name))

    def test_explicit_exception_is_exact_and_cannot_escape_workspace(self):
        (self.workspace / ".env").write_text("MODEL=fixture", encoding="utf-8")
        with patch.object(file_safety, "_SENSITIVE_PATH_EXCEPTIONS", frozenset({".env"})):
            self.assertIn("MODEL=fixture", tools.read_file(".env"))
            with self.assertRaises(PermissionError):
                tools.write_file(".env.local", "x")
            with self.assertRaises(PermissionError):
                tools.read_file("../.env")
        with patch.dict(os.environ, {"MINI_AGENT_ALLOW_SENSITIVE_PATHS": ".env"}):
            with self.assertRaises(PermissionError):
                tools.read_file(".env")

    def test_configuration_rejects_wildcards_and_parent_paths(self):
        for value in ("*", "../.env", '[".env", 1]', "C:/secret"):
            with self.subTest(value=value), patch.dict(os.environ, {"MINI_AGENT_ALLOW_SENSITIVE_PATHS": value}):
                with self.assertRaises(ValueError):
                    file_safety._configured_sensitive_exceptions()

    def test_redaction_masks_only_dummy_credentials_and_private_key_blocks(self):
        text = "Bearer fictional-bearer\nOTHER_TOKEN=fictional-token\n-----BEGIN RSA PRIVATE KEY-----\nfictional-key\n-----END RSA PRIVATE KEY-----\npublic note"
        with patch.dict(os.environ, {"DUMMY_API_KEY": "fictional-env-value"}):
            result = file_safety.redact_text(text + "\nfictional-env-value")
        for secret in ("fictional-bearer", "fictional-token", "fictional-key", "fictional-env-value"):
            self.assertNotIn(secret, result)
        self.assertIn("public note", result)

    def test_redaction_does_not_treat_token_budgets_as_credentials(self):
        text = "max_total_tokens=100000000\nx_value = 1"
        with patch.dict(os.environ, {"MINI_AGENT_MAX_TOTAL_TOKENS": "100000000", "SAMPLE_SECRET": "x"}):
            self.assertEqual(file_safety.redact_text(text), text)

    def test_reads_and_search_results_are_redacted(self):
        (self.workspace / "public.txt").write_text("CUSTOM_PASSWORD=fictional-password\n", encoding="utf-8")
        self.assertNotIn("fictional-password", tools.read_file("public.txt"))
        self.assertNotIn("fictional-password", tools.search_text("CUSTOM_PASSWORD"))

    def test_preview_does_not_write_and_shows_content_diff(self):
        target = self.workspace / "note.txt"
        target.write_bytes(b"old\n")
        arguments = {"path": "note.txt", "old_text": "old", "new_text": "new"}
        preview = file_safety.preview_tool_change("apply_patch", arguments, self.workspace)
        self.assertIn("-old", preview)
        self.assertIn("+new", preview)
        self.assertEqual(target.read_bytes(), b"old\n")
        self.assertFalse(self.store.exists())

    def test_preview_of_new_file_and_rename_is_side_effect_free(self):
        arguments = {"path": "new.txt", "content": "hello\n"}
        self.assertIn("+hello", file_safety.preview_tool_change("write_file", arguments, self.workspace))
        self.assertFalse((self.workspace / "new.txt").exists())
        tools.write_file("old.txt", "hello\n")
        preview = file_safety.preview_tool_change("rename_file", {"source": "old.txt", "destination": "new.txt"}, self.workspace)
        self.assertIn("a/old.txt", preview)
        self.assertIn("b/new.txt", preview)
        self.assertTrue((self.workspace / "old.txt").exists())
        self.assertFalse((self.workspace / "new.txt").exists())

    def test_unknown_mock_tool_never_executes_a_preview(self):
        self.assertIn("不会", file_safety.preview_tool_change("mock_tool", {"path": "../x"}, self.workspace))
        before = file_safety.capture_precondition("mock_tool", {"path": "../x"}, self.workspace)
        self.assertEqual(before, {})
        file_safety.validate_precondition("mock_tool", {}, self.workspace, before)

    def test_precondition_detects_edits_and_changed_arguments(self):
        tools.write_file("note.txt", "old")
        arguments = {"path": "note.txt", "content": "new"}
        before = file_safety.capture_precondition("write_file", arguments, self.workspace)
        file_safety.validate_precondition("write_file", arguments, self.workspace, before)
        with self.assertRaises(PermissionError):
            file_safety.validate_precondition("write_file", {**arguments, "content": "different"}, self.workspace, before)
        (self.workspace / "note.txt").write_bytes(b"external")
        with self.assertRaises(PermissionError):
            file_safety.validate_precondition("write_file", arguments, self.workspace, before)

    def test_precondition_detects_created_destination(self):
        tools.write_file("old.txt", "old")
        arguments = {"source": "old.txt", "destination": "new.txt"}
        before = file_safety.capture_precondition("rename_file", arguments, self.workspace)
        tools.write_file("new.txt", "external")
        with self.assertRaises(PermissionError):
            file_safety.validate_precondition("rename_file", arguments, self.workspace, before)

    def test_undo_restores_initial_bytes_and_removes_only_task_files(self):
        original = b"old\r\ncontent\r\n"
        (self.workspace / "old.txt").write_bytes(original)
        (self.workspace / "untouched.txt").write_bytes(b"user")
        with file_safety.journal_context("task-1", self.workspace, self.store):
            tools.write_file("old.txt", "middle\n")
            tools.apply_patch("old.txt", "middle", "final")
            tools.rename_file("old.txt", "renamed.txt")
            tools.write_file("created.txt", "created")
        log = json.loads((self.store / "task-1.json").read_text(encoding="utf-8"))
        self.assertEqual(log["status"], "complete")
        result = file_safety.undo_task("task-1", self.workspace, self.store)
        self.assertEqual(result["status"], "undone")
        self.assertEqual((self.workspace / "old.txt").read_bytes(), original)
        self.assertFalse((self.workspace / "renamed.txt").exists())
        self.assertFalse((self.workspace / "created.txt").exists())
        self.assertEqual((self.workspace / "untouched.txt").read_bytes(), b"user")
        self.assertEqual(file_safety.undo_task("task-1", self.workspace, self.store)["restored"], [])

    def test_undo_conflict_refuses_all_overwrites(self):
        tools.write_file("first.txt", "original")
        with file_safety.journal_context("conflict", self.workspace, self.store):
            tools.write_file("first.txt", "agent")
            tools.write_file("second.txt", "agent")
        (self.workspace / "second.txt").write_bytes(b"user edit")
        result = file_safety.undo_task("conflict", self.workspace, self.store)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["restored"], [])
        self.assertEqual((self.workspace / "first.txt").read_bytes(), b"agent")
        self.assertEqual((self.workspace / "second.txt").read_bytes(), b"user edit")

    def test_failed_atomic_write_has_no_success_record(self):
        tools.write_file("note.txt", "original")
        with file_safety.journal_context("failed", self.workspace, self.store):
            original_replace = file_safety.os.replace
            def fail_target(source, destination):
                if Path(destination) == self.workspace / "note.txt":
                    raise OSError("fixture replace failed")
                return original_replace(source, destination)
            with patch("file_safety.os.replace", side_effect=fail_target):
                with self.assertRaises(OSError):
                    tools.write_file("note.txt", "replacement")
        log = json.loads((self.store / "failed.json").read_text(encoding="utf-8"))
        self.assertEqual(log["status"], "partial")
        self.assertEqual(log["entries"], {})
        self.assertEqual((self.workspace / "note.txt").read_bytes(), b"original")

    def test_partial_task_can_undo_only_successful_changes(self):
        with self.assertRaises(RuntimeError):
            with file_safety.journal_context("partial", self.workspace, self.store):
                tools.write_file("done.txt", "done")
                raise RuntimeError("fixture cancellation")
        self.assertEqual(json.loads((self.store / "partial.json").read_text())["status"], "partial")
        self.assertEqual(file_safety.undo_task("partial", self.workspace, self.store)["status"], "undone")
        self.assertFalse((self.workspace / "done.txt").exists())

    def test_journal_store_and_workspace_identity_are_enforced(self):
        with self.assertRaises(PermissionError):
            with file_safety.journal_context("bad", self.workspace, self.workspace / "changes"):
                self.fail("must not enter")
        with file_safety.journal_context("valid", self.workspace, self.store):
            tools.write_file("created.txt", "x")
        other = self.base / "other"
        other.mkdir()
        with self.assertRaises(PermissionError):
            file_safety.undo_task("valid", other, self.store)
        self.assertTrue((self.workspace / "created.txt").exists())
        with self.assertRaises(ValueError):
            file_safety.undo_task("../escape", self.workspace, self.store)

    def test_undo_io_failure_reports_progress_and_can_resume(self):
        for name in ("first.txt", "second.txt"):
            tools.write_file(name, "original")
        with file_safety.journal_context("undo-failure", self.workspace, self.store):
            for name in ("first.txt", "second.txt"):
                tools.write_file(name, "agent")
        original_write = file_safety.atomic_write_bytes
        def fail_second(target, content, **kwargs):
            if target == self.workspace / "second.txt":
                raise OSError("fixture restore failed")
            return original_write(target, content, **kwargs)
        with patch("file_safety.atomic_write_bytes", side_effect=fail_second):
            result = file_safety.undo_task("undo-failure", self.workspace, self.store)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["restored"], ["first.txt"])
        self.assertIn("fixture restore failed", result["message"])
        self.assertEqual((self.workspace / "first.txt").read_bytes(), b"original")
        self.assertEqual((self.workspace / "second.txt").read_bytes(), b"agent")
        log = json.loads((self.store / "undo-failure.json").read_text())
        self.assertEqual(log["entries"]["first.txt"]["status"], "restored")
        self.assertEqual(file_safety.undo_task("undo-failure", self.workspace, self.store)["status"], "undone")
        self.assertEqual((self.workspace / "second.txt").read_bytes(), b"original")

    def test_undo_journal_failure_after_restore_remains_resumable(self):
        tools.write_file("note.txt", "original")
        with file_safety.journal_context("undo-save-failure", self.workspace, self.store):
            tools.write_file("note.txt", "agent")
        original_write = file_safety.atomic_write_bytes
        restored = False
        def fail_save_after_restore(target, content, **kwargs):
            nonlocal restored
            if target.parent == self.store and restored:
                raise OSError("fixture journal unavailable")
            original_write(target, content, **kwargs)
            if target == self.workspace / "note.txt":
                restored = True
        with patch("file_safety.atomic_write_bytes", side_effect=fail_save_after_restore):
            result = file_safety.undo_task("undo-save-failure", self.workspace, self.store)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["restored"], ["note.txt"])
        self.assertEqual((self.workspace / "note.txt").read_bytes(), b"original")
        self.assertEqual(file_safety.undo_task("undo-save-failure", self.workspace, self.store)["status"], "undone")

    def test_undo_prevalidates_every_entry_before_restoring_any_file(self):
        for name in ("first.txt", "second.txt"):
            tools.write_file(name, "original")
        with file_safety.journal_context("invalid-mode", self.workspace, self.store):
            for name in ("first.txt", "second.txt"):
                tools.write_file(name, "agent")
        log_path = self.store / "invalid-mode.json"
        log = json.loads(log_path.read_text())
        log["entries"]["second.txt"]["mode"] = {"invalid": True}
        log_path.write_text(json.dumps(log), encoding="utf-8")
        with self.assertRaises(ValueError):
            file_safety.undo_task("invalid-mode", self.workspace, self.store)
        for name in ("first.txt", "second.txt"):
            self.assertEqual((self.workspace / name).read_bytes(), b"agent")

    def test_undo_rechecks_later_paths_after_earlier_restore(self):
        for name in ("first.txt", "second.txt"):
            tools.write_file(name, "original")
        with file_safety.journal_context("concurrent-undo", self.workspace, self.store):
            for name in ("first.txt", "second.txt"):
                tools.write_file(name, "agent")
        original_write = file_safety.atomic_write_bytes
        def edit_second_after_first(target, content, **kwargs):
            original_write(target, content, **kwargs)
            if target == self.workspace / "first.txt":
                (self.workspace / "second.txt").write_bytes(b"user edit")
        with patch("file_safety.atomic_write_bytes", side_effect=edit_second_after_first):
            result = file_safety.undo_task("concurrent-undo", self.workspace, self.store)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["restored"], ["first.txt"])
        self.assertEqual(result["conflicts"], ["second.txt"])
        self.assertEqual((self.workspace / "first.txt").read_bytes(), b"original")
        self.assertEqual((self.workspace / "second.txt").read_bytes(), b"user edit")
        self.assertEqual(file_safety.undo_task("concurrent-undo", self.workspace, self.store)["status"], "conflict")

    def test_failed_undo_progress_write_does_not_modify_workspace(self):
        tools.write_file("note.txt", "original")
        with file_safety.journal_context("no-progress", self.workspace, self.store):
            tools.write_file("note.txt", "agent")
        original_write = file_safety.atomic_write_bytes
        def fail_progress(target, content, **kwargs):
            if target.parent == self.store:
                raise OSError("fixture journal unavailable")
            return original_write(target, content, **kwargs)
        with patch("file_safety.atomic_write_bytes", side_effect=fail_progress):
            result = file_safety.undo_task("no-progress", self.workspace, self.store)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["restored"], [])
        self.assertEqual((self.workspace / "note.txt").read_bytes(), b"agent")
        self.assertEqual(file_safety.undo_task("no-progress", self.workspace, self.store)["status"], "undone")


if __name__ == "__main__":
    unittest.main()
