"""Collect three independent live FAIL-grace continuations from the fixed prefix."""

from __future__ import annotations

import json
from pathlib import Path

from .phase24_1r import _run


OUTPUT = Path(__file__).with_name("phase24_2_results.json")


def summarize(result: dict) -> dict:
    events = [
        event for event in result["trace"]["events"]
        if event.get("turn") in (9, 10, 11) and event.get("action") == "tool_call"
    ]
    turns = []
    for turn in (9, 10, 11):
        calls = [event for event in events if event["turn"] == turn]
        actions = []
        for event in calls:
            tool = event["tool"]
            if event["classification"] == "BLOCKED_DUPLICATE":
                category = "REDUNDANT"
            elif tool in ("apply_patch", "write_file"):
                category = "EDIT"
            elif tool == "run_command":
                category = "VERIFY"
            elif tool == "finish_task":
                category = "FINISH"
            elif tool in ("read_file", "search_text", "list_files"):
                category = "DIAGNOSE"
            else:
                category = "OTHER"
            actions.append({
                "tool": tool,
                "category": category,
                "classification": event["classification"],
                "executed": event["executed"],
                "exit_code": event.get("exit_code"),
                "write_target": event.get("write_target"),
            })
        turns.append({
            "turn": turn,
            "actions": actions,
            "token_usage": result["recovery_token_usage"][turn - 9]
            if len(result["recovery_token_usage"]) > turn - 9 else None,
        })
    mutations = [event for event in events if event["tool"] in ("apply_patch", "write_file") and event["executed"]]
    tests = [event for event in events if event["tool"] == "run_command"]
    return {
        "fixed_prefix_id": result["fixed_prefix_id"],
        "boundary": result["boundary"],
        "valid_real_run": result["valid_real_run"],
        "runtime_errors": result["runtime_errors"],
        "model_calls": result["recovery_model_calls"],
        "tool_calls": len(events),
        "tokens": result["recovery_total_tokens"],
        "patch_calls": sum(event["tool"] == "apply_patch" for event in events),
        "write_calls": sum(event["tool"] == "write_file" for event in events),
        "read_calls": sum(event["tool"] == "read_file" for event in events),
        "search_calls": sum(event["tool"] == "search_text" for event in events),
        "duplicate_blocked": sum(event["classification"] == "BLOCKED_DUPLICATE" for event in events),
        "second_mutation": len(mutations) >= 2,
        "required_test_ran": bool(tests),
        "required_test_exit_codes": [event.get("exit_code") for event in tests],
        "finish_task_called": bool(result["finish_attempts"]),
        "artifact_passed": result["artifact_passed"],
        "interaction_completed": result["interaction_completed"],
        "agent_self_verified": result["agent_self_verified"],
        "accepted": result["accepted"],
        "status": result["status"],
        "verifier": result["verifier"],
        "turns": turns,
        "trace_events": events,
    }


def main() -> None:
    saved = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {"runs": []}
    for slot in range(len(saved["runs"]) + 1, 4):
        attempts = []
        for attempt in range(2):
            result = _run("FAIL")
            attempts.append(summarize(result))
            if result["valid_real_run"]:
                break
        saved["runs"].append({"slot": slot, "attempts": attempts, "result": attempts[-1]})
        OUTPUT.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"slot": slot, "attempts": len(attempts), "result": {
            key: attempts[-1][key] for key in ("valid_real_run", "model_calls", "tool_calls", "tokens", "status", "artifact_passed", "accepted")
        }}, ensure_ascii=False), flush=True)
        if not result["valid_real_run"]:
            raise RuntimeError(f"Slot {slot} has no valid real continuation after one retry")


if __name__ == "__main__":
    main()
