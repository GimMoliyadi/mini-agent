"""Offline checks for Phase 23.1 Provider proxy isolation and preflight."""

import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from eval import phase23_budget


class Phase23ProviderProxyTests(unittest.TestCase):
    def test_project_proxy_replaces_parent_proxies_only_in_child_environment(self):
        parent = {
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "ALL_PROXY": "socks5://127.0.0.1:7897",
            "MINI_AGENT_HTTP_PROXY": "http://127.0.0.1:9674",
        }

        child = phase23_budget._provider_child_environment(parent)

        self.assertEqual(child["HTTP_PROXY"], "http://127.0.0.1:9674")
        self.assertEqual(child["HTTPS_PROXY"], "http://127.0.0.1:9674")
        self.assertNotIn("ALL_PROXY", child)
        self.assertEqual(parent["HTTP_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(parent["HTTPS_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(parent["ALL_PROXY"], "socks5://127.0.0.1:7897")

    def test_missing_project_proxy_preserves_inherited_proxy_behavior(self):
        inherited = {
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "ALL_PROXY": "socks5://127.0.0.1:7897",
        }

        self.assertEqual(phase23_budget._provider_child_environment(inherited), inherited)

    def test_run_attempt_passes_project_proxy_to_provider_child(self):
        parent = {
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "ALL_PROXY": "socks5://127.0.0.1:7897",
            "MINI_AGENT_HTTP_PROXY": "http://127.0.0.1:9674",
        }
        captured = {}

        def fake_run(*args, **kwargs):
            captured.update(kwargs["env"])
            return SimpleNamespace(stdout=b"{}", stderr=b"", returncode=0)

        with (
            patch.dict(os.environ, parent, clear=True),
            patch.object(phase23_budget.config, "load_env_file"),
            patch.object(phase23_budget.phase22_5, "setup"),
            patch.object(phase23_budget.subprocess, "run", side_effect=fake_run),
        ):
            phase23_budget._run_attempt(10)

        self.assertEqual(captured["HTTP_PROXY"], "http://127.0.0.1:9674")
        self.assertEqual(captured["HTTPS_PROXY"], "http://127.0.0.1:9674")
        self.assertNotIn("ALL_PROXY", captured)

    def test_preflight_accepts_any_nonempty_assistant_response(self):
        model_config = SimpleNamespace(model="test-model", base_url="https://provider.invalid/v1")
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(role="assistant", content="Provider is ready."),
                )
            ]
        )
        client = MagicMock()
        client.with_options.return_value = client
        client.chat.completions.create.return_value = response

        with (
            patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://127.0.0.1:7897",
                    "HTTPS_PROXY": "http://127.0.0.1:7897",
                    "ALL_PROXY": "socks5://127.0.0.1:7897",
                    "MINI_AGENT_HTTP_PROXY": "http://127.0.0.1:9674",
                },
                clear=True,
            ),
            patch.object(phase23_budget.config, "load_env_file"),
            patch.object(phase23_budget.config, "load_config", return_value=model_config),
            patch.object(phase23_budget.main, "build_client", return_value=client),
        ):
            result = phase23_budget._provider_preflight()

        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["valid_assistant_response"])
        self.assertFalse(result["response_text_recorded"])
        client.with_options.assert_called_once_with(max_retries=0)
        client.chat.completions.create.assert_called_once()

    def test_report_section_preserves_gate_status_and_baseline(self):
        payload = json.loads(phase23_budget.RESULTS.read_text(encoding="utf-8"))
        payload["phase23_1_recovery"] = {
            "status": "preflight_failed",
            "project_proxy_configured": True,
            "proxy_environment": {
                "parent": phase23_budget._proxy_diagnostics({"HTTP_PROXY": "http://127.0.0.1:7897"}),
                "provider": phase23_budget._proxy_diagnostics({"HTTP_PROXY": "http://127.0.0.1:9674"}),
            },
            "provider_preflight": {
                "status": "fail",
                "valid_assistant_response": False,
                "request_completed": True,
                "provider_scheme": "https",
                "model": "test-model",
                "sdk_max_retries": 0,
            },
            "preflight_history": [],
            "historical_proxy_provenance": {
                "phase23_7897_source": "Parent process proxy assignment.",
            },
            "budget_runs": {},
        }

        report = phase23_budget._phase23_1_report_section(payload)

        self.assertIn("preflight_failed", report)
        self.assertIn("Parent process proxy assignment.", report)
        self.assertIn("Reused Phase 22.5 baseline", report)
        self.assertIn("Not started: Provider preflight did not pass.", report)

    def test_analysis_uses_the_actual_failed_test_and_observed_recovery(self):
        test_arguments = {
            "command": phase23_budget.REQUIRED_TEST[0],
            "args": phase23_budget.REQUIRED_TEST[1],
            "cwd": phase23_budget.REQUIRED_TEST[2],
        }
        result = {
            "metrics": {
                "accepted": True,
                "model_calls": 5,
                "tool_calls": 4,
                "total_tokens": 100,
                "finish_task_calls": 1,
                "post_verification_extra_tool_calls": 0,
                "tool_chain": [
                    {"event_seq": 1, "turn": 1, "tool": "apply_patch", "arguments": {"path": "src/pricing/coupons.py"}},
                    {"event_seq": 2, "turn": 2, "tool": "run_command", "arguments": test_arguments, "exit_code": 1, "result": "FAIL: test_decimal_percent (test_coupons.CouponTests)\nAssertionError: 99.9 != 90.0"},
                    {"event_seq": 3, "turn": 3, "tool": "apply_patch", "arguments": {"path": "src/pricing/coupons.py"}},
                    {"event_seq": 4, "turn": 4, "tool": "run_command", "arguments": test_arguments, "exit_code": 0, "result": "OK"},
                    {"event_seq": 5, "turn": 5, "tool": "finish_task", "arguments": {"summary": "fixed"}},
                ],
            },
            "trace": {
                "model_calls": 5,
                "tool_calls": 4,
                "max_steps_reached": False,
                "events": [
                    {"event_seq": 3, "turn": 3, "tool": "apply_patch", "write_target": "src/pricing/coupons.py"},
                ],
            },
            "canonical_history": [
                {"role": "assistant", "content": ""},
                {"role": "assistant", "content": ""},
                {"role": "assistant", "content": "", "tool_calls": [{"function": {"arguments": "{\"path\": \"src/pricing/coupons.py\"}"}}]},
                {"role": "assistant", "content": ""},
                {"role": "assistant", "content": ""},
            ],
        }

        analysis = phase23_budget._analysis(result)

        self.assertEqual(analysis["failure_test_name"], "test_decimal_percent")
        self.assertFalse(analysis["next_model_turn_visibly_references_failure"])
        self.assertTrue(analysis["diagnosis_recovery_observed"])
        self.assertEqual(analysis["second_mutation_target"], "src/pricing/coupons.py")


if __name__ == "__main__":
    unittest.main()
