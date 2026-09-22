# Phase 18.5：Repository Navigation Stability Check

本阶段只重复实验，不修改 Agent Runtime、Tool Schema、Prompt、fixture 或 Acceptance。
Phase 18 baseline commit：`0aaae56`。新增计划运行 `6` 次；本批次 Provider failure `0` 次。
此前 runner 的已知 timeout attempt 保留为 prior harness attempt：`1` 次，不计入 n=3 聚合。

## Phase 18 baseline

SMALL：`search_text → read_file → Final`，`0/1/1`，model/tool `3/2`，total `6,960`。
MEDIUM：`list_files×3 → read_file → search_text → read_file → apply_patch → Final`，`3/1/2`，model/tool `6/7`，total `14,757`，Verifier accepted。
LARGE：`list_files×3 → search_text → read_file×2 → apply_patch → run_command×2 → Final`，`3/1/2`，model/tool `8/9`，total `21,028`，Verifier accepted。
ERROR-STRING 本阶段不重复。

## 三次 Tool Chain

### small_symbol_navigation
- run 1: `search_text → read_file → Final`
- run 2: `search_text → read_file → Final`
- run 3: `search_text → read_file → Final`

### medium_symbol_coding
- run 1: `list_files → list_files → list_files → read_file → search_text → read_file → apply_patch → Final`
- run 2: `list_files → list_files → list_files → search_text → read_file → read_file → apply_patch → run_command → run_command`
- run 3: `list_files → list_files → list_files → search_text → read_file → read_file → apply_patch → run_command → Final`

### large_symbol_coding
- run 1: `list_files → list_files → list_files → search_text → read_file → read_file → apply_patch → run_command → run_command → Final`
- run 2: `list_files → list_files → list_files → search_text → read_file → read_file → apply_patch → run_command → Final`
- run 3: `list_files → list_files → list_files → read_file → search_text → read_file → apply_patch → run_command → Final`

## 聚合结果

以下统计包含 Phase 18 baseline + 两次新增运行；均值只用于描述本实验样本，不代表真实概率。

| scenario | search usage | list min/mean/max | search min/mean/max | read min/mean/max | model min/mean/max | tool min/mean/max | total tokens min/mean/max | accepted rate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| small_symbol_navigation | 3/3 | 0 / 0.0 / 0 | 1 / 1.0 / 1 | 1 / 1.0 / 1 | 3 / 3.0 / 3 | 2 / 2.0 / 2 | 6936 / 6960.67 / 6986 | 3/3 |
| medium_symbol_coding | 3/3 | 3 / 3.0 / 3 | 1 / 1.0 / 1 | 2 / 2.0 / 2 | 6 / 7.0 / 8 | 7 / 8.0 / 9 | 14757 / 17769.67 / 20892 | 2/3 |
| large_symbol_coding | 3/3 | 3 / 3.0 / 3 | 1 / 1.0 / 1 | 2 / 2.0 / 2 | 7 / 7.33 / 8 | 8 / 8.33 / 9 | 17809 / 18897.33 / 21028 | 3/3 |

## first_correct_file_turn

- small_symbol_navigation: [1, 1, 1]
- medium_symbol_coding: [3, 3, 3]
- large_symbol_coding: [3, 3, 3]

## MEDIUM required-test 行为

MEDIUM 三次记录的 `agent_ran_required_test`：false, false, false。Acceptance 逻辑不变，只观察，不修改 Completion/Contract。

## 分析

- SMALL 是否稳定直接 search：true；三次 chain 的首工具均为 `search_text`，search usage 为 3/3。
- MEDIUM 是否稳定先 list 再 search：true；三次均为三次 `list_files` 后再 `search_text`。LARGE 是否稳定使用 search：true，但有一条路径先 `read_file` 后 search。
- 仓库越大是否增加 list：没有观察到 MEDIUM→LARGE 增加；两者均为 `3/3/3`（min/mean/max）。read 也均为 `2/2/2`。
- first_correct_file_turn：SMALL [1, 1, 1]；MEDIUM [3, 3, 3]；LARGE [3, 3, 3]，导航定位稳定。
- 同样成功但路径不同：MEDIUM 有 3 条不同 chain，LARGE 有 3 条；MEDIUM 的失败记录为 ['run 2 max_steps=True final=False']。
- MEDIUM run 2 虽然 artifact 和 final test 通过，但连续执行两个非 contract 测试后撞 MAX_AGENT_STEPS，没有 Final Answer，因此 accepted=false；没有修改 Completion/Contract。
- Navigation Guidance：当前 search usage 为 SMALL/MEDIUM/LARGE 均 `3/3`，且 LARGE 的 list/read 数没有相对 MEDIUM 增加；证据显示多种可行路径，不足以强制 Tool Preference。

## 原始数据与复现

每条新增运行保留 `raw_result`；聚合结果与原始数据均在 `eval/navigation_stability_results.json`。
先前 timeout 的保留记录在 `eval/navigation_stability_prior_failures.json`。
入口：`python -m eval.navigation_stability`。每个场景从干净 fixture snapshot 开始，Provider failure 最多一次 replacement。
