# Phase 20：Post-Mutation Verification Guidance Experiment

Control 直接复用 Phase 19 + Recovery 的 5 个有效 Treatment，未重新调用模型。Treatment 只新增一条
事实性行为要求：`After your final code modification, run the required test command to verify the final workspace state before finishing.`。required test visibility、Runtime、Tool Schema、Contract、Completion Hint、
MAX_AGENT_STEPS、Context 和 Permission 保持不变。

## 结果

| condition | run | last mutation | post-mutation exact test | self verified | hint | final | accepted |
|---|---:|---:|---:|---:|---:|---:|---:|
| control_existing_treatment | 1 | 5 | true | true | true | true | true |
| control_existing_treatment | 2 | 5 | true | true | true | true | true |
| control_existing_treatment | 7 | 4 | true | true | true | true | true |
| control_existing_treatment | 8 | 5 | true | true | true | true | true |
| control_existing_treatment | 9 | 5 | false | false | false | true | true |
| treatment_guidance | 1 | 5 | true | true | true | true | true |
| treatment_guidance | 2 | 5 | true | true | true | true | true |
| treatment_guidance | 3 | 5 | true | true | true | false | false |

## Treatment aggregate

- valid runs：3；provider failures：0。
- agent_self_verified：3/3；post-mutation exact test：3/3。
- exact test passed：3/3；Hint：3/3；Final：2/3；accepted：2/3。
- model calls：[7, 7, 8]；tool calls：[8, 8, 9]；tokens：[18057, 18039, 21328]。

## Pattern analysis

- Control test FAIL → patch → Final：1。
- Treatment test FAIL → patch → Final：0。
- 核心观察指标是最终修改后的成功 required test 与 agent_self_verified，不改变 accepted 定义。
- 结果只描述本实验观察，不作统计显著性或因果结论。

## Conclusion

本实验只检验 Verification Freshness Guidance；不关闭 Completion Hint、不实现 ablation、不修改 Runtime。
是否正式纳入 Coding Task Guidance，需根据本轮与既有 5 个 Control 的对照观察决定。

## Raw result

完整结果保存在 `eval/post_mutation_verification_guidance_results.json`。
