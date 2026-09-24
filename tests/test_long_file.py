"""Phase 9 tests for ranged file reading and model-driven pagination."""

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
import tools  # noqa: E402
from config import MAX_READ_RESULT_CHARS, MAX_TOOL_RESULT_CHARS  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


def fake_call(arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        function=SimpleNamespace(
            name="read_file",
            arguments=json.dumps(arguments, ensure_ascii=False),
        )
    )


def make_long_markdown(path: Path) -> None:
    lines = []
    for line_number in range(1, 451):
        if line_number in {1, 151, 301}:
            lines.append(f"# Section {(line_number - 1) // 150 + 1}\n")
        elif line_number == 377:
            lines.append('TARGET_FACT = "phase9-secret-value"\n')
        else:
            lines.append(f"line {line_number}: ordinary filler content\n")
    path.write_text("".join(lines), encoding="utf-8")


class LongFileTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_workspace = tools.WORKSPACE_DIR
        tools.WORKSPACE_DIR = Path(self.temp_dir.name)
        self.long_file = tools.WORKSPACE_DIR / "long_notes.md"
        make_long_markdown(self.long_file)

    def tearDown(self):
        tools.WORKSPACE_DIR = self.original_workspace
        self.temp_dir.cleanup()

    def test_default_read_returns_first_segment_and_metadata(self):
        result = tools.read_file("long_notes.md")
        self.assertIn("当前范围：1-100 行", result)
        self.assertIn("总行数：450", result)
        self.assertIn("has_more：true", result)
        self.assertIn("next_start_line：101", result)
        self.assertNotIn("TARGET_FACT", result)

    def test_start_line_and_max_lines_select_exact_range(self):
        result = tools.read_file("long_notes.md", start_line=101, max_lines=3)
        self.assertIn("当前范围：101-103 行", result)
        self.assertIn("line 101: ordinary filler content", result)
        self.assertIn("line 103: ordinary filler content", result)
        self.assertNotIn("line 100: ordinary filler content", result)
        self.assertNotIn("line 104: ordinary filler content", result)
        self.assertIn("next_start_line：104", result)

    def test_metadata_marks_last_segment(self):
        result = tools.read_file("long_notes.md", start_line=401, max_lines=100)
        self.assertIn("当前范围：401-450 行", result)
        self.assertIn("总行数：450", result)
        self.assertIn("has_more：false", result)
        self.assertIn("next_start_line：null", result)

    def test_past_end_is_clear(self):
        result = tools.read_file("long_notes.md", start_line=999, max_lines=10)
        self.assertIn("当前范围：无 行", result)
        self.assertIn("总行数：450", result)
        self.assertIn("has_more：false", result)
        self.assertIn("next_start_line：null", result)

    def test_invalid_ranges_are_model_readable_errors(self):
        for arguments, expected in (
            ({"path": "long_notes.md", "start_line": 0}, "start_line"),
            ({"path": "long_notes.md", "max_lines": 0}, "max_lines"),
            ({"path": "long_notes.md", "max_lines": "100"}, "max_lines"),
        ):
            result = main.execute_tool_call(fake_call(arguments))
            self.assertTrue(result.startswith(main.TOOL_FAILURE_PREFIX), result)
            self.assertIn(expected, result)

    def test_empty_and_utf8_files(self):
        (tools.WORKSPACE_DIR / "empty.md").write_text("", encoding="utf-8")
        (tools.WORKSPACE_DIR / "中文.md").write_text(
            "第一行：你好\n第二行：世界\n", encoding="utf-8"
        )

        empty = tools.read_file("empty.md")
        self.assertIn("总行数：0", empty)
        self.assertIn("has_more：false", empty)
        self.assertIn("next_start_line：null", empty)

        chinese = tools.read_file("中文.md")
        self.assertIn("第一行：你好", chinese)
        self.assertIn("第二行：世界", chinese)
        self.assertIn("总行数：2", chinese)

    def test_result_length_guard_remains_active(self):
        result = main.execute_tool_call(
            fake_call({"path": "long_notes.md", "max_lines": 200})
        )
        self.assertNotIn("[工具结果已截断：", result)
        self.assertLess(len(result), MAX_TOOL_RESULT_CHARS)
        self.assertIn("has_more：true", result)

        guarded = main.limit_result_length("x" * (MAX_TOOL_RESULT_CHARS + 1))
        self.assertIn("[工具结果已截断：", guarded)

    def test_large_ranges_return_complete_lines_with_consistent_metadata(self):
        for start_line in (101, 301):
            with self.subTest(start_line=start_line):
                result = tools.read_file(
                    "long_notes.md", start_line=start_line, max_lines=200
                )
                self.assertLess(len(result), MAX_TOOL_RESULT_CHARS)
                self.assertNotIn("[工具结果已截断：", result)

                range_match = re.search(r"当前范围：(\d+)-(\d+) 行", result)
                self.assertIsNotNone(range_match, result)
                actual_start, actual_end = map(int, range_match.groups())
                self.assertEqual(actual_start, start_line)
                self.assertIn(
                    f"next_start_line：{actual_end + 1}",
                    result,
                )
                body = result.split("--- 内容开始 ---\n", 1)[1].split(
                    "\n--- 内容结束 ---", 1
                )[0]
                self.assertTrue(body.endswith("\n"), repr(body[-20:]))
                self.assertIn(f"line {actual_end}:", body)
                self.assertNotIn(f"line {actual_end + 1}:", body)

    def test_single_line_over_safe_budget_is_an_explicit_tool_error(self):
        oversized = tools.WORKSPACE_DIR / "oversized.md"
        oversized.write_text("x" * (MAX_READ_RESULT_CHARS + 1) + "\n", encoding="utf-8")

        result = main.execute_tool_call(fake_call({"path": "oversized.md"}))

        self.assertTrue(result.startswith(main.TOOL_FAILURE_PREFIX), result)
        self.assertIn("第 1 行长度超过单次读取安全上限", result)


class MockPaginationTests(unittest.TestCase):
    def test_mock_agent_reads_until_fact(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            make_long_markdown(workspace / "long_notes.md")
            port = self._free_port()
            environment = os.environ.copy()
            environment.update(
                {
                    "AGENT_WORKSPACE": str(workspace),
                    "OPENAI_API_KEY": "any-value",
                    "OPENAI_BASE_URL": f"http://127.0.0.1:{port}/v1",
                    "OPENAI_MODEL": "mock-model",
                    "NO_PROXY": "127.0.0.1,localhost",
                    "PYTHONUTF8": "1",
                }
            )
            server = subprocess.Popen(
                [sys.executable, "tests/mock_server.py", str(port)],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            session_id = None
            try:
                self._wait_for_server(port, server)
                result = subprocess.run(
                    [sys.executable, "main.py"],
                    cwd=ROOT,
                    env=environment,
                    input=(
                        "阅读 long_notes.md，找到其中 TARGET_FACT 的值。"
                        "你可以按需要分段读取，找到后告诉我答案。\nexit\n"
                    ),
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    text=True,
                    timeout=30,
                )
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                session_match = re.search(r"Session: ([A-Za-z0-9_-]+)", output)
                session_id = session_match.group(1) if session_match else None
                self.assertIn('read_file {"path": "long_notes.md"}', output)
                self.assertIn(
                    'read_file {"max_lines": 100, "path": "long_notes.md", "start_line": 101}',
                    output,
                )
                self.assertIn(
                    'read_file {"max_lines": 100, "path": "long_notes.md", "start_line": 201}',
                    output,
                )
                self.assertIn(
                    'read_file {"max_lines": 100, "path": "long_notes.md", "start_line": 301}',
                    output,
                )
                self.assertIn("TARGET_FACT 的值：phase9-secret-value", output)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                if session_id:
                    (ROOT / "sessions" / f"{session_id}.json").unlink(missing_ok=True)

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    @staticmethod
    def _wait_for_server(port: int, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("mock server unexpectedly exited")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise AssertionError("mock server did not start")


if __name__ == "__main__":
    unittest.main()
