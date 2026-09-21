# Phase 19：Required-Test Visibility Experiment

本阶段只修改评测 harness，不修改 Agent Runtime、Tool Schema、Completion Hint、Acceptance、
MAX_AGENT_STEPS、Permission、Sandbox 或 Context。Treatment 只向模型提供一条事实：
`Required test command: python -m unittest discover -s tests -p test_discount.py -q`。没有增加‘必须 Final’或‘优先执行’等指导。

## 实验条件

- baseline：`8c0620d`；Control 直接复用 Phase 18.5 MEDIUM 三次记录。
- Treatment：MEDIUM 干净 fixture × 3；每次独立临时 workspace。
- Treatment provider failure：1；Provider failure 不计入行为分母。
- n=3 只用于观察，不作统计显著性结论。

## 结果总表

| condition | run | exact test | hint | final | max steps | accepted | model calls | tokens |
|---|---:|---|---|---|---|---|---:|---:|
| control | 1 | false | false | true | false | true | 6 | 14757 |
| control | 2 | false | false | false | true | false | 8 | 20892 |
| control | 3 | false | false | true | false | true | 7 | 17660 |
| treatment | 1 | true | true | true | false | true | 7 | 17952 |
| treatment | 2 | true | true | true | false | true | 7 | 17920 |
| treatment | 3 | false | false | false | false | false | 1 | 2045 |

## 聚合结果

| condition | exact test | hint | final | max steps | accepted | zero-test attempts | model calls | tool calls | tokens |
|---|---:|---:|---:|---:|---:|---:|---|---|---|
| control | 0/3 | 0/3 | 2/3 | 1/3 | 2/3 | 1 | [6, 8, 7] | [7, 9, 8] | [14757, 20892, 17660] |
| treatment | 2/2 | 2/2 | 2/2 | 0/2 | 2/2 | 0 | [7, 7] | [8, 8] | [17952, 17920] |

## Control Tool Chain

- run 1: `list_files → list_files → list_files → read_file → search_text → read_file → apply_patch → Final`
- run 2: `list_files → list_files → list_files → search_text → read_file → read_file → apply_patch → run_command → run_command`
- run 3: `list_files → list_files → list_files → search_text → read_file → read_file → apply_patch → run_command → Final`

## Treatment Tool Chain

- run 1: `list_files → list_files → list_files → search_text → read_file → read_file → apply_patch → run_command → Final`
  - Turn 6: `python -m unittest discover -s tests -p test_discount.py -q` exit=0 tests=1 hint=true
- run 2: `list_files → list_files → list_files → read_file → search_text → read_file → apply_patch → run_command → Final`
  - Turn 6: `python -m unittest discover -s tests -p test_discount.py -q` exit=0 tests=1 hint=true
- run 3: `list_files`

## Provider failure records

- treatment run 3: ['APIConnectionError: Connection error.']；不计入 Model Behavior。

## 观察结论

- Control exact required test：0/3；Treatment：2/2。
- Control Completion Hint：0/3；Treatment：2/2。
- Control Final：2/3；Treatment：2/2。
- Control accepted：2/3；Treatment：2/2。
- 这些是 n=3 的行为观察，不能单独证明因果关系。
- required test 可见性改善收口的判断只在 exact test、Hint、Final 和 Acceptance 同时观察后成立；若 exact test 增加但仍无 Final，问题更接近 Completion Control。

## 人话解释

required test 是 Contract 的事实，因为 Verifier 会独立使用它重跑最终测试。此前模型看不到它，是因为 Phase 18 navigation harness 只提供通用 System Prompt 和用户任务，没有注入 Contract 指定命令。模型因此可能自行选择 `unittest discover` 或模块路径测试；exit code 0 只表示进程成功退出，不保证实际运行了测试。Completion Hint 只认 exact command，是为了把‘成功执行了 Contract 要求的验证’与普通命令区分开。Verifier 仍必须独立重跑，避免相信 Agent 自己的旧结果。visible test 只是把事实告诉模型，forced test 则会替模型执行或限制选择；Phase 19 只测前者。真实 Coding Agent 也需要从任务规格获得可执行的验证命令，但仍应保留 LLM 决策和独立验收。

## 原始结果

完整 Control/Treatment raw result 保存在 `eval/required_test_visibility_results.json`。
