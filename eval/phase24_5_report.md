# Phase 24.5 — Mutation-Aware Duplicate Calls

An actual workspace change now clears successful tool-call fingerprints in both Coding Tasks and plain chat. This lets the agent repeat an identical `read_file`, `search_text`, `list_files`, or `run_command` against the new workspace state. Failed and no-op edits leave the fingerprints intact. A later executed exact required-test FAIL also invalidates the prior PASS used by the Finish Gate, and recovery returns to `REPAIR_NEEDED`.

## Validation

- Targeted regression: in both Coding Tasks and plain chat, a no-op edit leaves an identical read blocked, while an actual edit lets it return new content. Coding Tasks also refresh identical searches and listings. A saved turn-eight FAIL followed by repair → PASS → edit → fresh PASS → finish is accepted at call 13. A later exact FAIL followed by finish in the same model reply is rejected.
- Full unit suite: 201 tests run, OK, one skipped. `tests/test_loop.py`, Phase 24.3 scripted cases, compilation, and `git diff --check` passed.
- Live continuation: `CONTEXT_MODE=WRITE_ONLY TOOL_APPROVAL_MODE=ALLOW python -m eval.phase24_5 1` and then `2`, with the repository's configured Provider proxy. Each run writes a separate result file and refuses to overwrite it. Both reused fixed prefix `caecc4acb2ce6820`, its reconstructed fixture, and the same exact required test. Both had an executed FAIL on call 8, with no Provider or harness error.

| Run | Calls 9 onward | Result | Recovery tokens |
| --- | --- | --- | ---: |
| 1 | patch → exact PASS → accepted finish | FINISHED at call 11; independent acceptance true | 19,343 |
| 2 | patch → exact PASS → accepted finish | FINISHED at call 11; independent acceptance true | 19,360 |

Both live runs had one actual repair, one exact verification, no nonprogress replies, and zero duplicate blocks. They ran before the plain-chat extension, which leaves the Coding Task path unchanged. The live paths did not attempt a repeated read or search, so they establish end-to-end closure after the initial change but do not isolate its causal effect. The deterministic regressions exercise both modes. Two additional successful samples still do not establish a stable success rate. Token totals cover live recovery calls only; the saved prefix has no usage data. The full traces are in `phase24_5_results_1.json` and `phase24_5_results_2.json`.
