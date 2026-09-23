# Phase 23.2R — Corrected Fixed-Prefix Recovery Rerun

Status: `CORRECTED_RERUNS_COMPLETE`. Runtime files were unchanged. Corrected real model slots: at most one each for 2 and 4 turns, with at most one infrastructure retry per slot.

## Harness bug and repair

The recovery loop returned, but the harness passed a trace-summary `dict` into `phase22_harness._verify`, which reads `CodingTaskTrace.final_answer`, `.max_steps_reached`, and `.summary()`. That postprocessing crash happened before canonical recovery history, TaskState and complete metrics were serialized.

The harness now reconstructs the combined summary as a concrete `main.CodingTaskTrace` before `_verify`, then stores canonical history, combined/recovery traces, TaskState before/start/after, executed-call fingerprints, verifier output and complete metrics/token totals. Missing required evidence or token totals invalidates a run and is classified separately from agent behavior.

## Offline scripted verification

Status: `passed`; provider used: `False`; CodingTaskTrace passed to verifier: `True`; FINISHED result survived JSON write/read: `True`.
Scripted result: status `FINISHED`, accepted `True`, model turns `3`, tools `3`, tokens `36`. Provider failure mock classification: `{'infrastructure_failure': True, 'infrastructure_kind': 'provider', 'valid_real_run': False, 'runtime_errors': ['ConnectionError: mock provider connection failure']}`.

## Fixed prefix integrity

Reused `eval/phase23_budget_results.json -> runs.8.result.canonical_history` unchanged: `28` canonical messages, id `caecc4acb2ce6820`. This is the saved Turn 8 history; no new baseline was run. Its last tool result is the actual failed required-test observation:

```text
Command: python -m unittest discover -s tests -p test_coupons.py -q
CWD: C:\Users\30858\AppData\Local\Temp\phase22-5-e-wgp0s4st\workspace
Exit code: 1
Timed out: false
STDOUT:
<empty>
STDERR:
======================================================================
FAIL: test_one_percent_boundary (test_coupons.CouponTests.test_one_percent_boundary)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\Users\30858\AppData\Local\Temp\phase22-5-e-wgp0s4st\workspace\tests\test_coupons.py", line 14, in test_one_percent_boundary
    self.assertEqual(discounted_amount(100.0, 1), 99.0)
AssertionError: 0.0 != 99.0

----------------------------------------------------------------------
Ran 3 tests in 0.000s

FAILED (failures=1)
```

The prefix TaskState is `LIMIT_REACHED`, event sequence 18, mutation sequence 17. Each run rebuilt the fixture and verified the initial snapshot, replayed the exact Turn 7 canonical patch, and changed only TaskState status to `RUNNING`. The recovery start retained event 18, mutation 17, and all 17 executed-call fingerprints.

## Corrected real runs

| Recovery budget | Attempts | Valid | Final status | Accepted | Recovery model calls | Tools | Tokens | Passing test turns | Finish turn |
|---:|---:|---|---|---|---:|---:|---:|---|---:|
| 2 | 1 | True | LIMIT_REACHED | False | 2 | 2 | 12302 | [10] | None |
| 4 | 1 | True | LIMIT_REACHED | False | 4 | 5 | 27318 | [] | None |

### 2-turn corrected run

- Slot attempt count: `1`; valid: `True`; infrastructure: `None`.
- First post-failure model response: `

`; first action: `apply_patch {"new_text": "    \"\"\"Return the amount after a percentage coupon.\n\n    ``percent`` may be given either as a percentage number (e.g. 10 for\n    10%, or 1 for 1%) or as a decimal fraction strictly between 0 and\n    1 (e.g. 0.10 for 10%). Values ``>= 1`` are treated as percentages,\n    values in ``(0, 1)`` are treated as fractions of the amount.\n    \"\"\"\n    factor = percent if 0 < percent < 1 else percent / 100.0\n    return amount - amount * factor", "old_text": "    \"\"\"Return the amount after a percentage coupon.\n\n    ``percent`` may be given either as a percentage number (e.g. 10 for\n    10%) or as a decimal fraction (e.g. 0.10 for 10%). Values greater\n    than 1 are treated as percentages; values in the [0, 1] range are\n    treated as fractions of the amount.\n    \"\"\"\n    factor = percent / 100.0 if percent > 1 else percent\n    return amount - amount * factor", "path": "src/pricing/coupons.py"}`.
- Failure evidence used: `True` (automatic exact test-name match: `False`); reads/searches/lists after failure: `0` / `0` / `0`.
- Targeted second mutation: `{'turn': 9, 'tool': 'apply_patch', 'arguments': '{"new_text": "    \\"\\"\\"Return the amount after a percentage coupon.\\n\\n    ``percent`` may be given either as a percentage number (e.g. 10 for\\n    10%, or 1 for 1%) or as a decimal fraction strictly between 0 and\\n    1 (e.g. 0.10 for 10%…', 'result': '已应用 patch 到 src/pricing/coupons.py（replaced occurrence count = 1；old_text length = 405；new_text length = 435）', 'exit_code': None, 'event_seq': 19, 'classification': 'PRODUCTIVE', 'action': 'tool_call', 'write_target': 'src/pricing/coupons.py'}`; target: `src/pricing/coupons.py`.
- Required-test attempts: `[{'turn': 10, 'test_name': None, 'exit_code': 0, 'output': 'Command: python -m unittest discover -s tests -p test_coupons.py -q\nCWD: C:\\Users\\30858\\AppData\\Local\\Temp\\phase23-2-recovery-2-o5j9xi0a\\workspace\nExit code: 0\nTimed out: false\nSTDOUT:\n<empty>\nSTDERR:\n----------------------------------------------------------------------\nRan 3 tests in 0.000s\n\nOK\n\n\n[Completion status]\nRequired test succeeded.\nIf all contract requirements are satisfied, call finish_task(summary=...) instead of repeating reads/writes/tests.'}]`; passing turns: `[10]`; artifact result: `PASS`; finish call turn: `None`; interaction completion: `not finished/accepted.
- Recovery model calls/turns: `2` / `2`; tool calls: `2`; recovery tokens: `12302`; extra post-verification tool calls: `None`.
- Full canonical history, combined/recovery traces, TaskState snapshots, metrics, tokens and verifier are saved under `corrected_reruns` in `phase23_2_results.json`.
- Full failure-to-completion tool chain:
- Turn 9: `apply_patch` `{"path": "src/pricing/coupons.py", "old_text": "    \"\"\"Return the amount after a percentage coupon.\n\n    ``percent`` may be given either as a percentage number (e.g. 10 for\n    10%) or as a decimal fraction (e.g. 0.10 for 10%). Values greater\n    than 1 are treated as percentages; values in the [0, 1] range are\n    treated as fractions of the amount.\n    \"\"\"\n    factor = percent / 100.0 if percent > 1 else percent\n    return amount - amount * factor", "new_text": "    \"\"\"Return the amount after a percentage coupon.\n\n    ``percent`` may be given either as a percentage number (e.g. 10 for\n    10%, or 1 for 1%) or as a decimal fraction strictly between 0 and\n    1 (e.g. 0.10 for 10%). Values ``>= 1`` are treated as percentages,\n    values in ``(0, 1)`` are treated as fractions of the amount.\n    \"\"\"\n    factor = percent if 0 < percent < 1 else percent / 100.0\n    return amount - amount * factor"}` → exit `None`; result `已应用 patch 到 src/pricing/coupons.py（replaced occurrence count = 1；old_text length = 405；new_text length = 435）`
- Turn 10: `run_command` `{"command": "python", "args": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_coupons.py", "-q"], "cwd": "."}` → exit `0`; result `Command: python -m unittest discover -s tests -p test_coupons.py -q CWD: C:\Users\30858\AppData\Local\Temp\phase23-2-recovery-2-o5j9xi0a\workspace Exit code: 0 Timed out: false STDOUT: <empty> STDERR: ---------------------------------------------------------------------- Ran 3 tests in 0.000s  OK   [Completion status] Required test succeeded. If all contract requirements are satisfied, call finish_task(summary=...) instead of repeating reads/writ`

### 4-turn corrected run

- Slot attempt count: `1`; valid: `True`; infrastructure: `None`.
- First post-failure model response: `

The boundary is inverted. `1` must mean "1 percent" (=99.0), so values ≤ 1 are percentages and only larger values are fractions.

`; first action: `apply_patch {"new_text": "    \"\"\"Return the amount after a percentage coupon.\n\n    ``percent`` may be given either as a percentage number (e.g. 10 for\n    10%, or 1 for 1%) or as a decimal fraction greater than 1 (e.g. 100\n    for the full amount). Values greater than 1 are treated as decimal\n    fractions of the amount; values in the [0, 1] range are treated as\n    percentages.\n    \"\"\"\n    factor = percent if percent > 1 else percent / 100.0\n    return amount - amount * factor", "old_text": "    \"\"\"Return the amount after a percentage coupon.\n\n    ``percent`` may be given either as a percentage number (e.g. 10 for\n    10%) or as a decimal fraction (e.g. 0.10 for 10%). Values greater\n    than 1 are treated as percentages; values in the [0, 1] range are\n    treated as fractions of the amount.\n    \"\"\"\n    factor = percent / 100.0 if percent > 1 else percent\n    return amount - amount * factor", "path": "src/pricing/coupons.py"}`.
- Failure evidence used: `True` (automatic exact test-name match: `False`); reads/searches/lists after failure: `0` / `2` / `0`.
- Targeted second mutation: `{'turn': 9, 'tool': 'apply_patch', 'arguments': '{"new_text": "    \\"\\"\\"Return the amount after a percentage coupon.\\n\\n    ``percent`` may be given either as a percentage number (e.g. 10 for\\n    10%, or 1 for 1%) or as a decimal fraction greater than 1 (e.g. 100\\n    for the full amoun…', 'result': '已应用 patch 到 src/pricing/coupons.py（replaced occurrence count = 1；old_text length = 405；new_text length = 456）', 'exit_code': None, 'event_seq': 19, 'classification': 'PRODUCTIVE', 'action': 'tool_call', 'write_target': 'src/pricing/coupons.py'}`; target: `src/pricing/coupons.py`.
- Required-test attempts: `[{'turn': 10, 'test_name': 'test_decimal_percent', 'exit_code': 1, 'output': 'Command: python -m unittest discover -s tests -p test_coupons.py -q\nCWD: C:\\Users\\30858\\AppData\\Local\\Temp\\phase23-2-recovery-4-7rbwbnqu\\workspace\nExit code: 1\nTimed out: false\nSTDOUT:\n<empty>\nSTDERR:\n======================================================================\nFAIL: test_decimal_percent (test_coupons.CouponTests.test_decimal_percent)\n----------------------------------------------------------------------\nTraceback (most recent call last):\n  File "C:\\Users\\30858\\AppData\\Local\\Temp\\phase23-2-recovery-4-7rbwbnqu\\workspace\\tests\\test_coupons.py", line 11, in test_decimal_percent\n    self.assertEqual(discounted_amount(100.0, 0.10), 90.0)\nAssertionError: 99.9 != 90.0\n\n======================================================================\nFAIL: test_reported_whole_percent (test_coupons.CouponTests.test_reported_whole_percent)\n-----------------------------------------------------------…'}]`; passing turns: `[]`; artifact result: `no PASS observed`; finish call turn: `None`; interaction completion: `not finished/accepted.
- Recovery model calls/turns: `4` / `4`; tool calls: `5`; recovery tokens: `27318`; extra post-verification tool calls: `None`.
- Full canonical history, combined/recovery traces, TaskState snapshots, metrics, tokens and verifier are saved under `corrected_reruns` in `phase23_2_results.json`.
- Full failure-to-completion tool chain:
- Turn 9: `apply_patch` `{"path": "src/pricing/coupons.py", "old_text": "    \"\"\"Return the amount after a percentage coupon.\n\n    ``percent`` may be given either as a percentage number (e.g. 10 for\n    10%) or as a decimal fraction (e.g. 0.10 for 10%). Values greater\n    than 1 are treated as percentages; values in the [0, 1] range are\n    treated as fractions of the amount.\n    \"\"\"\n    factor = percent / 100.0 if percent > 1 else percent\n    return amount - amount * factor", "new_text": "    \"\"\"Return the amount after a percentage coupon.\n\n    ``percent`` may be given either as a percentage number (e.g. 10 for\n    10%, or 1 for 1%) or as a decimal fraction greater than 1 (e.g. 100\n    for the full amount). Values greater than 1 are treated as decimal\n    fractions of the amount; values in the [0, 1] range are treated as\n    percentages.\n    \"\"\"\n    factor = percent if percent > 1 else percent / 100.0\n    return amount - amount * factor"}` → exit `None`; result `已应用 patch 到 src/pricing/coupons.py（replaced occurrence count = 1；old_text length = 405；new_text length = 456）`
- Turn 10: `run_command` `{"args": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_coupons.py", "-q"], "command": "python", "cwd": "."}` → exit `1`; result `Command: python -m unittest discover -s tests -p test_coupons.py -q CWD: C:\Users\30858\AppData\Local\Temp\phase23-2-recovery-4-7rbwbnqu\workspace Exit code: 1 Timed out: false STDOUT: <empty> STDERR: ====================================================================== FAIL: test_decimal_percent (test_coupons.CouponTests.test_decimal_percent) ---------------------------------------------------------------------- Traceback (most recent call last`
- Turn 11: `apply_patch` `{"path": "src/pricing/coupons.py", "old_text": "    \"\"\"Return the amount after a percentage coupon.\n\n    ``percent`` may be given either as a percentage number (e.g. 10 for\n    10%, or 1 for 1%) or as a decimal fraction greater than 1 (e.g. 100\n    for the full amount). Values greater than 1 are treated as decimal\n    fractions of the amount; values in the [0, 1] range are treated as\n    percentages.\n    \"\"\"\n    factor = percent if percent > 1 else percent / 100.0\n    return amount - amount * factor", "new_text": "    \"\"\"Return the amount after a percentage coupon.\n\n    ``percent`` may be given either as a percentage number (e.g. 10 for\n    10%, or 1 for 1%) or as a decimal fraction (e.g. 0.10 for 10%).\n    Values strictly below 1 are treated as decimal fractions of the\n    amount; values of 1 or greater are treated as percentages.\n    \"\"\"\n    factor = percent if percent < 1 else percent / 100.0\n    return amount - amount * factor"}` → exit `None`; result `已应用 patch 到 src/pricing/coupons.py（replaced occurrence count = 1；old_text length = 456；new_text length = 424）`
- Turn 12: `search_text` `{"query": "discounted_amount"}` → exit `None`; result `query: "discounted_amount" path: . matches_shown: 5 matches_total: 5 truncated: false  src/pricing/coupons.py:4 3 |  4 | def discounted_amount(amount: float, percent: float) -> float: 5 |     """Return the amount after a percentage coupon.  tests/test_coupons.py:3 2 |  3 | from src.pricing.coupons import discounted_amount 4 |   tests/test_coupons.py:8 7 |     def test_reported_whole_percent(self): 8 |         self.assertEqual(discounted_amount(20`
- Turn 12: `search_text` `{"query": "coupon"}` → exit `None`; result `query: "coupon" path: . matches_shown: 2 matches_total: 2 truncated: false  src/pricing/coupons.py:5 4 | def discounted_amount(amount: float, percent: float) -> float: 5 |     """Return the amount after a percentage coupon. 6 |   tests/test_coupons.py:3 2 |  3 | from src.pricing.coupons import discounted_amount 4 | `

## Legacy evidence (not valid runs)

Original attempts remain unchanged under `runs`; their captured stderr excerpts remain under `runs.*.captured_attempt_observations`. They are harness/infrastructure failures, not formal samples.
Provisional old 2-turn stderr: test PASS observed in at least one loop-returning log: `True`; finish observed: `False`. Provisional old 4-turn stderr: `4` loop-returning logs showed FINISHED after three recovery turns. These summaries remain provisional because those attempts never saved canonical recovery history or complete metrics.

## Conclusion and next step

Diagnosis capability: 2-turn corrected run 在固定失败状态中使用失败证据做了针对性二次修改并使 required test PASS，但没有 finish；这是 artifact recovery 成功、interaction completion 未完成。4-turn run 明确识别 1% 边界，首个修正又使 decimal-percent tests 失败；模型随后做了第二次针对性修改，但花掉剩余 turn 搜索，没有再次运行 required test 或 finish。每组仅一个有效样本，不足以说明 4 turns 稳定有效。

Budget semantics: 全局提高 MAX_AGENT_STEPS 会增加从任务开头起的整个 trajectory，早期行为也可能改变；failure 后 bounded recovery allowance 只在相同 required-test failure observation 后增加有限 turns，本实验对照的是这个局部 allowance。多类 budget semantics 还可分别限制普通模型轮数、工具动作与 failure-triggered recovery，但本阶段没有实现任何 Runtime 预算改动。Phase 23.1 的 10/12-step 从头独立运行，仍有 trajectory 混杂。

Bounded recovery direction supported by this sample: `True`. More samples needed: `True`.

唯一建议：下一阶段唯一建议：继续用固定 prefix 研究 bounded recovery allowance，并确保预算覆盖修改、复测和 finish；在获得更多重复样本前不实现 Runtime 预算改动，也不提高全局 MAX。

## Validation

- Full unittest: `passed (exit 0)`
- compileall: `passed (exit 0)`
- git diff --check: `passed (exit 0)`
