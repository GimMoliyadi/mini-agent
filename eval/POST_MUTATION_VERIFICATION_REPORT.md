# Phase 19.6：Post-Mutation Verification Forensics

本阶段只分析 Phase 19 的 2 个有效 Treatment 与 Phase 19.5R 的 3 个有效 Treatment。
不修改 Runtime、Prompt、Tool Schema、Contract、Completion Hint、Verifier 或 MAX_AGENT_STEPS。

## Run-level evidence

| run | tool chain | last mutation | exact turns / exits | last successful exact | final | post-mutation pass | hint | artifact | interaction | self-verified | accepted |
|---:|---|---:|---|---:|---|---|---|---|---|---|---|
| 1 | `list_files → list_files → list_files → search_text → read_file → read_file → apply_patch → run_command → Final` | 5 | 6/0 | 6 | 7 | true | true | true | true | true | true |
| 2 | `list_files → list_files → list_files → read_file → search_text → read_file → apply_patch → run_command → Final` | 5 | 6/0 | 6 | 7 | true | true | true | true | true | true |
| 7 | `list_files → search_text → list_files → list_files → read_file → read_file → apply_patch → run_command → Final` | 4 | 5/0 | 5 | 6 | true | true | true | true | true | true |
| 8 | `list_files → list_files → list_files → read_file → search_text → read_file → apply_patch → run_command → Final` | 5 | 6/0 | 6 | 7 | true | true | true | true | true | true |
| 9 | `list_files → list_files → list_files → read_file → search_text → read_file → run_command → apply_patch → Final` | 5 | 4/1 | None | 6 | false | false | true | true | false | true |

## Aggregate

- Final：5/5；accepted：5/5。
- exact test executed：5/5；exact test passed：4/5。
- post-mutation exact test passed：4/5；agent_self_verified：4/5。
- Completion Hint：4/5；artifact：5/5；interaction：5/5。
- stale PASS → later mutation evidence：0 runs。

## Run 9

Run 9 的 exact required test 发生在 Turn 4，exit code 为 1；最后一次 apply_patch 发生在 Turn 5。
因此 Agent 在最终修改后没有自验证。最终 Verifier 仍确认 artifact 正确，Agent 也正常 Final 并被 accepted；
这不等同于 Agent 自验证成功。

## Interpretation

Phase 19.6 保持 accepted 的既有定义：Verifier 仍是最终真实性来源，不要求 agent_self_verified=true 才 accepted。
当前主要观察问题更接近 Verification Freshness，而不是 Completion：Run 9 有 Final 且 accepted，但没有最后修改后的成功 exact test。
‘required test visible + 最终修改后重新验证’值得作为后续受控实验候选；本阶段不实现 Guidance 或 ablation。

## Raw source

结果保存在 `eval/post_mutation_verification_results.json`。
