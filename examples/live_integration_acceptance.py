"""Explicit real-provider acceptance on disposable fixtures and a supplied RAG URL.

python examples/live_integration_acceptance.py --config-dir /private/state \
    --rag-url http://127.0.0.1:8000 --output .state/live-integrations.json
The RAG service must contain the synthetic stable_unique specification described
in docs/integrations.md. Credentials are loaded locally and never reported.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import tempfile
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mini_agent.runtime import AgentApplication  # noqa: E402
from mini_agent.settings import Settings  # noqa: E402
from mini_agent.verifier import CodingTaskContract, TestCommand  # noqa: E402


def measure(name, result, started, *, passed, trace=None, evidence=None):
    events = result["trace"] if trace is None else trace
    usages = [event.get("usage") or {} for event in events if event["event"] == "model"]
    return {
        "scenario": name, "passed": bool(passed), "status": result["status"],
        "duration_seconds": round(perf_counter() - started, 2),
        "model_calls": len(usages),
        "input_tokens": sum(item.get("input_tokens") or 0 for item in usages),
        "output_tokens": sum(item.get("output_tokens") or 0 for item in usages),
        "usage_missing": sum(not all(item.get(field) is not None for field in
                                     ("input_tokens", "output_tokens")) for item in usages),
        "tool_calls": [event["name"] for event in events if event["event"] == "tool"],
        "verification": result["verification"], "error": result["error"],
        "answer": result["answer"], "evidence": evidence or {},
    }


def run(config_dir: Path, rag_url: str, *, only: str | None = None) -> dict:
    if only not in {None, 'repository-analysis', 'bug-reproduction-fix-tests',
                    'rag-mcp-local-development', 'checkpoint-new-application-resume'}:
        raise ValueError('Unknown acceptance scenario')
    if only in {None, 'rag-mcp-local-development'} and not rag_url:
        raise ValueError('--rag-url is required for the RAG development scenario')
    scenarios = []
    with tempfile.TemporaryDirectory(prefix="mini-agent-live-") as directory:
        temp = Path(directory)
        repository = temp / "repository"
        shutil.copytree(ROOT / "tests" / "fixtures" / "repo_fixture", repository)
        configured = Settings.load(repository, config_dir)
        settings = replace(configured, state_dir=temp / "state", approval_mode="ALLOW",
                           max_model_calls=24, request_timeout=60, trace_path=None,
                           rag_chain_url="", mcp_config_path=None,
                           budget_options={"MINI_AGENT_MAX_TOOL_CALLS": "80",
                                           "MINI_AGENT_TASK_TIMEOUT_SECONDS": "480"})
        if only in {None, 'repository-analysis'}:
            started = perf_counter()
            with AgentApplication(replace(settings, approval_mode="DENY")) as app:
                result = app.run("分析这个代码仓库：用 find_files 和 inspect_python_symbols 定位折扣计算函数及其测试，"
                                 "读取实际代码，给出文件路径、函数名、目前行为和潜在错误。不要修改文件。")
            tools = [event.get("name") for event in result["trace"] if event["event"] == "tool"]
            scenarios.append(measure("repository-analysis", result, started,
                passed=result["status"] == "completed" and "src/pricing.py" in (result["answer"] or "")
                       and {"find_files", "inspect_python_symbols"}.issubset(tools)))

        if only in {None, 'bug-reproduction-fix-tests'}:
            contract = CodingTaskContract("live-discount", "定位并修复折扣计算：100 元打八折应是 80 元。"
                "读取实现和测试，先运行指定测试复现失败，再精确修改源文件，保持测试不变，"
                "重新测试并总结实际结果。", ("src/pricing.py",),
                TestCommand("python", ("-m", "unittest", "discover", "-s", "tests", "-q")))
            started = perf_counter()
            with AgentApplication(settings) as app:
                result = app.run(contract.instruction, contract=contract)
            commands = [event for event in result["trace"] if event.get("name") == "run_command"]
            scenarios.append(measure("bug-reproduction-fix-tests", result, started,
                passed=result["status"] == "completed" and bool((result["verification"] or {}).get("accepted"))
                       and any(event["status"] == "error" for event in commands)
                       and commands[-1]["status"] == "success"))

        if only in {None, 'rag-mcp-local-development'}:
            work = temp / "rag-development"
            work.mkdir()
            (work / "collections_util.py").write_text("def stable_unique(items):\n    raise NotImplementedError\n", encoding="utf-8")
            (work / "test_collections_util.py").write_text(
                "import unittest\nfrom collections_util import stable_unique\n"
                "class StableUnique(unittest.TestCase):\n"
                "    def test_order_and_none(self):\n"
                "        self.assertEqual(stable_unique([None, 3, 1, 3, None, 2]), [3, 1, 2])\n"
                "    def test_empty(self):\n        self.assertEqual(stable_unique([]), [])\n"
                "    def test_strings(self):\n"
                "        self.assertEqual(stable_unique(['b', 'a', 'b']), ['b', 'a'])\n", encoding="utf-8")
            mcp_path = temp / "mcp.json"
            mcp_path.write_text(json.dumps({
                "servers": {"guide": {"transport": "stdio", "command": sys.executable,
                    "args": [str(ROOT / "tests" / "fixtures" / "mcp_server.py")]}},
                "allowed_tools": {"guide": ["python_test_guidance"]},
                "readonly_tools": {"guide": ["python_test_guidance"]},
            }), encoding="utf-8")
            contract = CodingTaskContract("live-rag-mcp", "先调用 search_knowledge 检索 stable_unique 的规范，"
                "再调用 mcp_guide_python_test_guidance 获取测试建议，然后读取本地代码和测试。"
                "依据检索到的规范实现 stable_unique，保持测试不变，运行指定测试。"
                "最终输出说明实现与测试，并引用检索结果的文档标题和来源路径。",
                ("collections_util.py",), TestCommand("python", ("-m", "unittest", "test_collections_util", "-q")))
            started = perf_counter()
            with AgentApplication(replace(settings, workspace=work, rag_chain_url=rag_url,
                                          mcp_config_path=mcp_path)) as app:
                result = app.run(contract.instruction, contract=contract)
                messages = app.graph.get_state(app.config(result["session_id"])).values["messages"]
            evidence = {message.name: message.artifact for message in messages
                        if message.type == "tool" and message.name in {"search_knowledge", "mcp_guide_python_test_guidance"}}
            tools = [event.get("name") for event in result["trace"] if event["event"] == "tool"]
            scenarios.append(measure("rag-mcp-local-development", result, started,
                passed=result["status"] == "completed" and bool((result["verification"] or {}).get("accepted"))
                       and {"search_knowledge", "mcp_guide_python_test_guidance", "run_command"}.issubset(tools)
                       and bool({"read_file", "search_text", "inspect_project"}.intersection(tools))
                       and "stable-unique" in (result["answer"] or "").lower(), evidence=evidence))

        if only in {None, 'checkpoint-new-application-resume'}:
            resume_work = temp / "resume"
            resume_work.mkdir()
            resume_settings = replace(settings, workspace=resume_work)
            started = perf_counter()
            events = []
            with AgentApplication(resume_settings, interrupt_after=["tools"]) as app:
                result = app.run("创建 checkpoint_note.txt，内容精确为 ready 加换行；然后读取文件核对内容，最后总结。")
                events.extend(result["trace"])
                for _ in range(5):
                    if (resume_work / "checkpoint_note.txt").exists() or result["status"] != "interrupted":
                        break
                    result = app.run(None, thread_id=result["session_id"])
                    events = list(result["trace"])
            note = resume_work / "checkpoint_note.txt"
            stamp = note.stat().st_mtime_ns if note.exists() else None
            interrupted = result["status"] == "interrupted"
            with AgentApplication(resume_settings) as app:
                result = app.run(None, thread_id=result["session_id"])
            events.extend(result["trace"])
            scenarios.append(measure("checkpoint-new-application-resume", result, started, trace=events,
                passed=interrupted and result["status"] == "completed" and note.exists()
                       and note.read_text(encoding="utf-8") == "ready\n" and note.stat().st_mtime_ns == stamp,
                evidence={"paused_after_write": interrupted and stamp is not None,
                          "write_not_replayed": note.exists() and note.stat().st_mtime_ns == stamp}))
        return {"model": configured.model, "scenarios": scenarios,
                "passed": sum(scenario["passed"] for scenario in scenarios), "total": len(scenarios),
                "cost": "Token usage reported; no provider price table configured."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--rag-url", default="", help="required for all scenarios or RAG development")
    parser.add_argument("--only", choices=["repository-analysis", "bug-reproduction-fix-tests",
                                            "rag-mcp-local-development", "checkpoint-new-application-resume"])
    parser.add_argument("--output", type=Path, default=ROOT / ".state" / "live-integrations.json")
    args = parser.parse_args()
    try:
        report = run(args.config_dir, args.rag_url, only=args.only)
    except ValueError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] == report["total"] else 1)
