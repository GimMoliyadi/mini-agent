"""原子变更、扫描预算和真实子进程清理回归。"""

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import file_safety
import process_runner
import tools


def process_is_running(pid: int) -> bool:
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x00000102
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/stat")
    return not status.exists() or status.read_text().split()[2] != "Z"


class ToolsHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.workspace_patch = patch.object(tools, "WORKSPACE_DIR", self.workspace)
        self.workspace_patch.start()

    def tearDown(self):
        self.workspace_patch.stop()
        self.temporary.cleanup()

    def test_invalid_write_content_and_utf8_failure_leave_original_bytes(self):
        target = self.workspace / "note.txt"
        original = b"original\r\nbytes\r\n"
        target.write_bytes(original)
        for content in (None, 1, b"bytes", "invalid\ud800"):
            with self.subTest(content=repr(content)):
                with self.assertRaises((ValueError, UnicodeEncodeError)):
                    tools.write_file("note.txt", content)
                self.assertEqual(target.read_bytes(), original)
        with self.assertRaises(UnicodeEncodeError):
            tools.write_file("not-created/note.txt", "invalid\ud800")
        self.assertFalse((self.workspace / "not-created").exists())

    def test_patch_encoding_failure_does_not_truncate_file(self):
        target = self.workspace / "note.txt"
        target.write_bytes(b"old\r\n")
        for old, new in (("old", "\ud800"), ("\ud800", "new"), ("old", None)):
            with self.subTest(old=repr(old), new=repr(new)):
                with self.assertRaises((ValueError, UnicodeEncodeError)):
                    tools.apply_patch("note.txt", old, new)
                self.assertEqual(target.read_bytes(), b"old\r\n")

    def test_atomic_replace_failure_preserves_original_and_cleans_temporary_file(self):
        target = self.workspace / "note.txt"
        target.write_bytes(b"old")
        with patch("file_safety.os.replace", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                tools.write_file("note.txt", "new")
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual({path.name for path in self.workspace.iterdir()}, {"note.txt"})

    def test_write_and_patch_cut_hardlink_aliases(self):
        for operation in ("write", "patch"):
            with self.subTest(operation=operation):
                target = self.workspace / f"{operation}.txt"
                alias = self.workspace / f"{operation}-alias.txt"
                target.write_bytes(b"old\r\n")
                os.link(target, alias)
                if operation == "write":
                    tools.write_file(target.name, "new\n")
                    self.assertEqual(target.read_bytes(), b"new\n")
                else:
                    tools.apply_patch(target.name, "old", "new")
                    self.assertEqual(target.read_bytes(), b"new\r\n")
                self.assertEqual(alias.read_bytes(), b"old\r\n")
                self.assertFalse(os.path.samefile(target, alias))

    def test_windows_ads_device_and_ambiguous_paths_are_rejected(self):
        for name in ("note.txt:stream", "NUL", "CON.txt", "folder/COM1.log", "folder/LPT1", "note.txt.", "note.txt ", "C:relative", "\\\\?\\C:\\device", "\\\\.\\NUL"):
            with self.subTest(name=name):
                with self.assertRaises(PermissionError):
                    tools.write_file(name, "must not write")
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_read_write_patch_and_search_have_file_size_limits(self):
        target = self.workspace / "large.txt"
        target.write_bytes(b"x" * (file_safety.MAX_FILE_BYTES + 1))
        for operation in (lambda: tools.read_file("large.txt"), lambda: tools.write_file("large.txt", "small"), lambda: tools.apply_patch("large.txt", "x", "y"), lambda: tools.write_file("new.txt", "x" * (file_safety.MAX_FILE_BYTES + 1))):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(target.stat().st_size, file_safety.MAX_FILE_BYTES + 1)
        self.assertIn("scan_truncated: true", tools.search_text("x"))
        self.assertFalse((self.workspace / "new.txt").exists())

    def test_search_scan_and_file_count_budgets_are_truthful(self):
        for index in range(4):
            (self.workspace / f"{index}.txt").write_bytes(b"needle\n")
        with patch.object(tools, "MAX_SEARCH_SCAN_BYTES", 14):
            result = tools.search_text("needle")
        self.assertIn("scan_truncated: true", result)
        self.assertIn("scanned_bytes: 14", result)
        self.assertIn("matches_total: 2", result)
        with patch.object(tools, "MAX_SEARCH_FILES", 1):
            result = tools.search_text("needle")
        self.assertIn("scan_truncated: true", result)
        self.assertIn("matches_total: 1", result)
        with patch.object(tools, "MAX_SEARCH_ENTRIES", 1):
            self.assertIn("scan_truncated: true", tools.search_text("needle"))

    def test_rename_never_overwrites_a_concurrently_created_destination(self):
        tools.write_file("old.txt", "old")
        original_link = file_safety.os.link
        def concurrent_create(source, destination):
            Path(destination).write_bytes(b"user")
            return original_link(source, destination)
        with patch("file_safety.os.link", side_effect=concurrent_create):
            with self.assertRaises(FileExistsError):
                tools.rename_file("old.txt", "new.txt")
        self.assertEqual((self.workspace / "old.txt").read_bytes(), b"old")
        self.assertEqual((self.workspace / "new.txt").read_bytes(), b"user")

    def test_child_environment_drops_credentials_and_injection_settings(self):
        injected = {"OPENAI_API_KEY": "fictional-key", "AWS_SECRET_ACCESS_KEY": "fictional-secret", "PYTHONPATH": "fictional-path", "PYTEST_ADDOPTS": "--fixture", "GIT_CONFIG_COUNT": "1", "MINI_AGENT_ALLOW_SENSITIVE_PATHS": ".env"}
        with patch.dict(os.environ, injected):
            result = process_runner.run_process([sys.executable, "-c", "import os; print(sorted(os.environ)); print(os.environ['AGENT_WORKSPACE'])"], self.workspace, workspace=self.workspace, timeout_seconds=5)
        self.assertEqual(result.returncode, 0)
        decoded = result.stdout.decode("utf-8")
        for name in injected:
            self.assertNotIn(name, decoded)
        self.assertIn(str(self.workspace), decoded)
        self.assertEqual(result.captured_bytes, len(result.stdout) + len(result.stderr))

    def test_true_combined_byte_budget_bounds_stdout_and_stderr(self):
        code = "import os; os.write(1, ('界'*2000).encode()); os.write(2, b'y'*6000)"
        result = process_runner.run_process([sys.executable, "-c", code], self.workspace, workspace=self.workspace, timeout_seconds=5, output_limit_bytes=1024)
        self.assertTrue(result.output_limit_exceeded)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.captured_bytes, 1024)
        self.assertEqual(len(result.stdout) + len(result.stderr), 1024)
        self.assertIsNone(result.returncode)

    def test_real_run_command_reports_output_limit_machine_line(self):
        (self.workspace / "test_emit.py").write_text("import os\nos.write(1, b'x' * 100000)\n", encoding="utf-8")
        with patch.object(tools, "MAX_COMMAND_OUTPUT_BYTES", 1024):
            result = tools.run_command("python", ["-m", "unittest", "test_emit"])
        self.assertIn("Output limit exceeded: true", result)
        self.assertIn("Captured bytes: 1024", result)
        self.assertIn("Timed out: false", result)

    def test_real_timeout_returns_partial_output_and_cleans_reader_threads(self):
        before_threads = {thread.ident for thread in threading.enumerate()}
        result = process_runner.run_process([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"], self.workspace, workspace=self.workspace, timeout_seconds=0.3)
        self.assertTrue(result.timed_out)
        self.assertIn(b"started", result.stdout)
        self.assertEqual({thread.ident for thread in threading.enumerate()}, before_threads)

    def test_cancellation_kills_only_own_process_tree(self):
        child_code = "from pathlib import Path; import time; Path('child_ready').write_text('ready'); time.sleep(30)"
        parent_code = f"import subprocess, sys, time; from pathlib import Path; child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); Path('child_pid').write_text(str(child.pid)); time.sleep(30)"
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        class CancelWhenReady:
            def check(inner):
                if (self.workspace / "child_ready").exists():
                    raise RuntimeError("fixture cancelled")
        try:
            with patch("process_runner._current_budget", return_value=CancelWhenReady()):
                with self.assertRaisesRegex(RuntimeError, "fixture cancelled"):
                    process_runner.run_process([sys.executable, "-c", parent_code], self.workspace, workspace=self.workspace, timeout_seconds=5)
            pid = int((self.workspace / "child_pid").read_text())
            self.assertFalse(process_is_running(pid))
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait(timeout=5)

    def test_output_limit_kills_descendant_which_inherits_output_pipe(self):
        child_code = "import time; time.sleep(30)"
        parent_code = f"import subprocess, sys, os, time; from pathlib import Path; child=subprocess.Popen([sys.executable, '-c', {child_code!r}]); Path('child_pid').write_text(str(child.pid)); os.write(1, b'x'*100000); time.sleep(30)"
        result = process_runner.run_process([sys.executable, "-c", parent_code], self.workspace, workspace=self.workspace, timeout_seconds=5, output_limit_bytes=1024)
        self.assertTrue(result.output_limit_exceeded)
        self.assertFalse(process_is_running(int((self.workspace / "child_pid").read_text())))

    def test_child_git_cannot_discover_repository_above_workspace(self):
        environment = process_runner.command_environment(self.workspace)
        self.assertEqual(environment["GIT_CEILING_DIRECTORIES"], str(self.workspace.parent))
        self.assertEqual(environment["MINI_AGENT_STATE_DIR"], str(self.workspace))

    def test_process_handle_is_closed_even_when_popen_is_retained(self):
        processes = []
        original_start = subprocess.Popen
        def retain_process(*args, **kwargs):
            process = original_start(*args, **kwargs)
            processes.append(process)
            return process
        with patch("process_runner.subprocess.Popen", side_effect=retain_process):
            result = process_runner.run_process([sys.executable, "-c", "print('done')"], self.workspace, workspace=self.workspace, timeout_seconds=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(processes), 1)
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)
        if os.name == "nt":
            self.assertTrue(processes[0]._handle.closed)

    def test_hardlink_writes_cannot_change_file_outside_workspace(self):
        restricted = self.workspace / "restricted"
        restricted.mkdir()
        for operation in ("write", "patch"):
            outside = self.workspace / f"external-{operation}.txt"
            outside.write_bytes(b"old\r\n")
            os.link(outside, restricted / f"{operation}.txt")
            with patch.object(tools, "WORKSPACE_DIR", restricted):
                if operation == "write":
                    tools.write_file("write.txt", "new\n")
                else:
                    tools.apply_patch("patch.txt", "old", "new")
            self.assertEqual(outside.read_bytes(), b"old\r\n")
            self.assertFalse(os.path.samefile(outside, restricted / f"{operation}.txt"))

    def test_exact_byte_limit_is_success_and_one_extra_byte_is_rejected(self):
        for amount, exceeded in ((1024, False), (1025, True)):
            with self.subTest(amount=amount):
                code = f"import os; os.write(1, b'x' * {amount})"
                result = process_runner.run_process([sys.executable, "-c", code], self.workspace, workspace=self.workspace, timeout_seconds=5, output_limit_bytes=1024)
                self.assertEqual(result.output_limit_exceeded, exceeded)
                self.assertEqual(result.captured_bytes, 1024)
                self.assertFalse(result.timed_out)
                self.assertEqual(result.returncode, None if exceeded else 0)

    def test_invalid_process_budgets_are_rejected_before_starting(self):
        invalid = ((float("nan"), 10), (float("inf"), 10), (True, 10), (1, 1.5), (1, True), (1, 0))
        with patch("process_runner.subprocess.Popen", side_effect=AssertionError("unexpected process")) as start:
            for timeout, limit in invalid:
                with self.subTest(timeout=timeout, limit=limit):
                    with self.assertRaises(ValueError):
                        process_runner.run_process([sys.executable, "-c", "pass"], self.workspace, workspace=self.workspace, timeout_seconds=timeout, output_limit_bytes=limit)
        start.assert_not_called()

    def test_reader_start_failure_cleans_started_thread_and_process_handles(self):
        processes, threads = [], []
        original_start = subprocess.Popen
        original_thread_start = threading.Thread.start
        def retain_process(*args, **kwargs):
            process = original_start(*args, **kwargs)
            processes.append(process)
            return process
        def fail_second_reader(thread):
            if threads:
                raise RuntimeError("fixture thread start failed")
            original_thread_start(thread)
            threads.append(thread)
        with patch("process_runner.subprocess.Popen", side_effect=retain_process), patch("process_runner.threading.Thread.start", new=fail_second_reader):
            with self.assertRaisesRegex(RuntimeError, "fixture thread start failed"):
                process_runner.run_process([sys.executable, "-c", "import time; time.sleep(30)"], self.workspace, workspace=self.workspace, timeout_seconds=5)
        self.assertEqual(len(threads), 1)
        self.assertFalse(threads[0].is_alive())
        self.assertIsNotNone(processes[0].returncode)
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)
        if os.name == "nt":
            self.assertTrue(processes[0]._handle.closed)

    def test_redaction_expansion_keeps_read_pagination_complete(self):
        import re
        secret = "12345678"
        (self.workspace / "redact.txt").write_text((secret + "\n") * 380, encoding="utf-8")
        with patch.dict(os.environ, {"SAMPLE_SECRET": secret}):
            first = tools.read_file("redact.txt", max_lines=500)
            self.assertLessEqual(len(first), tools.MAX_TOOL_RESULT_CHARS)
            self.assertIn("has_more：true", first)
            next_line = int(re.search(r"next_start_line：(\d+)", first).group(1))
            second = tools.read_file("redact.txt", start_line=next_line, max_lines=500)
        self.assertLessEqual(len(second), tools.MAX_TOOL_RESULT_CHARS)
        self.assertIn("has_more：false", second)
        self.assertEqual(first.count("[REDACTED]") + second.count("[REDACTED]"), 380)
        self.assertNotIn(secret, first + second)

    def test_single_line_that_expands_beyond_result_limit_fails_explicitly(self):
        secret = "12345678"
        (self.workspace / "redact.txt").write_text(secret * 400, encoding="utf-8")
        with patch.dict(os.environ, {"SAMPLE_SECRET": secret}):
            with self.assertRaisesRegex(ValueError, "脱敏后"):
                tools.read_file("redact.txt")


if __name__ == "__main__":
    unittest.main()
