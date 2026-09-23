# Phase 24 — Observation-Aware Budget Semantics Study

Scope: source analysis and provider-free state simulation only. Baseline `a4b9b99`; Agent Runtime, tool schema, acceptance rules, and `MAX_AGENT_STEPS` were not changed. The simulation in `eval/phase24_budget.py` is a proposed policy model, not a Runtime test or a new real-model sample.

## Current budget semantics

`config.py` sets `MAX_AGENT_STEPS = 8`. The first model reply is obtained before `run_agent_loop`; the loop then enumerates that reply as turn 1 and asks for each later reply only after the previous round. A turn is one completed model reply, regardless of how many tool calls it requests. `CodingTaskTrace.record_model_turn` increments its counter at the start of each loop iteration. A failed model request before a reply is an error, not a completed model turn.

For a tool reply, `run_tool_round` first appends the assistant message with tool calls through the first control-flow call. It then processes each selected call in order and appends a matching `role=tool` result, including duplicate and policy rejection results. `finish_task` is dispatched through the Finish Gate and ends that round; later calls in the same assistant reply are not included in canonical history. A successful gate sets `TaskState.status = FINISHED`. Back in `run_agent_loop`, `FINISHED` is checked **before** the step-limit check, so an accepted `finish_task` on turn 8 wins and `max_steps_reached` is not set. A rejected finish can still reach the limit.

For every other tool round, the limit check happens **after** the assistant call and all selected tool results are appended. On turn 8 it sets `LIMIT_REACHED` and returns before calling `ask` again. The canonical messages are later saved with the task state. `build_model_context` only prepares a compressed outbound view and is called before an *allowed next* `ask`; saved canonical history remains complete. A no-tool Coding Task answer is also appended but does not finish the task: below the limit, a Runtime user notice asks for `finish_task`; at the limit, it becomes `LIMIT_REACHED` without another request. Ordinary chat with a no-tool answer returns immediately.

Sources: `main.py` (`run_agent_loop`, `run_tool_round`, `tool_calls_through_control_flow`, `CodingTaskTrace`), `acceptance.py` (`TaskState`, `evaluate_finish_request`), `config.py`.

## Three observed terminal paths

The following are saved real traces, not outputs of the Phase 24 simulation. Phase 23.2 uses the unchanged Phase 22.5 28-message prefix (ID `caecc4acb2ce6820`) and resumes with a fresh local fixture. Recovery turn 1 is global turn 9.

```text
Phase 22.5 E, base limit 8
T7 apply_patch (mutation seq 17)
  → T8 exact required test FAIL, exit 1 (event seq 18)
  → assistant/tool result saved (28 canonical messages)
  → LIMIT_REACHED; no T9 model call; failure not consumed

Phase 23.2 corrected 2-turn recovery, local limit 2
fixed FAIL prefix → T9 apply_patch (seq 19)
  → T10 exact required test PASS, exit 0 (seq 20)
  → PASS result and completion hint saved
  → LIMIT_REACHED; no T11 finish_task; artifact passed, interaction incomplete

Phase 23.2 corrected 4-turn recovery, local limit 4
fixed FAIL prefix → T9 apply_patch (seq 19)
  → T10 exact required test FAIL, exit 1 (seq 20)
  → T11 apply_patch (seq 21)
  → T12 search_text ×2 (seq 22–23)
  → both search results saved
  → LIMIT_REACHED; no T13 model call; no post-second-patch test or finish
```

The shared defect is a **terminal observation without a consumption opportunity**. The observation may report a failure, prove a fresh pass, or contain ordinary search data. This mechanism alone does not prove that more calls would yield success. In particular, the corrected 4-turn run has no observed PASS after its second patch. Phase 23.1's separate 10-call run finished in 9 calls, whereas its 12-call run hit the limit after failures; those are different trajectories, not a controlled proof that a larger cap cures the issue.

## Candidate comparison

| Candidate | Benefit | Cost / boundary | Decision |
| --- | --- | --- | --- |
| A. Raise global `MAX_AGENT_STEPS` | One constant; retains a fixed cap | Pays extra calls on every trajectory, changes behavior from the start, and the new last turn can still produce an unconsumed observation. Phase 23.1's 10/12 runs diverged. | Do not choose for Phase 24. |
| B. Observation-aware grace | Makes a specific terminal test observation consumable without enlarging ordinary read/search loops | Requires deterministic classification plus a one-shot allowance. It cannot promise recovery when the hard ceiling is reached or the model spends grace on other actions. | Recommend as the smallest implementation candidate. |
| C. Explicit Recovery State / Recovery Budget | Expresses normal and recovery costs separately; useful if later traces show multi-stage recovery needs | More state, persistence, resume, and accounting rules; still needs an absolute ceiling and finish precedence. | Defer until evidence requires it. |

## Recommended minimal candidate: one-shot terminal test grace

Proposed *soft* normal budget: 8 model calls. Proposed *absolute* ceiling: 11 total model calls. At the end of normal turn 8, after the complete Tool Round and the `FINISHED` check, inspect the **last tool result in that round**. It must come from an actually executed exact required-test call. Grant exactly once, only for a Coding Task:

- **FAIL:** The call exactly matches `contract.test_command`; permission allowed it; it really executed (not a duplicate); it returned a parsed nonzero exit code with `Timed out: false`. Grant up to 3 more calls total, through call 11. This gives a minimal scripted `patch → retest → finish` path after consuming the failure. A failed test earlier than the boundary gets no special grant because ordinary calls remain.
- **Fresh PASS:** The same exact/allowed/executed check yields exit 0; its event sequence is newer than the last actual workspace mutation, with no later mutation in that Tool Round. Grant one additional call, through call 9, to allow `finish_task`. This is an opportunity, not automatic acceptance: the existing Finish Gate remains authoritative.
- **Ordinary read/search:** No grant, including successful output and a round containing multiple searches. Mere novelty is not a reliable deterministic trigger.
- **Failed patch:** No grant. A tool failure by itself does not establish required-test recovery or a verified artifact.
- **Policy rejection:** No grant. A denied or blocked action must not turn into budget extension. Duplicate-blocked calls, malformed tool arguments, timeout/unknown exit, rejected finish, and stale PASS likewise get none.

The last-result restriction is deliberately narrow for the first implementation candidate. If a model calls an exact test and then another tool in the same round, the later result does not qualify; this avoids treating an intermediate result as the terminal observation. It should be tested explicitly before implementation. No LLM Judge, textual failure interpretation, file-name guessing, or output keyword search is needed: exact-call matching, execution/approval facts, exit code, event sequence, and mutation sequence already exist in Runtime data.

Grace is **nonrenewable**. It is decided only at the normal budget boundary, cannot be triggered in grace, and never changes the absolute ceiling. A second FAIL in grace consumes an ordinary remaining slot; a FAIL on call 11 is saved but cannot obtain call 12. An accepted `finish_task` on call 8, 9, 10, or 11 still returns `FINISHED` before the limit decision. Any future implementation should enforce the 11-call ceiling before scheduling each `ask`, including the initial request in its accounting; an unsuccessful request aborts as an error and must not create an uncounted retry path. The current Runtime trace counter records completed replies, so this enforcement detail is not simulated here. A finite ceiling necessarily permits some last-call observations to remain unconsumed. The policy promises only the specified bounded opportunities, not universal observation consumption or task success.

## Provider-free scripted simulation

`eval/phase24_budget.py` consumes preclassified per-turn events. It models decision order and call counts, not tool execution, actual model behavior, the Finish Gate's detailed reasons, or whether a patch fixes the artifact. Its assertions cover:

| Case | Scripted boundary and continuation | Result |
| --- | --- | --- |
| A | T8 exact FAIL → T9 patch → T10 fresh PASS → T11 accepted finish | T8 failure consumed; `FINISHED` on call 11 |
| B | T8 fresh exact PASS → T9 accepted finish | PASS consumed; `FINISHED` on call 9 |
| C | T8 search or read → further scripted search/read | `LIMIT_REACHED` on call 8; no extension |
| D | T8 FAIL → T9 patch → T10 FAIL → T11 FAIL → attempted T12 finish | `LIMIT_REACHED` on call 11; no renewal; T11 FAIL stranded |
| E | Accepted finish on call 11 after FAIL grace | `FINISHED` wins at the absolute ceiling |
| Extra | Accepted finish on base call 8 | `FINISHED` wins at the normal boundary |
| Extra | Failed patch, policy rejection, or stale PASS on call 8 | `LIMIT_REACHED` on call 8 |

Run with `python eval/phase24_budget.py`. These results validate only the proposed transition rules. The real traces establish the cutoff mechanism; they do not establish that a live model will follow the scripted recovery chain.

## Designs to avoid

- Raising the global maximum as a substitute for observation semantics.
- Renewing grace after each observation or each failed test; any such rule can repeatedly extend a task.
- Granting grace for every tool result, including ordinary reads, search, failed patches, policy rejections, or duplicate notices.
- Letting a PASS bypass `finish_task` or treating a verifier's artifact PASS as `FINISHED`.
- Moving the limit check ahead of the final allowed Tool Round, which would discard legitimate tool results or prevent last-turn finish.
- Adding an LLM Judge or a new Recovery State before a deterministic one-shot rule is evaluated.
