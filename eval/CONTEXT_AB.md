# Context Management 真实 A/B

每个任务在 OFF / WRITE_ONLY / FULL 各运行一次；共 9 次任务。
三种模式使用相同模型、System Prompt、Tool Schema、任务文本和 workspace snapshot。

| task | mode | success | Final Answer | model calls | tool calls | max steps | prompt | completion | total | Provider errors |
|---|---|---|---|---:|---:|---|---:|---:|---:|---:|
| task_1 | OFF | True | True | 5 | 5 | False | 7,561 | 1,017 | 8,578 | 0 |
| task_2 | OFF | True | True | 2 | 2 | False | 2,487 | 987 | 3,474 | 0 |
| task_3 | OFF | True | True | 3 | 2 | False | 2,948 | 283 | 3,231 | 0 |
| task_1 | WRITE_ONLY | True | True | 4 | 4 | False | 4,948 | 979 | 5,927 | 0 |
| task_2 | WRITE_ONLY | True | True | 2 | 2 | False | 2,487 | 681 | 3,168 | 0 |
| task_3 | WRITE_ONLY | True | True | 3 | 2 | False | 2,948 | 172 | 3,120 | 0 |
| task_1 | FULL | True | True | 5 | 7 | False | 9,099 | 923 | 10,022 | 0 |
| task_2 | FULL | False | False | 1 | 2 | False | 906 | 75 | 981 | 1 |
| task_3 | FULL | True | True | 3 | 2 | False | 2,948 | 197 | 3,145 | 0 |

A = task_1 摘要写入；B = task_2 双文件比较；C = task_3 列目录。
一次运行不能证明统计显著性；结果只用于观察这三个模式在同一组任务上的行为和成本差异。
