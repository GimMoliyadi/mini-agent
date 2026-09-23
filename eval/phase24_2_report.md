# Phase 24.2 — Recovery Closure Stability Study

## Fixed starting point and method

Three independent live continuations replayed canonical prefix `caecc4acb2ce6820` from the same fixture, TaskState, Contract, model/Provider, prompt, tools, `WRITE_ONLY` context, and Phase 24.1 Runtime. Every run reached the same turn 8 required-test FAIL (exit 1), event sequence 18, last mutation sequence 17, and Runtime-granted `FAIL` grace with final model-call limit 11. Each slot succeeded on its first attempt; no Provider or harness retry was needed. No Runtime or Agent behavior was changed. Recovery actions below are classified directly from tool trace, without an LLM judge.

## Three recovery runs

| Run | Turn 9 | Turn 10 | Turn 11 | Calls (model/tool) | Tokens | Patch/write | Read/search | Duplicate blocked | Second mutation | Required test | `finish_task` | Artifact / interaction / self-verified / accepted |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| 1 | EDIT: `apply_patch` | FINISH: rejected; fresh test missing/stale | VERIFY: required test PASS (0) | 3/3 | 19,943 | 1/0 | 0/0 | 0 | No | Ran, PASS | Called, rejected | true / false / true / false |
| 2 | EDIT: `apply_patch` | VERIFY: required test FAIL (1) | EDIT: second `apply_patch` | 3/3 | 19,625 | 2/0 | 0/0 | 0 | Yes | Ran, FAIL; no test after second edit | Not called | false / false / false / false |
| 3 | EDIT: `apply_patch` | VERIFY: required test PASS (0) | FINISH: accepted | 3/3 | 18,774 | 1/0 | 0/0 | 0 | No | Ran, PASS | Called, accepted | true / true / true / true |

All three used the full three-call grace. Aggregate recovery: 9 model calls, 9 tool calls, 58,342 tokens, 4 edits, 3 verifications, 2 finish attempts, 0 reads, 0 searches, 0 duplicate blocks. These are tool actions; the rejected finish remains categorized as FINISH, with its rejection shown explicitly.

## Closure and turn use

- `repair → verify → finish` closed in **1/3** runs; accepted **1/3**.
- Artifact recovered but interaction unfinished in **1/3**: run 1 passed the fresh required test on turn 11 after a premature finish rejection on turn 10, leaving no call for a second finish attempt.
- A required test was called in **3/3**, but a successful test after the **last mutation** occurred in **2/3**. Run 2 had no final verification after its turn 11 repair and its independent final artifact check failed. Thus **1/3** did not reach final verification.
- Recovery turns went to edits (4/9), tests (3/9), and finish attempts (2/9). No new run spent a turn on reading or searching. One finish attempt was premature and consumed a turn without closing the task.
- The Phase 24.1R predecessor had one duplicate-blocked read in its single FAIL continuation. None of these three new samples repeated it. Across the four observed FAIL continuations, duplicate blocking occurred once; current evidence does not show a stable duplicate-guard drain.

## Budget directions

| Direction | Fit to evidence | Limitation |
| --- | --- | --- |
| A. Keep three turns | Sufficient when the model takes EDIT, successful VERIFY, then FINISH (run 3). | Closure was only 1/3 in the new samples. |
| B. Increase the count | One or two extra calls could have let run 1 retry finish and run 2 verify and finish after its second edit, if the revised artifact passed. | The run 2 final artifact check failed; extra calls alone do not ensure correct repair or action order. A fixed count can still end immediately after a mutation or verification. |
| C. Give recovery stages meaning | Explicit repair, fresh verification, and finish opportunities would address the observed stranded states and premature finish ordering. | Requires a separate design and evaluation of transitions and ceilings; this phase does not implement it. |

Current three-turn FAIL grace is **not stable for closure** in this small fixed-prefix sample. The issue is not solely one or two missing calls: run 1 spent a call trying to finish before verifying, while run 2 needed another repair and then lacked a final verification. A fixed number of turns grants opportunity but cannot guarantee `repair → verify → finish` order. The **single next-phase recommendation** is to evaluate direction C as a stage-aware recovery budget design, including its hard ceiling, before any Runtime change.

Full per-turn tool events, token usage, boundary checks, and verifier outcomes are in `phase24_2_results.json`.
