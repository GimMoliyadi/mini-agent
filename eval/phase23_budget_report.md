# Phase 23 — Diagnosis Recovery & Step-Budget Semantics

Experiment status: `infrastructure_blocked`.
Baseline commit: `950b235`.

## Phase 22.5 baseline and runtime semantics

Phase 22.5 E used the diagnosis task, the coupon fixture, `test_coupons.py`, WRITE_ONLY context, ALLOW approval, and `sensenova-6.8-flash-lite`. Turn 7 applied the first mutation. Turn 8 ran the exact required test and failed only `test_one_percent_boundary` (`0.0 != 99.0`). The failed tool result was appended to canonical history. The 8-model-call budget was then exhausted, so no Turn 9 model response consumed that observation. Baseline: accepted `False`, 8 model calls, 18 tool calls, 30,421 tokens, one required-test attempt, one failure, no finish call.

`MAX_AGENT_STEPS` counts model requests/responses (the loop iteration), not individual tools. A model response can request and execute multiple tool calls; the final allowed response is processed through its tool calls before the loop sets `LIMIT_REACHED`, unless a `finish_task` succeeds first. Therefore the last allowed tool result can enter history without a following model request. This is exactly what happened after Turn 8's test failure.

Prior controls: Phase 22 accepted 6/6 tasks in [4, 6, 6, 7, 7, 6] model calls, with 0 budget hits; Phase 22.5 navigation accepted 3/3 in [8, 5, 5] calls. N1 used all 8 calls and still finished because the final `finish_task` succeeded. These samples do not show systematic cutoff of ordinary tasks.

## 8 / 10 / 12 step comparison

| Budget | Source | Accepted | Model calls | Tool calls | Tokens | Required test attempts / failures | PASS turn | Finish turn | Extra tools after PASS |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 | Phase 22.5 trace | False | 8 | 18 | 30421 | 1 / 1 | — | — | — |
| 10 | 2 Phase 23 attempt(s) | False | 0 | 0 | 0 | 0 / 0 | — | — | — |
| 12 | 2 Phase 23 attempt(s) | False | 0 | 0 | 0 | 0 / 0 | — | — | — |

## Diagnosis chains

### Run B — MAX_AGENT_STEPS=10

Infrastructure retry: 1 prior attempt(s), recorded separately and excluded from Agent behavior.

Infrastructure failure: `provider` — `APIConnectionError: Connection error.`
No model response was received: first mutation, first required-test failure, post-failure read/search, second mutation, PASS, and finish turn are all absent. Required-test attempts/failures: 0/0; accepted: `False`; completed model calls/tool calls/tokens: 0/0/0.
Exploration redundancy and post-PASS behavior are not assessable because neither run reached a PASS.

### Run C — MAX_AGENT_STEPS=12

Infrastructure retry: 1 prior attempt(s), recorded separately and excluded from Agent behavior.

Infrastructure failure: `provider` — `APIConnectionError: Connection error.`
No model response was received: first mutation, first required-test failure, post-failure read/search, second mutation, PASS, and finish turn are all absent. Required-test attempts/failures: 0/0; accepted: `False`; completed model calls/tool calls/tokens: 0/0/0.
Exploration redundancy and post-PASS behavior are not assessable because neither run reached a PASS.

## Provider connectivity and evidence limits

Both budgets attempted one initial provider request and one infrastructure retry. Both attempts for each budget returned `APIConnectionError: Connection error.` before a model response; no Agent behavior, test run, mutation, or recovery chain was observed. The run used HTTPS proxy port `7897`; the local TCP check found port 7897 accepting connections=`False` and port 9674 accepting connections=`True`. The provider endpoint was HTTPS and non-loopback. This points to the configured proxy endpoint as the connection blocker.

The first driver version sent the child JSON to stderr; its parser mislabeled the provider failures as harness errors. Output capture was fixed after both configured attempts per budget had ended. The captured child result contained APIConnectionError and only initial system/user messages, with no assistant response.

## Interpretation

A recovery is supported only when the trace shows that the model received the failed test result, made a later mutation that addresses the reported boundary, then obtained a fresh required-test PASS and finished. The next assistant message and its tool arguments are the observable evidence; hidden reasoning is not inferred. Any unrelated post-failure reads/searches and calls after PASS are reported from the trace.

## Candidate designs

| Design | Complexity | Predictability | Token cost | Extension risk | Preserves hard cap? | Fit to observed issue |
| --- | --- | --- | --- | --- | --- | --- |
| A. Raise the default cap | Lowest: change one constant | Simple global ceiling, but no work-type semantics | Raises worst-case model turns for every task (10 is +25% vs 8; 12 is +50%); token growth also depends on history size | Still bounded; another end-of-budget observation can be stranded | Yes, at the new higher ceiling | May work if the tested recovery chain fits; does not reserve a chance to consume the last observation |
| B. One bounded recovery turn after a final failure observation | Moderate: classify eligible results and track one exception | Precise for the diagnosed failure path | At most one additional model response per eligible terminal failure | Low if the exception is consumed once and never renews | Yes, with a strict one-turn exception | Directly targets Turn 8 FAIL → no Turn 9; may be insufficient if recovery needs several model turns |
| C. Explicit model/action/recovery budgets | Highest: define and enforce multiple counters | Most explicit when each counter and total ceiling is specified | Controllable by category; can avoid paying recovery budget on ordinary tasks | Depends on a nonrenewable global ceiling; otherwise easy to extend indefinitely | Yes, if a hard total ceiling remains | Clarifies the semantic mismatch, but only solves this case if recovery budget is allocated after failure |

## Conclusion

- Diagnosis capability: UNRESOLVED: neither B nor C produced a valid model response
- Phase 22.5 budget finding: CONFIRMED: Turn 8's failure result was recorded after the eighth model response, then LIMIT_REACHED prevented a ninth response
- Systematic 8-step truncation: NOT SHOWN in current historical samples: 6/6 Phase 22 tasks and 3/3 Phase 22.5 navigation tasks completed; one successful run used exactly 8 calls. This does not remove the demonstrated terminal-observation truncation path.
- Budget truncation evidence: The mechanism is proven; historical samples do not show systematic truncation, while the larger-budget recovery effect remains unmeasured.
- Larger-budget effect: UNTESTED: one or both larger-budget runs failed before model behavior was observed
- Single next recommendation: Restore provider connectivity, then rerun the same 10-step and 12-step experiment; make no Runtime or diagnosis-capability changes until valid traces exist.

## Validation

- `python -m unittest discover -s tests -p test*.py`: PASS (exit 0)
- `python -m compileall .`: PASS (exit 0)
- `git diff --check`: PASS (exit 0)

## Phase 23.1 — Provider recovery and budget rerun

Status: `completed`.

### Provider proxy isolation

The Eval parent proxy environment is recorded separately from the effective Provider child environment:

- Parent HTTP/HTTPS/ALL: `{'configured': True, 'scheme': 'http', 'loopback': True, 'port': 7897}` / `{'configured': True, 'scheme': 'http', 'loopback': True, 'port': 7897}` / `{'configured': True, 'scheme': 'socks5', 'loopback': True, 'port': 7897}`
- Provider child HTTP/HTTPS/ALL: `{'configured': True, 'scheme': 'http', 'loopback': True, 'port': 9674}` / `{'configured': True, 'scheme': 'http', 'loopback': True, 'port': 9674}` / `{'configured': False, 'scheme': None, 'loopback': False, 'port': None}`
- Project override configured: `True`; Codex/global proxy settings changed: `False`.

### Provider preflight

- Status: `pass`; valid assistant response: `True`; completed request: `True`.
- Provider scheme/model: `https` / `sensenova-6.8-flash-lite`; SDK retries: `0`.
- Response text and API key were not recorded. The check accepts any non-empty assistant response; it does not require the text `OK`.

- Earlier 7897 source finding: The Phase 23 parent launch explicitly assigned the 7897 proxy values under the supplied network instruction; the child inherited them.

Earlier preflight observations retained: 1; the earlier exact-`OK` mismatch is historical and is not the current acceptance rule.

### Budget comparison

| Budget | Run status | Accepted | Model calls | Tool calls | Tokens | Required tests / failures | PASS turn(s) | Finish turn | Max steps reached |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| 8 | Reused Phase 22.5 baseline | False | 8 | 18 | 30421 | 1 / 1 | — | — | True |
| 10 | Valid run (1 attempt(s)) | True | 9 | 12 | 32973 | 2 / 1 | [8] | 9 | False |
| 12 | Valid run (1 attempt(s)) | False | 12 | 15 | 47106 | 2 / 2 | — | — | True |

### Diagnosis traces

#### MAX_AGENT_STEPS=10

- First mutation turn: 5; first required-test failure turn: 6.
- Required test failure at turn 6 (test_decimal_percent):
```text
Command: python -m unittest discover -s tests -p test_coupons.py -q
CWD: C:\Users\30858\AppData\Local\Temp\phase23-budget-10-zbibv16o\workspace
Exit code: 1
Timed out: false
STDOUT:
<empty>
STDERR:
======================================================================
FAIL: test_decimal_percent (test_coupons.CouponTests.test_decimal_percent)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\Users\30858\AppData\Local\Temp\phase23-budget-10-zbibv16o\workspace\tests\test_coupons.py", line 11, in test_decimal_percent
    self.assertEqual(discounted_amount(100.0, 0.10), 90.0)
AssertionError: 99.9 != 90.0

----------------------------------------------------------------------
Ran 3 tests in 0.000s

FAILED (failures=1)

```
  Immediate next model turn: 7; explicitly references the failing test: `False`; actions: `['Turn 7 `apply_patch` target `src/pricing/coupons.py`']`.
- Required test PASS at turn 8.
- First model turn after the first failure: 7; explicitly names the failing test: `False`.
- First later explanatory assistant turn: none; content: `none`.
- Post-failure list/search/read calls: `[]`.
- Second mutation: Turn 7 `apply_patch` target `src/pricing/coupons.py`.
- All post-failure mutations: `['Turn 7 `apply_patch` target `src/pricing/coupons.py`']`.
- Required-test attempts/failures: 2/1; PASS turn(s): [8].
- Finish turn/attempts: 9/1; accepted: `True`.
- Post-verification extra tool calls: 0; max steps reached: `False`.

#### MAX_AGENT_STEPS=12

- First mutation turn: 5; first required-test failure turn: 6.
- Required test failure at turn 6 (test_one_percent_boundary):
```text
Command: python -m unittest discover -s tests -p test_coupons.py -q
CWD: C:\Users\30858\AppData\Local\Temp\phase23-budget-12-v74219fo\workspace
Exit code: 1
Timed out: false
STDOUT:
<empty>
STDERR:
======================================================================
FAIL: test_one_percent_boundary (test_coupons.CouponTests.test_one_percent_boundary)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\Users\30858\AppData\Local\Temp\phase23-budget-12-v74219fo\workspace\tests\test_coupons.py", line 14, in test_one_percent_boundary
    self.assertEqual(discounted_amount(100.0, 1), 99.0)
AssertionError: 0.0 != 99.0

----------------------------------------------------------------------
Ran 3 tests in 0.000s

FAILED (failures=1)

```
  Immediate next model turn: 7; explicitly references the failing test: `False`; actions: `['Turn 7 `apply_patch` target `src/pricing/coupons.py`']`.
- Required test failure at turn 10 (test_one_percent_boundary):
```text
Command: python -m unittest discover -s tests -p test_coupons.py -q
CWD: C:\Users\30858\AppData\Local\Temp\phase23-budget-12-v74219fo\workspace
Exit code: 1
Timed out: false
STDOUT:
<empty>
STDERR:
======================================================================
FAIL: test_one_percent_boundary (test_coupons.CouponTests.test_one_percent_boundary)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\Users\30858\AppData\Local\Temp\phase23-budget-12-v74219fo\workspace\tests\test_coupons.py", line 14, in test_one_percent_boundary
    self.assertEqual(discounted_amount(100.0, 1), 99.0)
AssertionError: 0.0 != 99.0

----------------------------------------------------------------------
Ran 3 tests in 0.000s

FAILED (failures=1)

```
  Immediate next model turn: 11; explicitly references the failing test: `False`; actions: `['Turn 11 `read_file` {"path": "tests/test_coupons.py"}', 'Turn 11 `read_file` {"path": "src/pricing/coupons.py"}']`.
- First model turn after the first failure: 7; explicitly names the failing test: `False`.
- First later explanatory assistant turn: 12; content: `

The test `test_one_percent_boundary` expects `discounted_amount(100.0, 1)` to equal 99.0 (i.e., 1 is interpreted as 1%). My threshold `percent > 1` was wrong — it should be `percent < 1` (strictly less than 1) to treat values as fractions; `1` should be treated as whole-percent 1.

`.
- Post-failure list/search/read calls: `['Turn 11 `read_file` {"path": "tests/test_coupons.py"}', 'Turn 11 `read_file` {"path": "src/pricing/coupons.py"}']`.
- Second mutation: Turn 7 `apply_patch` target `src/pricing/coupons.py`.
- All post-failure mutations: `['Turn 7 `apply_patch` target `src/pricing/coupons.py`', 'Turn 8 `apply_patch` target `src/pricing/coupons.py`', 'Turn 9 `apply_patch` target `src/pricing/coupons.py`', 'Turn 12 `apply_patch` target `src/pricing/coupons.py`']`.
- Required-test attempts/failures: 2/2; PASS turn(s): none.
- Finish turn/attempts: none/0; accepted: `False`.
- Post-verification extra tool calls: None; max steps reached: `True`.

### Current conclusion

- Diagnosis recovery observed: `True`.
- Next step: The 10-step run was accepted after 9 model calls; the 12-step run used its full budget and did not finish verification. These single runs do not justify changing the Runtime or selecting a new default budget.
- Full child traces, canonical messages, tool chains, and metrics are retained in `phase23_budget_results.json`.
