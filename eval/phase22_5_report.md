# Phase 22.5 — Navigation & Diagnosis Forensics

## Navigation

| Task | First correct source turn | list | search | read | Before source: list/search/read | Tools | Tokens | Accepted |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- |
| N1 | 3 | 3 | 2 | 1 | 3/0/0 | 10 | 25816 | True |
| N2 | 1 | 1 | 1 | 4 | 1/0/0 | 9 | 18161 | True |
| N3 | 2 | 1 | 2 | 0 | 1/0/0 | 7 | 15242 | True |

N1 listed three directories before searching for shipping/subtotal. N2 searched for discount on turn 1 and then traced the summary/pricing files. N3 searched for credit after one root list. No task repeated a directory listing. N2 read the unrelated coupons module once; the other navigation tasks had no clearly unrelated reads. Search was effective in all three tasks. There is no consistent blind traversal or repeated listing pattern across tasks, so repository navigation is not sufficiently supported as Phase 23.

## Diagnosis recovery

Deterministic fixture check: baseline failed, the plausible first normalization (`percent > 1`) failed only `test_one_percent_boundary` (`0.0 != 99.0`), and the complete boundary-aware fix passed.

Real tool chain:

- 1. `list_files` — {}
- 2. `list_files` — {"path": "src"}
- 2. `list_files` — {"path": "tests"}
- 3. `read_file` — {"path": "tests/test_coupons.py"}
- 3. `list_files` — {"path": "src/pricing"}
- 3. `list_files` — {"path": "src/orders"}
- 4. `read_file` — {"path": "src/pricing/coupons.py"}
- 5. `search_text` — {"query": "percent"}
- 5. `read_file` — {"path": "src/pricing/totals.py"}
- 5. `read_file` — {"path": "src/pricing/loyalty.py"}
- 5. `read_file` — {"path": "src/pricing/catalog.py"}
- 5. `read_file` — {"path": "src/pricing/rounding.py"}
- 6. `read_file` — {"path": "tests/test_loyalty_invoice.py"}
- 6. `read_file` — {"path": "tests/test_summary.py"}
- 6. `read_file` — {"path": "src/orders/summary.py"}
- 6. `read_file` — {"path": "src/orders/invoice.py"}
- 7. `apply_patch` — {"path": "src/pricing/coupons.py", "old_text": "    \"\"\"Return the amount after a percentage coupon.\"\"\"\n    return amount - percent", "new_text": "    \"\"\"Return the amount after a percentage coupon.\n\n    ``percent`` may be given …
- 8. `run_command` — {"command": "python", "args": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_coupons.py", "-q"], "cwd": "."}

First mutation: turn 7. The patch used `percent > 1` and treated the `[0, 1]` range as decimal fractions, despite the model having read the `1%` test on turn 3. Required test: turn 8, exit 1. It reported `test_one_percent_boundary` with `0.0 != 99.0`. The failure was appended to canonical history, but no next model call occurred because the agent reached the frozen eight-step limit. Thus the model did not see the failure in a subsequent inference. There was no second diagnosis, second mutation, passing test, or accepted finish.

Metrics: required_test_attempts=1, mutation_count=1, accepted=False, tokens=30421, tool_calls=18.

## Phase 23 candidate

Diagnosis recovery within the current step budget is the only candidate for further study. The broad read phase consumed turns 3–6, including several unrelated pricing/order files after the relevant test and source were read. Phase 22.5 does not prove recovery succeeds. No Runtime change was made.

## Validation

Full unittest suite: 175 tests, 0 failed, 1 skipped. `python -m compileall -q eval tests`: passed. `git diff --check`: passed.
