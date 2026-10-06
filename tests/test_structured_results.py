"""Result state, protocol compatibility, and trace privacy regressions."""
import io
import json
import os
import py_compile
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main
import tools
from mini_agent.result import ToolResult
from acceptance import parse_test_result


def call(name, arguments):
    return SimpleNamespace(id='call-1', function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


class StructuredResultsTests(unittest.TestCase):
    def test_verifier_and_duplicate_lock_use_process_metadata_over_output_text(self):
        result = ToolResult('Exit code: 0\nTimed out: false\nSTDOUT:\n1 passed\nSTDERR:\n',
                            'command_failed', 'timeout', {'returncode': None, 'timed_out': True})
        command = call('run_command', {'command': 'python', 'args': ['-m', 'unittest']})
        self.assertEqual(parse_test_result(result)['reason'], 'test_command_timed_out')
        self.assertFalse(main.counts_as_successful_duplicate(command, result))
        self.assertEqual(main.classify_tool_call(command, result, 'ALLOW'), 'FAILED_COMMAND')

    def test_status_survives_truncation_and_completion_hint(self):
        result = ToolResult('x' * (main.MAX_TOOL_RESULT_CHARS + 1), 'tool_error', 'read_error')
        bounded = main.limit_result_length(result)
        self.assertEqual(bounded.status, 'tool_error')
        self.assertEqual(bounded.code, 'read_error')
        command = call('run_command', {'command': 'python', 'args': ['-m', 'unittest']})
        passed = ToolResult('Exit code: 0\nTimed out: false\nSTDOUT:\n\nSTDERR:\nRan 1 test in 0.01s\n\nOK\n',
                            metadata={'returncode': 0, 'timed_out': False})
        hinted = main.add_completion_hint(command, passed, ('python', ('-m', 'unittest'), '.'))
        self.assertEqual(hinted.status, 'success')
        self.assertEqual(hinted.metadata['returncode'], 0)

    def test_nonzero_empty_unittest_is_unverified_on_python313(self):
        result = ToolResult('Exit code: 5\nTimed out: false\nSTDOUT:\n\nSTDERR:\nRan 0 tests in 0.000s\n\nNO TESTS RAN\n',
                            'command_failed', 'exit_nonzero', {'returncode': 5})
        parsed = parse_test_result(result)
        self.assertEqual(parsed['status'], 'UNKNOWN')
        self.assertEqual(parsed['test_count'], 0)
        self.assertEqual(parsed['reason'], 'no_tests_executed')

    def test_policy_failure_and_missing_file_are_distinct(self):
        with TemporaryDirectory() as directory, patch.object(tools, 'WORKSPACE_DIR', Path(directory)):
            outside = main.execute_tool_call(call('read_file', {'path': '../outside.txt'}))
            missing = main.execute_tool_call(call('read_file', {'path': 'missing.txt'}))
        self.assertEqual(outside.status, 'policy_rejected')
        self.assertEqual(missing.status, 'tool_error')
        self.assertIsInstance(missing, str)

    def test_live_handler_success_does_not_parse_error_looking_content(self):
        definition = SimpleNamespace(tool_kind=main.ToolKind.NORMAL, uses_runtime_context=False,
                                     handler=lambda: '[工具失败] is literal content')
        with patch.dict(main.TOOL_REGISTRY, {'literal_content': definition}):
            result = main.execute_tool_call(call('literal_content', {}))
        self.assertEqual(result.status, 'success')
        self.assertEqual(result.text, '[工具失败] is literal content')

    def test_denial_and_duplicate_preserve_complete_tool_pairs(self):
        with TemporaryDirectory() as directory, patch.object(tools, 'WORKSPACE_DIR', Path(directory)), patch.object(main, 'WORKSPACE_DIR', Path(directory)), redirect_stdout(io.StringIO()):
            messages = []
            request = call('write_file', {'path': 'hello.txt', 'content': 'hello'})
            reply = SimpleNamespace(content=None, tool_calls=[request])
            trace = main.CodingTaskTrace()
            executed = set()
            main.run_tool_round(messages, reply, executed, main.always_deny, trace=trace)
            main.run_tool_round(messages, reply, executed, main.always_allow, trace=trace)
            main.run_tool_round(messages, reply, executed, main.always_allow, trace=trace)
            statuses = [event['result_status'] for event in trace.events]
            self.assertEqual(statuses, ['policy_rejected', 'success', 'duplicate_blocked'])
            for index in (0, 2, 4):
                self.assertEqual(messages[index + 1]['tool_call_id'], messages[index]['tool_calls'][0]['id'])
            json.dumps(messages)  # str-compatible ToolResult stays legal JSON text.

    def test_jsonl_omits_private_bodies_model_text_and_unknown_arguments(self):
        private = 'private customer document body'
        secret = 'fictional-test-api-secret'
        with TemporaryDirectory() as directory, patch.dict(os.environ, {'OPENAI_API_KEY': secret}):
            path = Path(directory) / 'events.jsonl'
            trace = main.CodingTaskTrace(jsonl_path=path)
            request = call('write_file', {'path': 'hello.txt', 'content': private, 'Authorization': secret})
            trace.record_tool(1, request, ToolResult(private), 'ALLOW', True, mutated=True, duration_seconds=0.01)
            trace.record_model_turn(1, main.ModelReply(SimpleNamespace(content=private, tool_calls=None), 'stop', 1, 1, 2))
            trace.record_verifier({'accepted': True, 'reasons': []})
            text = path.read_text(encoding='utf-8')
            self.assertNotIn(private, text)
            self.assertNotIn(secret, text)
            self.assertNotIn('Authorization', text)
            events = [json.loads(line) for line in text.splitlines()]
            self.assertTrue(events[0]['mutation'])
            self.assertEqual(events[-1]['verifier_result']['accepted'], True)

    def test_trace_default_has_no_disk_output(self):
        self.assertIsNone(main.CodingTaskTrace().jsonl_path)

    def test_test_rerun_does_not_use_same_size_stale_bytecode(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / 'value.py'
            module.write_text('VALUE = 1\n', encoding='utf-8')
            stamp = module.stat()
            py_compile.compile(str(module), doraise=True)
            module.write_text('VALUE = 2\n', encoding='utf-8')
            os.utime(module, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            (root / 'test_value.py').write_text(
                'import unittest\nfrom value import VALUE\nclass Fresh(unittest.TestCase):\n'
                '    def test_fresh(self):\n        self.assertEqual(VALUE, 2)\n', encoding='utf-8')
            result = tools.run_command('python', ['-m', 'unittest', 'test_value', '-q'], workspace=root)
        self.assertEqual(result.metadata['returncode'], 0)
        self.assertEqual(parse_test_result(result)['status'], 'PASS')

    def test_jsonl_redaction_keeps_framing_and_hides_short_credentials(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {'OPENAI_API_KEY': 'qz7', 'AUTHORIZATION': 'basic-fixture-authorization'}):
            path = Path(directory) / 'events.jsonl'
            trace = main.CodingTaskTrace(jsonl_path=path)
            trace.record_verifier({'accepted': False, 'reasons': ['Authorization: Bearer fictional-token', 'qz7', 'basic-fixture-authorization']})
            text = path.read_text(encoding='utf-8')
            event = json.loads(text)
            self.assertNotIn('fictional-token', text)
            self.assertNotIn('qz7', text)
            self.assertNotIn('basic-fixture-authorization', text)
            self.assertEqual(len(event['verifier_result']['reasons']), 3)


if __name__ == '__main__':
    unittest.main()
