# Phase 19.5：Required-Test Visibility Treatment Stability

本阶段只重复 Phase 19 Treatment；不重新运行 Control，也不修改 Runtime、System Prompt、
Tool Schema、Completion Hint、Contract、MAX_AGENT_STEPS、MEDIUM fixture 或实验 Guidance。
Treatment 继续只暴露事实：`Required test command: python -m unittest discover -s tests -p test_discount.py -q`。

## 实验边界

- 稳定节点：`e6ed38a`。
- 新计划 run：4、5、6；每次从干净 MEDIUM fixture 开始。
- 每个计划 run 最多一次 replacement；只对 provider failure 使用 replacement。
- Control 直接复用 Phase 19，未重新调用模型。
- provider failure 保留在结果中，但不进入 Model Behavior 分母；不作统计显著性结论。

## 新增 Treatment 记录

| run | attempt | provider failure | tool chain | exact test | test turn | exit | hint | hint turn | final | final turn | accepted | max steps | model calls | tool calls | total tokens |
|---:|---:|---|---|---|---:|---|---|---:|---|---:|---|---|---:|---:|---:|
| 4 | 1 | true | `list_files → list_files → list_files → search_text → read_file → read_file` | false | None | None | false | None | false | None | false | false | 4 | 6 | 9034 |
| 4 | 2 | true | `` | false | None | None | false | None | false | None | false | false | 0 | 0 | 0 |
| 5 | 1 | true | `` | false | None | None | false | None | false | None | false | false | 0 | 0 | 0 |
| 5 | 2 | true | `` | false | None | None | false | None | false | None | false | false | 0 | 0 | 0 |
| 6 | 1 | true | `` | false | None | None | false | None | false | None | false | false | 0 | 0 | 0 |
| 6 | 2 | true | `` | false | None | None | false | None | false | None | false | false | 0 | 0 | 0 |

## 新增聚合

- planned runs：3；observed attempts：6。
- valid Treatment：0；provider failure：6。
- exact required test：0/0；Completion Hint：0/0；Final：0/0；accepted：0/0。
- MAX_AGENT_STEPS：0；ineffective zero-test：0。
- 有效样本 model calls：[]；tool calls：[]；total tokens：{'min': None, 'max': None}。
- 全部新增 attempts（含 provider failure）model calls：[4, 0, 0, 0, 0, 0]；tool calls：[6, 0, 0, 0, 0, 0]；total tokens：{'min': 0, 'max': 9034}。

## Phase 19 合并观察

- Phase 19 原有有效 Treatment：2；本轮新增有效 Treatment：0；合并有效样本：2。
- 合并 exact required test：2/2；Completion Hint：2/2；Final：2/2；accepted：2/2。
- `exact test → Hint → Final → accepted` 复现：2 次。
- 合并 MAX_AGENT_STEPS：0；合并 ineffective zero-test：0。
- 合并 token 范围：{'min': 17920, 'max': 17952}。

## Provider failure

- run 3 attempt 1: ['APIConnectionError: Connection error.']；不计入 Model Behavior。
- run 4 attempt 1: ['APIConnectionError: Connection error.']；不计入 Model Behavior。
- run 4 attempt 2: ['APIConnectionError: Connection error.']；不计入 Model Behavior。
- run 5 attempt 1: ['APIConnectionError: Connection error.']；不计入 Model Behavior。
- run 5 attempt 2: ['APIConnectionError: Connection error.']；不计入 Model Behavior。
- run 6 attempt 1: ['APIConnectionError: Connection error.']；不计入 Model Behavior。
- run 6 attempt 2: ['APIConnectionError: Connection error.']；不计入 Model Behavior。

## 解释边界

本报告只描述有限的真实运行观察，不作统计显著性或因果结论。若合并样本持续复现完整链，
Completion Hint ablation 可作为下一项实验候选；本阶段不实现 ablation，也不进入 Runtime 功能开发。

## 原始结果

完整结果保存在 `eval/required_test_visibility_stability_results.json`。
