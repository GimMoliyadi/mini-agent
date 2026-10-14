"""Opt-in real model acceptance against an isolated, disposable coding fixture."""

from dataclasses import replace
import json
from pathlib import Path
from time import perf_counter

import pytest

from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings
from mini_agent.verifier import CodingTaskContract, TestCommand as RequiredCommand


@pytest.mark.live
def test_real_model_file_and_code_task(tmp_path):
    root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (workspace / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\nclass TestAdd(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n", encoding="utf-8",
    )
    (workspace / "notes.txt").write_text("status: draft\n", encoding="utf-8")
    settings = replace(Settings.load(workspace, root), state_dir=tmp_path / "state",
                       approval_mode="ALLOW", max_model_calls=18, trace_path=None)
    contract = CodingTaskContract(
        "live-file-code", "先查看目录，搜索 draft 和 add，读取相关文件；把 notes.txt 的 draft 改为 ready；"
        "修复 calculator.py 的 add，使其正确相加。不要修改测试。运行指定 unittest，"
        "如果失败继续修复。读回 notes.txt 核对后给出修改与测试总结。",
        ("calculator.py", "notes.txt"), RequiredCommand("python", ("-m", "unittest", "test_calculator", "-q")),
    )
    started = perf_counter()
    with AgentApplication(settings) as app:
        result = app.run(contract.instruction, contract=contract)
    report = {
        "scenario": "real-model-file-and-code", "status": result["status"],
        "duration_seconds": round(perf_counter() - started, 2),
        "accepted": bool((result["verification"] or {}).get("accepted")),
        "verification": result["verification"],
        "model_calls": sum(e["event"] == "model" for e in result["trace"]),
        "tool_calls": [e["name"] for e in result["trace"] if e["event"] == "tool"],
        "usage": [e["usage"] for e in result["trace"] if e["event"] == "model"],
        "error": result["error"],
    }
    (root / ".state").mkdir(exist_ok=True)
    (root / ".state" / "live-validation.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    assert result["status"] == "completed", report
    assert report["accepted"], report
    assert (workspace / "notes.txt").read_text().strip() == "status: ready"
    assert {"list_files", "search_text", "read_file", "run_command"}.issubset(report["tool_calls"])
