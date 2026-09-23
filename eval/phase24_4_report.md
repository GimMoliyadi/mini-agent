# Phase 24.4 — Stage-Aware Recovery

The normal Coding Task budget remains eight model replies. Only an executed exact required-test FAIL on reply eight starts `Recovery(REPAIR_NEEDED)`; the existing fresh-PASS boundary still has one finish reply. The controller in `recovery.py` consumes actual mutation and exact-test events produced by `run_tool_round`. Its counters are task-local and never renew on a stage transition. Session persistence remains compatible because in-progress loops are not saved; an interrupted loop is recorded as `ERROR`.

| Stage | Decisive event | Next stage |
| --- | --- | --- |
| REPAIR_NEEDED | Successful workspace mutation | VERIFY_NEEDED |
| VERIFY_NEEDED | Executed exact test FAIL / PASS | REPAIR_NEEDED / FINISH_NEEDED |
| FINISH_NEEDED | Further mutation / Gate-accepted finish | VERIFY_NEEDED / FINISHED |

Only actual changed-file mutations consume the two-repair quota. Only executed exact tests in `VERIFY_NEEDED` consume the two-verification quota. A rejected finish in `FINISH_NEEDED` spends the single finish opportunity and stops; earlier rejected finishes consume one nonprogress reply but no finish opportunity. Any reply with no progress consumes one nonprogress allowance, regardless of its tool-call count; a third stops. Acceptance is checked before quotas. Calls 9–15 are the entire recovery envelope; call 16 is never scheduled.

## Validation

- `.venv/Scripts/python.exe eval/phase24_3.py`: scripted cases A–G and saved Phase 24.2 traces passed.
- `.venv/Scripts/python.exe -m unittest discover -s tests -p "test*.py"`: 196 tests, OK, one skipped. Includes recovery transitions, finish protocol, acceptance, session compatibility, normal eight-call limit, fresh-PASS grace, and call-15 finish.
- `.venv/Scripts/python.exe -m compileall .`: passed.
- `git diff --check`: passed.

## Live fixed-prefix validation

Run `.venv/Scripts/python.exe -m eval.phase24_4` with `CONTEXT_MODE=WRITE_ONLY` and `TOOL_APPROVAL_MODE=ALLOW`; the second run uses an extra argument to write `phase24_4_results_2.json`. Both reuse prefix `caecc4acb2ce6820`, reconstructed fixture, and the same required test. The boundary was an actual exact FAIL at call 8. No Provider or harness error occurred.

| Run | Calls 9 onward | Result | Recovery tokens |
| --- | --- | --- | ---: |
| 1 | failed patch → blocked duplicate read → read | LIMIT_REACHED at call 11, accepted false, artifact false | 18,122 |
| 2 | patch → exact FAIL → blocked duplicate read → read → patch → exact PASS → accepted finish | FINISHED at call 15, accepted true, artifact true | 49,192 |

Run 2 ended with two actual repairs, two verifications, two nonprogress replies, and one accepted finish. The independent verifier reported `artifact_passed`, `interaction_completed`, and `agent_self_verified` true. Full traces and canonical histories are in the two JSON result files. Saved prefix replies have no usage values, so the token figures cover the live recovery replies only.
