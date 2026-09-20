# Context Management 离线 Benchmark

同一份固定历史、同一份 messages，只改变 Context Mode；不调用真实模型。
样本来自当前 `demo_workspace/` 的真实文件内容，包含 5 个 Tool Round。

| mode | chars | 相比 OFF 减少 | read 信息保留 | write 信息保留 | tool result chars |
|---|---:|---:|---|---|---:|
| OFF | 4,007 | 0 | 1253/1253 chars | 621/621 chars | 1,523 |
| WRITE_ONLY | 3,345 | 662 | 1253/1253 chars | 48/621 chars | 1,523 |
| FULL | 2,877 | 1,130 | 817/1253 chars | 48/621 chars | 1,087 |

- WRITE_ONLY 相比 OFF 减少 **662 字符**。
- FULL 相比 OFF 减少 **1,130 字符**。
- FULL 在 WRITE_ONLY 基础上再减少 **468 字符**。
- 三种模式的协议配对检查均通过。
