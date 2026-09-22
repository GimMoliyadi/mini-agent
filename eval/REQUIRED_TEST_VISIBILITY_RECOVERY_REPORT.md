# Phase 19.5R：Provider Recovery & Resume

Provider preflight 已成功后，本阶段只补 Phase 19.5 缺失的 Treatment 样本。
不重跑 Control，不重复有效 Phase 19 Treatment，不修改 Runtime、Prompt、Tool Schema、
Contract、Completion Hint 或 MAX_AGENT_STEPS。

## Provider preflight

- status：`success`；request：`回复 OK`；response：`OK`；tools：`False`。
- base URL：`https://token.sensenova.cn/v1`；model：`sensenova-6.8-flash-lite`；API Key present：`True`。
- User-Agent：`python-httpx2/2.13.0`；proxy：`{'HTTP_PROXY': 'http://127.0.0.1:9674', 'HTTPS_PROXY': 'http://127.0.0.1:9674', 'ALL_PROXY': None, 'NO_PROXY': '127.0.0.1,localhost'}`。

## Recovery 条件

- Run 7/8/9；每次从干净 MEDIUM fixture 开始。
- 每个计划 run 只尝试一次；不做 replacement。首次 provider/harness failure 后停止后续补样本。
- required test visibility：`python -m unittest discover -s tests -p test_discount.py -q`。

## 新增有效 Treatment / provider failure 记录

| run | provider failure | tool chain | exact test | test turn | exit | hint | hint turn | final | final turn | max steps | accepted | model calls | tool calls | total tokens |
|---:|---|---|---|---:|---|---|---:|---|---:|---|---|---:|---:|---:|
| 7 | false | `list_files → search_text → list_files → list_files → read_file → read_file → apply_patch → run_command → Final` | true | 5 | 0 | true | 5 | true | 6 | false | true | 6 | 8 | 15634 |
| 8 | false | `list_files → list_files → list_files → read_file → search_text → read_file → apply_patch → run_command → Final` | true | 6 | 0 | true | 6 | true | 7 | false | true | 7 | 8 | 18070 |
| 9 | false | `list_files → list_files → list_files → read_file → search_text → read_file → run_command → apply_patch → Final` | false | 4 | 1 | false | None | true | 6 | false | true | 6 | 8 | 15463 |

## 结果

- 新增 provider failures：0；新增有效 Treatment：3。
- 保留 harness failures：3；不计入有效 Treatment。
- 新增 exact test：2/3；Hint：2/3；Final：3/3；accepted：3/3。
- 新增 MAX_AGENT_STEPS：0；ineffective zero-test：0。
- 新增 model calls：[6, 7, 6]；tool calls：[8, 8, 8]；tokens：{'min': 15463, 'max': 18070}。

## 合并 Phase 19 有效 Treatment

- Phase 19 有效样本：2；本轮有效样本：3；合并有效样本：5。
- exact test：4/5；Hint：4/5；Final：5/5；accepted：5/5。
- MAX_AGENT_STEPS：0/5；ineffective zero-test：0。
- `exact test → Hint → Final → accepted`：4 次。
- token range：{'min': 15463, 'max': 18070}。

## 结论边界

以上只报告本实验观察值，不作概率或统计显著性结论。即使达到完整链，本阶段也只标记
Treatment Stability，不执行 Completion Hint ablation，不进入 Phase 20 或新的 Runtime 阶段。

## 原始结果

完整结果保存在 `eval/required_test_visibility_recovery_results.json`。
