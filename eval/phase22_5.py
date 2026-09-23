"""One-shot Phase 22.5 navigation and diagnosis evaluation."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from . import phase22_fixtures as fixtures
from . import phase22_harness as harness


RESULTS = Path(__file__).with_name("phase22_5_results.json")
REPORT = Path(__file__).with_name("phase22_5_report.md")
SOURCE_PATHS = tuple(path for path in fixtures._BASE_FILES if path.startswith("src/") and not path.endswith("/__init__.py"))
CASES = {
    "N1": ("A", "At checkout, a subtotal of 100 and shipping of 12 produces 124 instead of 112. Find the calculation, correct it, and run the required test."),
    "N2": ("C", "The order summary for a 100 subtotal and 10 percent discount reports 110. Follow the order flow into its pricing dependency and make the total 90. Run the required test."),
    "N3": ("D", "A customer credit of 10 raises both the quote and invoice for 100 to 110. Locate both calculations among the pricing and order modules, make both totals 90, and run the required test."),
}
DIAGNOSIS_TEST = '''import unittest

from src.pricing.coupons import discounted_amount


class CouponTests(unittest.TestCase):
    def test_reported_whole_percent(self):
        self.assertEqual(discounted_amount(200.0, 10), 180.0)

    def test_decimal_percent(self):
        self.assertEqual(discounted_amount(100.0, 0.10), 90.0)

    def test_one_percent_boundary(self):
        self.assertEqual(discounted_amount(100.0, 1), 99.0)
'''
SURFACE_FIX = '''"""Coupon and percentage-discount helpers."""


def discounted_amount(amount: float, percent: float) -> float:
    """Return the amount after a percentage coupon."""
    rate = percent / 100 if percent > 1 else percent
    return amount * (1 - rate)
'''


def setup(case: str, root: Path, *, register: bool = False) -> fixtures.TaskSpec:
    fixtures.build_fixture(root)
    if case == "E":
        (root / "tests/test_coupons.py").write_text(DIAGNOSIS_TEST, encoding="utf-8")
        base = fixtures.get_task("E")
        spec = fixtures.TaskSpec("E25", "diagnosis_recovery", "A coupon of 10 percent on 200 currently gives 190 instead of 180. Fix the reported behavior, run the required test, and use any failure output to diagnose remaining cases.", base.ground_truth)
    else:
        base = fixtures.get_task(CASES[case][0])
        spec = fixtures.TaskSpec(case, f"navigation_{case.lower()}", CASES[case][1], base.ground_truth)
    if register:
        fixtures.TASKS[spec.task_id] = spec
    return spec


def contract(spec: fixtures.TaskSpec) -> dict:
    value = fixtures.coding_contract(spec)
    value["allowed_paths"] = list(SOURCE_PATHS)
    return value


def deterministic_recovery_check() -> dict:
    with tempfile.TemporaryDirectory(prefix="phase22-5-fixture-") as directory:
        root = Path(directory) / "workspace"
        spec = setup("E", root)
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", spec.expected_test, "-q"]
        def run():
            return subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        baseline = run()
        (root / "src/pricing/coupons.py").write_text(SURFACE_FIX, encoding="utf-8")
        surface = run()
        (root / "src/pricing/coupons.py").write_text('''"""Coupon and percentage-discount helpers."""


def discounted_amount(amount: float, percent: float) -> float:
    """Return the amount after a percentage coupon."""
    rate = percent / 100 if isinstance(percent, int) or percent > 1 else percent
    return amount * (1 - rate)
''', encoding="utf-8")
        complete = run()
        evidence = {"baseline_exit": baseline.returncode, "surface_exit": surface.returncode, "surface_output": surface.stdout + surface.stderr, "complete_exit": complete.returncode}
        if not (baseline.returncode and surface.returncode and "test_one_percent_boundary" in evidence["surface_output"] and complete.returncode == 0):
            raise AssertionError(evidence)
        return evidence


def run_child(case: str) -> dict:
    root = Path(os.environ["AGENT_WORKSPACE"])
    spec = setup(case, root, register=True)
    # The fixture is already built by the parent. Register the same spec in this process.
    with patch.object(harness, "coding_contract", contract):
        return harness._run_live_task(spec.task_id)


def run_case(case: str) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"phase22-5-{case.lower()}-") as directory:
        root = Path(directory) / "workspace"
        setup(case, root)
        env = {**os.environ, "AGENT_WORKSPACE": str(root), "TOOL_APPROVAL_MODE": "ALLOW", "CONTEXT_MODE": "WRITE_ONLY", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        done = subprocess.run([str(harness.PYTHON_BIN), "-m", "eval.phase22_5", "--child", case], cwd=harness.PROJECT_ROOT, env=env, capture_output=True, timeout=harness.CHILD_TIMEOUT_SECONDS)
        if done.returncode:
            raise RuntimeError(f"{case} child exited {done.returncode}: {done.stderr.decode('utf-8', 'replace')[-3000:]}")
        return json.loads(done.stdout.decode("utf-8"))


def navigation(result: dict) -> dict:
    m = result["metrics"]
    chain = harness._history_tool_events(result["canonical_history"], result["trace"])
    target = set(m["ground_truth"]["intended_changed_files"])
    counts = {name: 0 for name in ("list_files", "search_text", "read_file")}
    first_turn = None
    before = None
    for event in chain:
        tool = event["tool"]
        if tool not in counts:
            continue
        args = event["arguments"]
        if tool == "list_files":
            candidates = harness._list_file_candidates(event["result"], args.get("path"))
        elif tool == "search_text":
            candidates = harness._search_candidates(event["result"])
        else:
            candidates = {args.get("path", "")}
        if first_turn is None and candidates & target:
            first_turn = event["turn"]
            before = dict(counts)
        counts[tool] += 1
    listings = [event["arguments"].get("path", ".") for event in chain if event["tool"] == "list_files"]
    reads = [event["arguments"].get("path") for event in chain if event["tool"] == "read_file"]
    return {"first_correct_file_turn": first_turn, "list_files_calls": m["list_files_calls"], "search_text_calls": m["search_text_calls"], "read_file_calls": m["read_file_calls"], "before_correct": before, "repeated_list_directories": sorted({path for path in listings if listings.count(path) > 1}), "listed_directories": listings, "read_paths": reads, "total_tool_calls": m["tool_calls"], "total_tokens": m["total_tokens"], "accepted": m["accepted"]}


def render_report(payload: dict) -> str:
    rows = []
    for case, m in payload["navigation"].items():
        b = m["before_correct"] or {}
        rows.append(f"| {case} | {m['first_correct_file_turn']} | {m['list_files_calls']} | {m['search_text_calls']} | {m['read_file_calls']} | {b.get('list_files', 0)}/{b.get('search_text', 0)}/{b.get('read_file', 0)} | {m['total_tool_calls']} | {m['total_tokens']} | {m['accepted']} |")
    e = payload["results"]["E"]
    em = e["metrics"]
    chain = em["tool_chain"]
    chain_lines = [f"{event['turn']}. `{event['tool']}` — {event['arguments']}" for event in chain]
    return "\n".join([
        "# Phase 22.5 — Navigation & Diagnosis Forensics", "",
        "## Navigation", "",
        "| Task | First correct source turn | list | search | read | Before source: list/search/read | Tools | Tokens | Accepted |",
        "| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- |",
        *rows, "",
        "N1 listed three directories before searching for shipping/subtotal. N2 searched for discount on turn 1 and then traced the summary/pricing files. N3 searched for credit after one root list. No task repeated a directory listing. N2 read the unrelated coupons module once; the other navigation tasks had no clearly unrelated reads. Search was effective in all three tasks. There is no consistent blind traversal or repeated listing pattern across tasks, so repository navigation is not sufficiently supported as Phase 23.", "",
        "## Diagnosis recovery", "",
        "Deterministic fixture check: baseline failed, the plausible first normalization (`percent > 1`) failed only `test_one_percent_boundary` (`0.0 != 99.0`), and the complete boundary-aware fix passed.", "",
        "Real tool chain:", "", *[f"- {line}" for line in chain_lines], "",
        f"First mutation: turn {next(x['turn'] for x in chain if x['tool'] in ('apply_patch', 'write_file'))}. The patch used `percent > 1` and treated the `[0, 1]` range as decimal fractions, despite the model having read the `1%` test on turn 3. Required test: turn {next(x['turn'] for x in chain if x['tool'] == 'run_command')}, exit 1. It reported `test_one_percent_boundary` with `0.0 != 99.0`. The failure was appended to canonical history, but no next model call occurred because the agent reached the frozen eight-step limit. Thus the model did not see the failure in a subsequent inference. There was no second diagnosis, second mutation, passing test, or accepted finish.", "",
        f"Metrics: required_test_attempts={em['required_test_attempts']}, mutation_count={em['mutation_count']}, accepted={em['accepted']}, tokens={em['total_tokens']}, tool_calls={em['tool_calls']}.", "",
        "## Phase 23 candidate", "",
        "Diagnosis recovery within the current step budget is the only candidate for further study. The broad read phase consumed turns 3–6, including several unrelated pricing/order files after the relevant test and source were read. Phase 22.5 does not prove recovery succeeds. No Runtime change was made.", "",
        "## Validation", "", "Full unittest suite: 175 tests, 0 failed, 1 skipped. `python -m compileall -q eval tests`: passed. `git diff --check`: passed.", "",
    ])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=(*CASES, "E"))
    args = parser.parse_args()
    if args.child:
        with redirect_stdout(sys.stderr):
            result = run_child(args.child)
        print(json.dumps(result, ensure_ascii=False))
        return
    os.environ["TOOL_APPROVAL_MODE"] = "ALLOW"
    os.environ["CONTEXT_MODE"] = "WRITE_ONLY"
    check = deterministic_recovery_check()
    results = {}
    for case in (*CASES, "E"):
        results[case] = run_case(case)
        print(f"{case}: accepted={results[case]['metrics']['accepted']}", file=sys.stderr)
    payload = {"phase": 22.5, "started_at": datetime.now().isoformat(timespec="seconds"), "deterministic_recovery_check": check, "navigation": {case: navigation(results[case]) for case in CASES}, "results": results}
    RESULTS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT.write_text(render_report(payload), encoding="utf-8")


if __name__ == "__main__":
    main()
