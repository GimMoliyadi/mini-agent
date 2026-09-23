"""Provider-free simulation of a proposed stage-aware FAIL recovery budget.

Only preclassified deterministic tool outcomes are inputs. This module never
imports the Agent Runtime or invokes a model, tool, or required test.
"""

from dataclasses import dataclass
import json
from pathlib import Path


NORMAL_CALLS = 8
RECOVERY_CALLS = 7
HARD_CEILING = NORMAL_CALLS + RECOVERY_CALLS
MAX_REPAIRS = 2
MAX_VERIFICATIONS = 2
MAX_FINISH_ATTEMPTS = 1
MAX_DETOURS = 2
SAVED_REQUIRED_TEST = {
    "command": "python",
    "args": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_coupons.py", "-q"],
    "cwd": ".",
}


@dataclass(frozen=True)
class Outcome:
    status: str
    stage: str
    model_calls: int
    repairs: int
    verifications: int
    finish_attempts: int
    detours: int
    stages: tuple[str, ...]


def simulate(events: list[str], *, boundary: str = "FAIL") -> Outcome:
    """Replay one action per model reply after the normal eight-call boundary.

    EDIT denotes an actual workspace mutation; TEST_* denotes an executed,
    exact, non-timed-out required test. FINISH_* is the existing Gate result.
    A pending script is allowed so saved traces can be replayed as prefixes.
    """
    if boundary not in {"FAIL", "PASS", "NONE"}:
        raise ValueError(boundary)
    stage = {"FAIL": "REPAIR_NEEDED", "PASS": "FINISH_NEEDED", "NONE": "NORMAL"}[boundary]
    calls = NORMAL_CALLS
    repairs = verifications = finishes = detours = 0
    stages = [stage]
    status = "PENDING" if boundary != "NONE" else "LIMIT_REACHED"
    for event in events:
        if stage in {"FINISHED", "STOPPED", "NORMAL"}:
            break
        if calls >= HARD_CEILING:
            stage, status = "STOPPED", "LIMIT_REACHED"
            break
        calls += 1
        if event == "FINISH_ACCEPTED":
            finishes += 1
            stage, status = "FINISHED", "FINISHED"
        elif event == "EDIT":
            repairs += 1
            stage = "VERIFY_NEEDED"
        elif event == "TEST_FAIL" and stage in {"VERIFY_NEEDED", "FINISH_NEEDED"}:
            verifications += 1
            stage = "REPAIR_NEEDED"
        elif event == "TEST_PASS" and stage in {"VERIFY_NEEDED", "FINISH_NEEDED"}:
            verifications += 1
            stage = "FINISH_NEEDED"
        elif event == "FINISH_REJECTED" and stage == "FINISH_NEEDED":
            finishes += 1
        elif event in {"FINISH_REJECTED", "TEST_FAIL", "TEST_PASS", "READ", "SEARCH", "OTHER"}:
            detours += 1
        else:
            raise ValueError(event)

        if stage != "FINISHED" and (
            calls >= HARD_CEILING
            or detours > MAX_DETOURS
            or repairs > MAX_REPAIRS
            or verifications > MAX_VERIFICATIONS
            or finishes >= MAX_FINISH_ATTEMPTS
            or (stage == "REPAIR_NEEDED" and (repairs >= MAX_REPAIRS or verifications >= MAX_VERIFICATIONS))
            or (stage == "VERIFY_NEEDED" and verifications >= MAX_VERIFICATIONS)
        ):
            stage, status = "STOPPED", "LIMIT_REACHED"
        stages.append(stage)
    return Outcome(status, stage, calls, repairs, verifications, finishes, detours, tuple(stages))


def trace_events(slot: dict) -> list[str]:
    """Classify the saved Phase 24.2 tool trace; no model replay occurs."""
    result = slot["result"]
    assert result["boundary"]["recovery_grace"] == "FAIL"
    events = []
    for turn in result["turns"]:
        assert len(turn["actions"]) == 1
        action = turn["actions"][0]
        tool = action["tool"]
        if tool in {"apply_patch", "write_file"} and action["executed"]:
            events.append("EDIT")
        elif tool == "run_command" and action["executed"]:
            detailed = next(event for event in result["trace_events"] if event["turn"] == turn["turn"] and event["tool"] == tool)
            assert json.loads(detailed["arguments"]) == SAVED_REQUIRED_TEST
            assert action["exit_code"] in {"0", "1"}
            events.append("TEST_PASS" if action["exit_code"] == "0" else "TEST_FAIL")
        elif tool == "finish_task":
            events.append("FINISH_ACCEPTED" if action["classification"] == "FINISH_ACCEPTED" else "FINISH_REJECTED")
        elif tool in {"read_file", "search_text", "list_files"}:
            events.append("READ" if tool == "read_file" else "SEARCH")
        else:
            events.append("OTHER")
    return events


def main() -> None:
    cases = {
        "A": (["EDIT", "TEST_PASS", "FINISH_ACCEPTED"], "FINISHED", 11),
        "B": (["EDIT", "FINISH_REJECTED", "TEST_PASS", "FINISH_ACCEPTED"], "FINISHED", 12),
        "C": (["EDIT", "TEST_FAIL", "EDIT", "TEST_PASS", "FINISH_ACCEPTED"], "FINISHED", 13),
        "D": (["EDIT", "TEST_FAIL", "EDIT", "TEST_FAIL", "FINISH_ACCEPTED"], "LIMIT_REACHED", 12),
        "E": (["READ", "EDIT", "TEST_PASS", "FINISH_ACCEPTED"], "FINISHED", 12),
        "F": (["FINISH_REJECTED", "FINISH_ACCEPTED"], "LIMIT_REACHED", 9),
    }
    for name, (events, expected_status, expected_calls) in cases.items():
        outcome = simulate(events, boundary="PASS" if name == "F" else "FAIL")
        assert (outcome.status, outcome.model_calls) == (expected_status, expected_calls)
        print(f"Case {name}: {outcome.status}, calls={outcome.model_calls}, stages={' -> '.join(outcome.stages)}")

    ordinary = simulate(["OTHER"] * 20, boundary="NONE")
    assert (ordinary.status, ordinary.model_calls, ordinary.stage) == ("LIMIT_REACHED", 8, "NORMAL")
    print("Case G: LIMIT_REACHED, calls=8, no recovery")

    ceiling = simulate(["READ", "SEARCH", "EDIT", "TEST_FAIL", "EDIT", "TEST_PASS", "FINISH_ACCEPTED"])
    assert (ceiling.status, ceiling.model_calls) == ("FINISHED", HARD_CEILING)
    assert simulate(["READ", "SEARCH", "OTHER", "EDIT"]).model_calls == 11
    assert simulate(["EDIT", "TEST_PASS", "FINISH_REJECTED", "FINISH_ACCEPTED"]).model_calls == 11
    assert simulate(["EDIT", "TEST_PASS", "TEST_FAIL"]).stage == "STOPPED"
    print(f"Ceiling check: accepted finish at call {HARD_CEILING}; detour and finish retries bounded")

    saved = json.loads(Path(__file__).with_name("phase24_2_results.json").read_text(encoding="utf-8"))
    expected = {1: ("FINISH_NEEDED", 11), 2: ("VERIFY_NEEDED", 11), 3: ("FINISHED", 11)}
    for slot in saved["runs"]:
        events = trace_events(slot)
        outcome = simulate(events)
        assert (outcome.stage, outcome.model_calls) == expected[slot["slot"]]
        print(f"Real Run {slot['slot']}: {' -> '.join(events)} => {outcome.stage}, calls={outcome.model_calls}")


if __name__ == "__main__":
    main()
