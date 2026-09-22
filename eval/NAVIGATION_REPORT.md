# Phase 18：Repository Navigation Eval

## 结论状态

评测 harness、三档 fixture、确定性 ground truth、指标提取和离线回归已完成。
四个有效真实场景各运行一次，均完成并被接受。第一次使用仓库指令中的 `127.0.0.1:7897` 时端口没有监听，
未进入 Agent loop；随后使用项目 Phase 17 已验证且当前监听的 `127.0.0.1:9674` relay 完成了四个有效场景。
报告只统计这四个有效场景，没有把传输配置失败混入模型行为数据。

结构化原始结果：[navigation_results.json](navigation_results.json)。

## Phase 17 baseline

Phase 17 的有效真实样本为 `f8440f0` fixture：

| model_calls | tool_calls | list_files | search_text | read_file | apply_patch | run_command | total_tokens | accepted |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 6 | 8 | 4 | 0 | 2 | 1 | 1 | 15,238 | true |

工具链为：`list_files ×4 → read_file ×2 → apply_patch → run_command → Final`。
模型完成了 Coding Task，独立 Verifier `accepted=true`，但没有使用 `search_text`。

## Phase 18 fixtures

Fixture 由 `eval/navigation_fixtures.py` 在每个场景的临时 workspace 中确定性生成，不复制私人项目。

| repo_size | files | symbol target | error-string target |
|---|---:|---|---|
| SMALL | 7 | `src/pricing.py` | `src/validation.py` |
| MEDIUM | 25 | `src/services/billing/discounts.py` | `src/services/billing/validation.py` |
| LARGE-SYNTHETIC | 75 | `src/domain/commerce/pricing/discounts.py` | `src/domain/commerce/pricing/validation.py` |

每个 fixture 都包含唯一的 `def calculate_discount` 实现、唯一的 `invalid discount rate` 文本和一个
可确定失败/通过的 `test_discount.py`。Coding contract 只允许修改 symbol target。

## 真实场景结果

| scenario | repo_size | list | search | read | first_correct_turn | model_calls | total_tokens | accepted |
|---|---|---:|---:|---:|---:|---:|---:|---|
| small_symbol_navigation | SMALL | 0 | 1 | 1 | 1 | 3 | 6,960 | true |
| medium_symbol_coding | MEDIUM | 3 | 1 | 2 | 3 | 6 | 14,757 | true |
| large_symbol_coding | LARGE-SYNTHETIC | 3 | 1 | 2 | 3 | 8 | 21,028 | true |
| medium_error_string_navigation | MEDIUM | 0 | 1 | 0 | 1 | 2 | 4,296 | true |

实际工具链：

- SMALL symbol：`search_text → read_file → Final`；candidate files `2`，首个正确文件前 `0` 个工具调用。
- MEDIUM symbol coding：`list_files ×3 → read_file → search_text → read_file → apply_patch → Final`；
  candidate files `2`，首个正确文件前 `4` 个工具调用，`apply_patch=1`、`run_command=0`、Verifier `accepted=true`。
- LARGE-SYNTHETIC symbol coding：`list_files ×3 → search_text → read_file ×2 → apply_patch → run_command ×2 → Final`；
  candidate files `2`，首个正确文件前 `3` 个工具调用，第一次测试失败、第二次测试通过，Verifier `accepted=true`。
- MEDIUM error-string：`search_text → Final`；candidate files `1`，首个正确文件前 `0` 个工具调用。

四个场景均 `accepted=true`；Coding Task 的 Acceptance 是独立最终状态验证。Medium coding 没有执行 contract
指定的 required test，但最终 workspace 仍通过了独立 Verifier；这两个指标已在 JSON 中分开记录。

| scenario | candidate files | nav calls before correct | prompt | completion | total | apply_patch | run_command | required test | verifier |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| small_symbol_navigation | 2 | 0 | 6,580 | 380 | 6,960 | 0 | 0 | N/A | N/A |
| medium_symbol_coding | 2 | 4 | 14,248 | 509 | 14,757 | 1 | 0 | false | true |
| large_symbol_coding | 2 | 3 | 20,425 | 603 | 21,028 | 1 | 2 | false | true |
| medium_error_string_navigation | 1 | 0 | 4,120 | 176 | 4,296 | 0 | 0 | N/A | N/A |

## 问题回答

1. 小仓库是否 `list_files` 更自然：本轮 SMALL navigation-only 直接使用 `search_text`；Phase 17 的小 coding 样本使用 `list_files`，说明任务类型也影响路径，不能归因于规模单一变量。
2. 仓库变大后模型是否开始 `search_text`：MEDIUM 和 LARGE coding 都使用了 `search_text`，但两者都先做了三次 `list_files`；本样本显示会搜索，不显示明确规模阈值。
3. `search_text` 是否减少目录遍历：没有稳定减少。SMALL/D error-string 没有 `list_files`，MEDIUM/LARGE coding 仍各有三次 `list_files`，不能证明因果节省。
4. `search_text` 是否减少 `read_file`：navigation-only 的 error-string 不需要 `read_file`，symbol navigation 读了一次；coding 两个场景都读了两次。任务完成要求不同，不能直接归因。
5. 哪种任务最容易触发 search：本轮两个 navigation-only 场景都是第一工具直接 `search_text`；两个 coding 场景也最终使用了它，但先浏览目录。
6. 是否存在“工具有了但模型不知道什么时候用”：本轮没有证据支持“完全不知道”，四个场景都用了 `search_text`；coding 场景的延迟使用说明选择顺序仍可能受任务类型和模型策略影响。
7. 这是 Tool Description 问题，还是仓库规模问题：当前样本更支持“任务类型/模型路径”比简单仓库规模更能解释差异；样本只有每场景一次，不能定论，也不修改 Prompt/Tool Description。
8. 是否值得进入 Navigation Guidance：暂不值得凭这四个单次样本改 Guidance；下一阶段最有价值的是在同一 Provider 下重复 A/B/C，区分稳定偏好与单次路径差异。

## 离线验证与复现

已通过：

```text
python -m unittest tests.test_navigation_eval -v
python -m unittest discover -s tests -p "test_*.py" -q
```

结果：Phase 18 离线测试通过；全量 `110` 项通过，`1` 项 Windows 符号链接测试跳过。
真实评测入口为：

```powershell
$env:HTTPS_PROXY="http://127.0.0.1:7897"
$env:HTTP_PROXY="http://127.0.0.1:7897"
$env:ALL_PROXY="socks5://127.0.0.1:7897"
.venv\Scripts\python.exe -m eval.navigation_eval
```

每个有效场景只执行一次，源工作目录不修改；结果写入 `eval/navigation_results.json`。
