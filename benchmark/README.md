# Benchmark v1

Benchmark v1 is a provider-free regression suite for the controlled tool-use
coding agent runtime. It contains 36 repeatable tasks and runs each task in a
fresh temporary workspace through the production Agent Loop, Tool Registry,
approval callback, Coding Contract, Finish Gate, and independent Verifier. The
scripted schedule exercises repository
navigation, literal search, precise edits, multi-file changes, test failure
and repair, bounded reads of long files, path and approval rejection, illegal
modifications, incomplete work, finish gates, duplicate calls, and recovery.

Run it from the repository root:

```powershell
python benchmark/run_benchmark.py --repetitions 2 --output benchmark/reports/benchmark-v1.json
```

The command never calls a provider, reads `.env`, or requires an API key. It
prints a Markdown report and writes the JSON result plus a sibling Markdown
file. Use `--list` to inspect the task IDs, or `--task-id exact-04` to replay
one case. Repetitions are independent workspace resets, so a task can be
replayed without carrying state from an earlier run.

The command exits nonzero when runtime errors or deterministic-oracle
mismatches occur. Oracles check the intended rejection, mutation scope and
recovery evidence as well as the completion verdict. An unrelated refusal
cannot make a security case pass.

## What the numbers mean

The runner supplies a deterministic scripted model schedule to the production
runtime. Its `model_calls` and `tool_calls` are protocol regression
measurements. Scripted replies omit
provider usage, so `Median Tokens` and `P95 Tokens` are reported as `null`; a
separate `median_scripted_tokens` field is only a local diagnostic estimate,
not a provider tokenizer result or a cost estimate. The harness records tool result statuses such as
`policy_rejected`, `tool_error`, `command_failed`, `duplicate_blocked`,
`finish_accepted`, and `finish_rejected` so a change in the runtime can be
diagnosed from the JSON event trace.

Acceptance is split into two layers:

1. **Deterministic acceptance** checks exact fixture state, allowed paths,
   required test exit status, test freshness, path policy, and the finish gate.
   `Acceptance Rate` includes the suite's intentional negative controls, while
   `Positive Acceptance Rate` only includes tasks marked
   `expected_acceptance=true`. A negative control passing means it was
   rejected as designed; it is not counted as an accepted coding task.
2. **Manual review** is a separate queue. Tasks with
   `manual_review=true` need a human to judge semantic explanation quality,
   scope, and whether the recovery reason actually follows from the observed
   failure. The offline runner leaves `manual_review_pass=null` and reports
   the pending count. No machine rule is treated as semantic quality of 100%.

The published metrics are:

| Metric | Definition |
| --- | --- |
| Acceptance Rate | Deterministically accepted rows divided by every attempted row, including runtime errors and negative controls in the denominator |
| Positive Acceptance Rate | Accepted rows with `expected_acceptance=true` divided by all positive rows |
| Median Model Calls | Median scripted turns per non-runtime-error row |
| Median Tool Calls | Median tool invocations per non-runtime-error row |
| Median Tokens | Provider usage median; `null` for scripted replies with unknown usage |
| P95 Tokens | Provider usage nearest-rank 95th percentile; `null` when usage is unknown |
| Max-step Rate | Rows that hit the eight-turn guard |
| Unexpected Modification Rate | Rows with files outside the task's allowed mutation paths |
| Runtime Error Rate | Rows whose harness raised an unexpected runtime error |

The JSON payload keeps the full per-task event list, changed files, finish
attempts, status counts, recovery marker, and runtime error text. A generated
report is an artifact of the exact manifest and repetition count; it does not
replace a real-provider evaluation or a semantic review.

## Task coverage

The manifest is [`tasks/manifest.json`](tasks/manifest.json). Its 36 rows are
grouped as follows:

| Category | Tasks |
| --- | ---: |
| repository navigation | 3 |
| file search | 3 |
| precise modification | 5 |
| multi-file change | 4 |
| test failure and repair | 4 |
| long file | 3 |
| wrong path | 2 |
| permission rejected | 2 |
| illegal modification | 2 |
| cannot complete | 2 |
| finish gate | 3 |
| recovery | 3 |

The fixture source and reset rules are documented in
[`fixtures/README.md`](fixtures/README.md). The benchmark does not copy a
real repository, user files, credentials, or host environment variables into
the temporary workspace.

## Interpreting a run

Use deterministic acceptance to detect regressions in path handling,
permission handling, command execution, mutation scope, and finish ordering.
Use the manual-review queue to inspect semantic quality separately. A passing
benchmark run demonstrates that the scripted offline path still exercises the
runtime boundary; it does not demonstrate that an arbitrary live model will
choose the same tool sequence or solve an unseen repository task.
