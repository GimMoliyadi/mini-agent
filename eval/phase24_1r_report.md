# Phase 24.1R — Real Recovery Verification

The Phase 24 study artifacts were committed separately as `52eb481`. Runtime, prompts, tools, Finish Gate, and context policy were not changed in this phase.

## Provider preflight

The Phase 23.1 project proxy override `MINI_AGENT_HTTP_PROXY` was active. The Provider process used HTTP/HTTPS proxy port 9674 with no `ALL_PROXY`; it did not inherit the Codex parent proxy on port 7897. One request with SDK retries disabled returned a valid, nonempty assistant response. No API key or response text was recorded.

## Fixed-prefix FAIL grace

Source prefix: `caecc4acb2ce6820`, 28 canonical messages. Its saved turn 8 exact required test failed with exit 1 in `test_one_percent_boundary`. The local fixture and first eight assistant/tool rounds were replayed. At turn 8, TaskState had event sequence 18 and last mutation sequence 17; Runtime recorded `recovery_grace=FAIL` and `final_model_call_limit=11` before requesting the next model reply.

| Turn | Action | Observation |
| --- | --- | --- |
| 8 | Exact required test | FAIL, exit 1; grace granted once |
| 9 | `apply_patch` | Mutation of `src/pricing/coupons.py` |
| 10 | `read_file` | Duplicate blocked; no new file read |
| 11 | `write_file` | Second mutation of `src/pricing/coupons.py`; hard limit reached |

Three recovery model calls, three recovery tool calls, and 19,466 recovery tokens were observed. There was no recovery test attempt or `finish_task` call. Final status was `LIMIT_REACHED`; `artifact_passed`, `interaction_completed`, `agent_self_verified`, and `accepted` were all false. The attempt had no infrastructure failure. The complete per-call token usage, canonical history, TaskState, and trace are in `phase24_1r_results.json`.

## Fresh-PASS grace

The PASS fixture reused the same saved eight-turn assistant prefix and appended the successful correction from the Phase 23.2 two-turn recovery to turn 7's patch round. The resulting turn 8 exact required test passed with exit 0 after the last mutation. TaskState had event sequence 19, mutation sequence 18, and successful test sequence 19. Runtime recorded `recovery_grace=PASS` and `final_model_call_limit=9` before the live continuation.

Turn 9 called `finish_task`, which the existing Finish Gate accepted. One recovery model call, one recovery tool call, and 6,140 recovery tokens were observed. Final status was `FINISHED`; `artifact_passed`, `interaction_completed`, `agent_self_verified`, and `accepted` were all true. This attempt also had no infrastructure failure.

Both grants occurred only at turn 8. The FAIL run stopped after 11 total model replies; the PASS run finished after 9. Neither requested another model reply beyond its granted limit. Each scenario had one valid live continuation and no retry.
