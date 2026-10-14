"""Opt-in real-model task app acceptance; credentials stay in Settings.

Run with --config-dir pointing to existing local configuration. Disposable
workspaces, checkpoints, model/tool messages and independent evidence are
retained under --work-dir. This script never commits or publishes agent output.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
from time import monotonic, perf_counter, sleep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from mini_agent.browser_tools import BrowserSession  # noqa: E402
from mini_agent.code_index import _parser  # noqa: E402
from mini_agent.dev_servers import DevServerManager, _port_reachable  # noqa: E402
from mini_agent import dev_environment as dev  # noqa: E402
from mini_agent.runtime import AgentApplication  # noqa: E402
from mini_agent.sandbox import redact_text  # noqa: E402
from mini_agent.settings import Settings  # noqa: E402
from live_integration_acceptance import measure  # noqa: E402


def prepare(workspace: Path, scenario: str) -> None:
    shutil.copytree(ROOT / "tests/fixtures/task_web", workspace)
    backend = workspace / "backend"
    with sqlite3.connect(backend / "tasks.db") as connection:
        connection.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
        connection.execute("INSERT INTO tasks VALUES (1, 'Seed task')")
    subprocess.run([sys.executable, "-m", "venv", str(backend / ".venv")], check=True, timeout=45)
    # venv's system-site-packages does not include a parent virtualenv. Reuse
    # this explicitly prepared acceptance environment via a disposable .pth.
    site = backend / ".venv" / ("Lib/site-packages" if os.name == "nt" else
                                f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    (site / "acceptance-dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n", encoding="utf-8")
    installed = dev._execute(["npm", "install", "--no-audit", "--no-fund"],
                             workspace / "frontend", workspace, timeout=240)
    print(installed.text, flush=True)
    assert installed.status == "success", installed.text
    if scenario == "C":
        source = workspace / "frontend/src/main.tsx"
        source.write_text(source.read_text(encoding="utf-8").replace("{task.title}", "{task.name}"), encoding="utf-8")
    # The actual code-review tool requires the workspace itself to be a Git
    # repository. This private baseline is never pushed or committed by Agent.
    for argv in (["git", "init", "--initial-branch=qa-fixture"], ["git", "add", "."],
                 ["git", "-c", "user.name=Acceptance Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-m", "Baseline task application"]):
        baseline = dev._execute(argv, workspace, workspace, timeout=20)
        assert baseline.status == "success", baseline.text


def wait_text(browser: BrowserSession, selector: str, expected: str) -> str:
    deadline = monotonic() + 20
    text = ""
    while monotonic() < deadline:
        text = browser.invoke("inspect", "", selector, "")["text"]
        if expected in text:
            return text
        sleep(0.25)
    raise AssertionError(f"Expected {expected!r} in {selector}: {text!r}")


def verify_typed_hooks(source: str) -> None:
    tree = _parser("tsx").parse(source.encode("utf-8"))
    assert not tree.root_node.has_error, "Invalid TSX syntax"
    imported = False
    calls = set()
    pending = [tree.root_node]
    while pending:
        node = pending.pop()
        if node.type == "import_statement":
            origin = node.child_by_field_name("source")
            if origin and origin.text.decode().strip("'\"") == "./api-hooks":
                bindings = list(node.named_children)
                while bindings:
                    binding = bindings.pop()
                    if binding.type == "import_specifier":
                        name = binding.child_by_field_name("name")
                        alias = binding.child_by_field_name("alias")
                        if name and name.text == b"$api" and (not alias or alias.text == b"$api"):
                            imported = True
                    bindings.extend(binding.named_children)
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            if function and function.type == "member_expression":
                owner = function.child_by_field_name("object")
                member = function.child_by_field_name("property")
                if owner and member and owner.text == b"$api":
                    calls.add(member.text.decode())
        pending.extend(node.named_children)
    assert imported, "Generated hooks are not imported"
    assert {"useQuery", "useMutation"} <= calls, "Typed hooks are not called"


def close_resources(browser, servers) -> None:
    try:
        browser.shutdown()
    finally:
        servers.close()


def verify(workspace: Path, priority: bool) -> dict:
    assert (workspace / "backend/test_main.py").read_text(encoding="utf-8") == (
        ROOT / "tests/fixtures/task_web/backend/test_main.py"
    ).read_text(encoding="utf-8"), "Existing CRUD regression was modified"
    if priority:
        source = (workspace / "frontend/src/main.tsx").read_text(encoding="utf-8")
        verify_typed_hooks(source)
        migrated = dev._execute(
            [str(dev._python_exe(workspace / "backend")), "-c",
             "import main; main.initialize(); main.initialize()"],
            workspace / "backend", workspace, timeout=30,
        )
        assert migrated.status == "success", migrated.text
    checks = {}
    for language, path, task in [("python", "backend", "test"),
                                 ("node", "frontend", "typecheck"), ("node", "frontend", "build")]:
        argv = ([str(dev._python_exe(workspace / path)), "-m", "pytest", "-q"]
                if language == "python" else ["npm", "run", task])
        result = dev._execute(argv, workspace / path, workspace, timeout=120)
        checks[f"{path}_{task}"] = {"status": result.status, "output": result.text}
        assert result.status == "success", result.text
    servers = DevServerManager(workspace)
    browser = BrowserSession(workspace)
    evidence = {"checks": checks}
    try:
        servers.start("backend", "backend", 8000, app_import="main:app")
        servers.start("frontend", "frontend", 5173)
        deadline = monotonic() + 30
        while monotonic() < deadline:
            if all(servers.status(name).get("port_open") for name in ("backend", "frontend")):
                break
            sleep(0.25)
        else:
            raise AssertionError({name: servers.status(name) for name in ("backend", "frontend")})
        with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False) as client:
            rows = client.get("/api/tasks").json()
            seed = next(item for item in rows if item["id"] == 1)
            assert isinstance(seed["title"], str) and seed["title"], seed
            if priority:
                assert seed["title"] == "Seed task", seed
                assert rows[0]["priority"] == 2, rows
                created = client.post("/api/tasks", json={"title": "API priority", "priority": 3})
                assert created.status_code == 200, created.text
                task_id = created.json()["id"]
                updated = client.put(f"/api/tasks/{task_id}", json={"title": "API updated", "priority": 1})
                assert updated.status_code == 200 and updated.json()["priority"] == 1, updated.text
                assert client.post("/api/tasks", json={"title": "Invalid", "priority": 9}).status_code == 422
                with sqlite3.connect(workspace / "backend/tasks.db") as connection:
                    assert connection.execute("SELECT priority FROM tasks WHERE id=?", (task_id,)).fetchone() == (1,)
                schema = client.get("/openapi.json").json()
                assert any("priority" in item.get("properties", {}) for item in schema["components"]["schemas"].values())
            browser.invoke("open", "http://127.0.0.1:5173", "body", "")
            wait_text(browser, "#tasks", seed["title"])
            browser.invoke("fill", "", "#title", "Browser created")
            if priority:
                browser.invoke("fill", "", "#priority", "3")
            browser.invoke("click", "", "#save", "")
            wait_text(browser, "#tasks", "Browser created")
            created = next(item for item in client.get("/api/tasks").json() if item["title"] == "Browser created")
            if priority:
                assert created["priority"] == 3, created
            task_id = created["id"]
            if priority:
                row_text = browser.invoke("inspect", "", f"#task-{task_id}", "")["text"].lower()
                assert any(label in row_text for label in ("3", "高", "high")), row_text
            browser.invoke("click", "", f"#edit-{task_id}", "")
            browser.invoke("fill", "", "#title", "Browser edited")
            if priority:
                browser.invoke("fill", "", "#priority", "1")
            browser.invoke("click", "", "#save", "")
            wait_text(browser, f"#task-{task_id}", "Browser edited")
            saved = next(item for item in client.get("/api/tasks").json() if item["id"] == task_id)
            assert saved["title"] == "Browser edited", saved
            if priority:
                assert saved["priority"] == 1, saved
                row_text = browser.invoke("inspect", "", f"#task-{task_id}", "")["text"].lower()
                assert any(label in row_text for label in ("1", "低", "low")), row_text
                with sqlite3.connect(workspace / "backend/tasks.db") as connection:
                    assert connection.execute("SELECT priority FROM tasks WHERE id=?", (task_id,)).fetchone() == (1,)
            evidence["saved_browser_task"] = saved
        diagnostics = browser.diagnostic_snapshot()
        assert not [event for event in diagnostics["events"] if event["kind"] in
                    {"console_error", "javascript_exception", "request_failed", "http_error"}], diagnostics
        evidence["browser_diagnostics"] = diagnostics
        (workspace / "screenshots").mkdir(exist_ok=True)
        evidence["screenshot"] = browser.capture("screenshots/final.png")
    finally:
        close_resources(browser, servers)
    assert not servers.servers
    assert not any(_port_reachable(port) for port in (8000, 5173)), "Managed server is still listening"
    evidence["process_cleanup"] = True
    return evidence


def run_scenario(scenario: str, config_dir: Path, work_dir: Path, max_total_tokens: int,
                 max_model_calls: int, context_tokens: int, resume: str | None) -> dict:
    workspace = work_dir / f"scenario-{scenario}"
    previous = None
    if resume:
        previous = json.loads((work_dir / f"result-{scenario}.json").read_text(encoding="utf-8"))
        baseline_status = previous["evidence"]["baseline_typecheck"]
        resume_number = len(list(work_dir.glob(f"result-{scenario}-before-resume-*.json"))) + 1
        (work_dir / f"result-{scenario}-before-resume-{resume_number}.json").write_text(
            json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        prepare(workspace, scenario)
        baseline = dev._execute(["npm", "run", "typecheck"], workspace / "frontend", workspace, timeout=60)
        if scenario == "C":
            assert baseline.status == "command_failed" and "name" in baseline.text, baseline.text
        else:
            assert baseline.status == "success", baseline.text
        baseline_status = baseline.status
    settings = replace(Settings.load(workspace, config_dir), state_dir=work_dir / f"state-{scenario}",
                       approval_mode="ALLOW", max_model_calls=max_model_calls, request_timeout=90,
                       context_tokens=context_tokens,
                       trace_path=work_dir / f"trace-{scenario}.jsonl", rag_chain_url="", mcp_config_path=None,
                       budget_options={"MINI_AGENT_MAX_TOOL_CALLS": "180", "MINI_AGENT_TASK_TIMEOUT_SECONDS": "900",
                                       "MINI_AGENT_MAX_TOTAL_TOKENS": str(max_total_tokens)})
    instruction = (
        "为现有 FastAPI + React/React Query + SQLite 任务应用增加优先级：整数 1=低、2=普通、3=高，"
        "默认 2。前端须能创建、编辑、显示优先级，API 校验范围并正确保存 SQLite；保留已有 Seed task，"
        "数据库已有 tasks(id,title)，需可重复执行的向前 schema 迁移，旧任务默认优先级 2。"
        "保留现有 CRUD 行为和测试。优先级输入用 number input，id=priority；保留 title/save/tasks/task-ID/edit-ID DOM id。"
        "同步导出 OpenAPI、生成 frontend/src/openapi.d.ts、api-client.ts 和 api-hooks.ts；"
        "在 main.tsx 中导入 api-hooks 并使用生成的 $api.useQuery/useMutation。"
        "自主规划、修改，补充回归，执行后端 pytest、前端 typecheck 和 build，启动后端8000前端5173，"
        "用浏览器实际创建/编辑并诊断错误，审查代码，最后总结实际结果；不提交 Git、不访问远程。"
        if scenario == "B" else
        "这个任务应用显示不正常，已有任务名称不显示，请检查运行与构建情况，复现故障、定位原因、"
        "修复前后端字段不一致并验证。保留 API 的 title 字段和现有 DOM id，不改测试来绕过失败。"
        "执行后端 pytest、前端 typecheck/build，启动后端8000前端5173，浏览器验证显示及创建编辑，"
        "收集错误、审查代码，最后总结。无需提交 Git 或远程访问。"
    )
    started = perf_counter() - (previous["duration_seconds"] if previous else 0)
    messages = []
    with AgentApplication(settings) as app:
        result = app.run(None if resume else instruction, thread_id=resume, auto_recover=True)
        for message in app.graph.get_state(app.config(result["session_id"])).values.get("messages", []):
            messages.append({"type": message.type, "name": getattr(message, "name", None),
                             "content": redact_text(str(message.content)),
                             "tool_calls": getattr(message, "tool_calls", [])})
    (work_dir / f"messages-{scenario}.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    if previous:
        result["trace"] = previous["trace"] + result["trace"]
    evidence = {"baseline_typecheck": baseline_status, "workspace": str(workspace),
                "resumed_after_failure": bool(resume)}
    passed = False
    independent_passed = False
    try:
        evidence.update(verify(workspace, scenario == "B"))
        if scenario == "B":
            called = {event.get("name") for event in result["trace"]
                      if event["event"] == "tool" and event.get("status") == "success"}
            assert {"export_fastapi_openapi", "generate_openapi_types", "generate_typed_api_client",
                    "generate_react_query_hooks"} <= called, "Typed binding toolchain was not exercised"
        independent_passed = True
        passed = result["status"] == "completed"
    except Exception as exc:
        evidence["independent_failure"] = redact_text(f"{type(exc).__name__}: {exc}")
    report = measure(f"task-web-{scenario}", result, started, passed=passed, evidence=evidence)
    report["trace"] = result["trace"]
    report["max_total_tokens"] = max_total_tokens
    report["session_id"] = result["session_id"]
    report["context_tokens"] = context_tokens
    report["independent_passed"] = independent_passed
    report["manual_resumes"] = len(list(work_dir.glob(f"result-{scenario}-before-resume-*.json")))
    report["auto_recovery_rounds"] = sum(event["event"] == "recovery_started" for event in result["trace"])
    report["cached_input_tokens"] = sum(
        (event.get("usage") or {}).get("input_token_details", {}).get("cache_read", 0)
        for event in result["trace"] if event["event"] == "model"
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--scenario", choices=["B", "C"], required=True)
    parser.add_argument("--max-total-tokens", type=int, default=750000,
                        help="Finite real-model acceptance budget; product default stays unchanged")
    parser.add_argument("--max-model-calls", type=int, default=80)
    parser.add_argument("--context-tokens", type=int, default=32000)
    parser.add_argument("--resume", help="Resume the saved checkpoint after an explicit environment/budget repair")
    args = parser.parse_args()
    if args.max_total_tokens < 1 or args.max_model_calls < 1 or args.context_tokens < 256:
        parser.error("Model/token budgets must be positive, with at least 256 context tokens")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    report = run_scenario(args.scenario, args.config_dir, args.work_dir.resolve(), args.max_total_tokens,
                          args.max_model_calls, args.context_tokens, args.resume)
    output = args.work_dir / f"result-{args.scenario}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["scenario", "passed", "status", "independent_passed",
          "duration_seconds", "model_calls", "tool_calls", "input_tokens", "output_tokens",
          "manual_resumes", "auto_recovery_rounds"]}, ensure_ascii=False), flush=True)
    raise SystemExit(0 if report["passed"] else 1)
