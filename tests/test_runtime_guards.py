"""任务额度、缺失用量及上下文恢复的确定性回归。"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from runtime_guards import (
    DEFAULT_MAX_BATCH_TOOL_CALLS,
    DEFAULT_MAX_CONTEXT_CHARS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TOTAL_TOKENS,
    DEFAULT_TASK_TIMEOUT_SECONDS,
    BudgetExceeded,
    RunBudget,
    TaskCancelled,
    budget_context,
    get_current_budget,
)


MESSAGES = [{"role": "user", "content": "hello"}]


class ManualClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def reply(prompt=None, completion=None, total=None):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


class RunBudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()

    def budget(self, **settings):
        return RunBudget(clock=self.clock, environ=settings)

    def test_named_defaults_are_loaded_without_external_environment(self):
        limits = self.budget().snapshot()["limits"]
        self.assertEqual(limits, {
            "task_timeout_seconds": DEFAULT_TASK_TIMEOUT_SECONDS,
            "max_tool_calls": DEFAULT_MAX_TOOL_CALLS,
            "max_batch_tool_calls": DEFAULT_MAX_BATCH_TOOL_CALLS,
            "max_context_chars": DEFAULT_MAX_CONTEXT_CHARS,
            "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            "max_total_tokens": DEFAULT_MAX_TOTAL_TOKENS,
        })
        self.assertEqual(self.budget().snapshot()["unknown_usage_policy"], "STOP")

    def test_default_constructor_reads_the_environment(self):
        with patch.dict("os.environ", {"MINI_AGENT_MAX_TOOL_CALLS": "3"}, clear=True):
            self.assertEqual(RunBudget(clock=self.clock).max_tool_calls, 3)

    def test_invalid_positive_integer_settings_fail_with_configuration_name(self):
        names = (
            "MINI_AGENT_MAX_TOOL_CALLS", "MINI_AGENT_MAX_BATCH_TOOL_CALLS",
            "MINI_AGENT_MAX_CONTEXT_CHARS", "MINI_AGENT_MAX_OUTPUT_TOKENS",
            "MINI_AGENT_MAX_TOTAL_TOKENS",
        )
        for name in names:
            for invalid in ("", "0", "-1", "1.5", "nan", "inf", "garbage"):
                with self.subTest(name=name, invalid=invalid):
                    with self.assertRaisesRegex(ValueError, name):
                        self.budget(**{name: invalid})

    def test_deadline_rejects_nonfinite_and_nonpositive_values(self):
        for invalid in ("0", "-1", "nan", "inf", "-inf", "1e999", "bad", ""):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "MINI_AGENT_TASK_TIMEOUT_SECONDS"):
                    self.budget(MINI_AGENT_TASK_TIMEOUT_SECONDS=invalid)

    def test_unknown_usage_policy_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "MINI_AGENT_UNKNOWN_USAGE_POLICY"):
            self.budget(MINI_AGENT_UNKNOWN_USAGE_POLICY="IGNORE")
        self.assertEqual(self.budget(MINI_AGENT_UNKNOWN_USAGE_POLICY=" estimate ").unknown_usage_policy, "ESTIMATE")

    def test_deadline_is_whole_task_not_per_operation(self):
        budget = self.budget(MINI_AGENT_TASK_TIMEOUT_SECONDS="10.5")
        self.clock.advance(10)
        budget.before_tool_call()
        kwargs = budget.before_model_request(MESSAGES)
        self.assertEqual(kwargs["timeout"], 0.5)
        self.clock.advance(0.5)
        with self.assertRaisesRegex(BudgetExceeded, "总时限"):
            budget.check()
        with self.assertRaises(BudgetExceeded):
            budget.before_tool_call()
        self.assertEqual(budget.snapshot()["tool_calls"], 1)
        self.assertEqual(budget.snapshot()["remaining_seconds"], 0)

    def test_cancellation_prevents_every_new_operation(self):
        budget = self.budget()
        budget.cancel()
        operations = (budget.check, budget.before_tool_call, lambda: budget.validate_batch_size(1), lambda: budget.before_model_request(MESSAGES))
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(TaskCancelled):
                    operation()
        snapshot = budget.snapshot()
        self.assertEqual((snapshot["tool_calls"], snapshot["model_requests"]), (0, 0))
        self.assertTrue(snapshot["cancelled"])
        self.assertTrue(issubclass(TaskCancelled, KeyboardInterrupt))

    def test_cancel_has_priority_over_expired_deadline(self):
        budget = self.budget(MINI_AGENT_TASK_TIMEOUT_SECONDS="1")
        self.clock.advance(2)
        budget.cancel()
        with self.assertRaises(TaskCancelled):
            budget.check()

    def test_tool_call_limit_counts_attempts_before_side_effects(self):
        budget = self.budget(MINI_AGENT_MAX_TOOL_CALLS="2")
        executed = []
        for index in range(3):
            try:
                budget.before_tool_call()
            except BudgetExceeded:
                break
            executed.append(index)
        self.assertEqual(executed, [0, 1])
        self.assertEqual(budget.snapshot()["tool_calls"], 2)

    def test_batch_validation_is_side_effect_free(self):
        budget = self.budget(MINI_AGENT_MAX_BATCH_TOOL_CALLS="2", MINI_AGENT_MAX_TOOL_CALLS="3")
        budget.validate_batch_size(0)
        budget.validate_batch_size(2)
        with self.assertRaisesRegex(BudgetExceeded, "单批"):
            budget.validate_batch_size(3)
        self.assertEqual(budget.snapshot()["tool_calls"], 0)
        budget.before_tool_call()
        budget.before_tool_call()
        with self.assertRaisesRegex(BudgetExceeded, "剩余工具"):
            budget.validate_batch_size(2)
        self.assertEqual(budget.snapshot()["tool_calls"], 2)

    def test_batch_size_rejects_invalid_counts(self):
        for count in (-1, 1.5, True, "2", None):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    self.budget().validate_batch_size(count)

    def test_context_limit_includes_structured_tool_arguments(self):
        messages = [{"role": "assistant", "content": None, "tool_calls": [{"function": {"arguments": "x" * 100}}]}]
        budget = self.budget(MINI_AGENT_MAX_CONTEXT_CHARS="100")
        with self.assertRaisesRegex(BudgetExceeded, "上下文字符"):
            budget.before_model_request(messages)
        self.assertEqual(budget.snapshot()["model_requests"], 0)

    def test_exact_context_character_boundary_accepts_unicode(self):
        messages = [{"role": "user", "content": "测试"}]
        serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        budget = self.budget(MINI_AGENT_MAX_CONTEXT_CHARS=str(len(serialized)))
        self.assertEqual(budget.before_model_request(messages)["max_tokens"], DEFAULT_MAX_OUTPUT_TOKENS)
        self.assertGreater(budget.snapshot()["estimated_pending_tokens"], len(serialized))
        with self.assertRaises(BudgetExceeded):
            self.budget(MINI_AGENT_MAX_CONTEXT_CHARS=str(len(serialized) - 1)).before_model_request(messages)

    def test_invalid_context_fails_without_counting_a_request(self):
        for messages in ([{"content": object()}], [{"value": float("nan")}], [{"value": float("inf")}]):
            with self.subTest(messages=messages):
                budget = self.budget()
                with self.assertRaisesRegex(ValueError, "JSON"):
                    budget.before_model_request(messages)
                self.assertEqual(budget.snapshot()["model_requests"], 0)

    def test_output_is_capped_by_single_request_and_remaining_total(self):
        budget = self.budget(MINI_AGENT_MAX_OUTPUT_TOKENS="20", MINI_AGENT_MAX_TOTAL_TOKENS="100")
        kwargs = budget.before_model_request(MESSAGES)
        self.assertEqual(kwargs["max_tokens"], 20)
        budget.record_usage(reply(30, 20, 50))
        serialized = json.dumps(MESSAGES, ensure_ascii=False, separators=(",", ":"))
        next_kwargs = budget.before_model_request(MESSAGES)
        self.assertEqual(next_kwargs["max_tokens"], 50 - len(serialized.encode("utf-8")))
        self.assertEqual(budget.snapshot()["model_requests"], 2)

    def test_model_request_is_rejected_when_even_prompt_does_not_fit(self):
        budget = self.budget(MINI_AGENT_MAX_TOTAL_TOKENS="10")
        with self.assertRaisesRegex(BudgetExceeded, "累计token"):
            budget.before_model_request(MESSAGES)
        self.assertEqual(budget.snapshot()["model_requests"], 0)

    def test_known_usage_is_recorded_before_rejecting_further_requests(self):
        budget = self.budget(MINI_AGENT_MAX_TOTAL_TOKENS="100")
        budget.before_model_request(MESSAGES)
        budget.record_usage(reply(80, 40, 120))
        snapshot = budget.snapshot()
        self.assertEqual((snapshot["prompt_tokens"], snapshot["completion_tokens"], snapshot["total_tokens"]), (80, 40, 120))
        self.assertEqual(snapshot["known_token_sum"], 120)
        with self.assertRaises(BudgetExceeded):
            budget.before_model_request(MESSAGES)

    def test_unknown_usage_is_not_reported_as_zero_and_default_policy_stops(self):
        budget = self.budget()
        reservation = budget.before_model_request(MESSAGES)
        budget.record_usage(reply())
        snapshot = budget.snapshot()
        self.assertIsNone(snapshot["prompt_tokens"])
        self.assertIsNone(snapshot["completion_tokens"])
        self.assertIsNone(snapshot["total_tokens"])
        self.assertEqual(snapshot["known_token_sum"], 0)
        self.assertEqual(snapshot["unknown_usage_responses"], 1)
        self.assertGreaterEqual(snapshot["estimated_unknown_tokens"], reservation["max_tokens"])
        self.assertIn("不是精确token", snapshot["token_accounting_note"])
        with self.assertRaisesRegex(BudgetExceeded, "未返回完整用量"):
            budget.before_model_request(MESSAGES)

    def test_missing_reported_total_can_be_derived_from_known_components(self):
        budget = self.budget()
        budget.before_model_request(MESSAGES)
        budget.record_usage(reply(10, 5))
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["known_token_sum"], 15)
        self.assertEqual(snapshot["unknown_usage_responses"], 0)
        self.assertIsNone(snapshot["total_tokens"])
        budget.before_model_request(MESSAGES)

    def test_known_total_does_not_require_prompt_and_completion_fields(self):
        budget = self.budget()
        budget.before_model_request(MESSAGES)
        budget.record_usage(reply(total=15))
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["total_tokens"], 15)
        self.assertIsNone(snapshot["prompt_tokens"])
        self.assertEqual(snapshot["unknown_usage_responses"], 0)
        budget.before_model_request(MESSAGES)

    def test_known_components_cannot_be_hidden_by_a_smaller_reported_total(self):
        budget = self.budget()
        budget.before_model_request(MESSAGES)
        budget.record_usage(reply(20, 10, 5))
        self.assertEqual(budget.snapshot()["known_token_sum"], 30)
        self.assertEqual(budget.snapshot()["total_tokens"], 5)

    def test_estimate_policy_charges_unknown_replies_and_exhausts_budget(self):
        budget = self.budget(MINI_AGENT_MAX_TOTAL_TOKENS="100", MINI_AGENT_MAX_OUTPUT_TOKENS="20", MINI_AGENT_UNKNOWN_USAGE_POLICY="ESTIMATE")
        first = budget.before_model_request(MESSAGES)
        budget.record_usage(reply())
        first_snapshot = budget.snapshot()
        self.assertGreater(first_snapshot["estimated_unknown_tokens"], first["max_tokens"])
        second = budget.before_model_request(MESSAGES)
        self.assertLess(second["max_tokens"], first["max_tokens"])
        budget.record_usage(reply())
        self.assertEqual(budget.snapshot()["accounted_tokens"], 100)
        self.assertEqual(budget.snapshot()["unknown_usage_responses"], 2)
        with self.assertRaises(BudgetExceeded):
            budget.before_model_request(MESSAGES)

    def test_partial_unknown_usage_adds_known_and_estimated_parts_separately(self):
        budget = self.budget(MINI_AGENT_MAX_OUTPUT_TOKENS="20", MINI_AGENT_UNKNOWN_USAGE_POLICY="ESTIMATE")
        budget.before_model_request(MESSAGES)
        budget.record_usage(reply(prompt=60))
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["known_token_sum"], 60)
        self.assertEqual(snapshot["estimated_unknown_tokens"], 20)
        self.assertEqual(snapshot["accounted_tokens"], 80)
        self.assertEqual(snapshot["prompt_tokens"], 60)
        self.assertIsNone(snapshot["total_tokens"])

    def test_malformed_usage_is_unknown_instead_of_false_precision(self):
        for value in (-1, True, 1.5, "10", float("nan")):
            with self.subTest(value=value):
                budget = self.budget()
                budget.before_model_request(MESSAGES)
                budget.record_usage(reply(value, value, value))
                snapshot = budget.snapshot()
                self.assertEqual(snapshot["unknown_usage_responses"], 1)
                self.assertIsNone(snapshot["total_tokens"])

    def test_zero_usage_is_known_not_missing(self):
        budget = self.budget()
        budget.before_model_request(MESSAGES)
        budget.record_usage(reply(0, 0, 0))
        self.assertEqual(budget.snapshot()["unknown_usage_responses"], 0)
        self.assertEqual(budget.snapshot()["total_tokens"], 0)
        budget.before_model_request(MESSAGES)

    def test_pending_request_remains_reserved_and_cannot_be_silently_retried(self):
        budget = self.budget()
        budget.before_model_request(MESSAGES)
        snapshot = budget.snapshot()
        self.assertIsNone(snapshot["total_tokens"])
        self.assertEqual(snapshot["pending_responses"], 1)
        self.assertGreater(snapshot["estimated_pending_tokens"], 0)
        with self.assertRaisesRegex(BudgetExceeded, "尚未完成用量记账"):
            budget.before_model_request(MESSAGES)
        self.assertEqual(budget.snapshot()["model_requests"], 1)
        budget.record_usage(None)
        self.assertEqual(budget.snapshot()["unknown_usage_responses"], 1)

    def test_duplicate_or_unpaired_usage_is_rejected(self):
        budget = self.budget()
        with self.assertRaisesRegex(ValueError, "待记账"):
            budget.record_usage(reply(1, 1, 2))
        budget.before_model_request(MESSAGES)
        budget.record_usage(reply(1, 1, 2))
        with self.assertRaises(ValueError):
            budget.record_usage(reply(1, 1, 2))
        self.assertEqual(budget.snapshot()["usage_responses"], 1)

    def test_usage_after_cancel_or_deadline_is_still_honestly_recorded(self):
        budget = self.budget(MINI_AGENT_TASK_TIMEOUT_SECONDS="1")
        budget.before_model_request(MESSAGES)
        self.clock.advance(2)
        budget.cancel()
        budget.record_usage(reply(10, 5, 15))
        self.assertEqual(budget.snapshot()["total_tokens"], 15)
        with self.assertRaises(TaskCancelled):
            budget.check()

    def test_snapshot_does_not_expose_mutable_accounting_state(self):
        budget = self.budget()
        snapshot = budget.snapshot()
        snapshot["known_usage"]["total_tokens"] = 99
        snapshot["missing_usage_fields"]["total_tokens"] = 99
        snapshot["limits"]["max_tool_calls"] = 99
        self.assertEqual(budget.snapshot()["known_usage"]["total_tokens"], 0)
        self.assertEqual(budget.snapshot()["limits"]["max_tool_calls"], DEFAULT_MAX_TOOL_CALLS)


class BudgetContextTests(unittest.TestCase):
    def test_nested_context_restores_outer_budget_even_on_cancellation(self):
        outer, inner = RunBudget(environ={}), RunBudget(environ={})
        self.assertIsNone(get_current_budget())
        with budget_context(outer) as current:
            self.assertIs(current, outer)
            with self.assertRaises(TaskCancelled):
                with budget_context(inner):
                    self.assertIs(get_current_budget(), inner)
                    inner.cancel()
                    inner.check()
            self.assertIs(get_current_budget(), outer)
        self.assertIsNone(get_current_budget())

    def test_async_tasks_have_separate_active_budgets(self):
        async def run():
            first, second = RunBudget(environ={}), RunBudget(environ={})
            release = asyncio.Event()

            async def worker(budget):
                with budget_context(budget):
                    await release.wait()
                    return get_current_budget()

            tasks = [asyncio.create_task(worker(first)), asyncio.create_task(worker(second))]
            release.set()
            self.assertEqual(await asyncio.gather(*tasks), [first, second])
            self.assertIsNone(get_current_budget())

        asyncio.run(run())


class ModelRequestBoundaryTests(unittest.TestCase):
    def test_context_serialization_time_cannot_outlive_deadline(self):
        clock = ManualClock()
        budget = RunBudget(clock=clock, environ={"MINI_AGENT_TASK_TIMEOUT_SECONDS": "1"})
        original = json.dumps

        def delayed_serialization(*args, **kwargs):
            clock.advance(1)
            return original(*args, **kwargs)

        with patch("runtime_guards.json.dumps", side_effect=delayed_serialization):
            with self.assertRaises(BudgetExceeded):
                budget.before_model_request(MESSAGES)
        self.assertEqual(budget.snapshot()["model_requests"], 0)
        self.assertEqual(budget.snapshot()["pending_responses"], 0)

    def test_unencodable_context_is_a_clear_error_without_request_count(self):
        budget = RunBudget(environ={})
        with self.assertRaisesRegex(ValueError, "Unicode"):
            budget.before_model_request([{"role": "user", "content": chr(0xD800)}])
        self.assertEqual(budget.snapshot()["model_requests"], 0)
