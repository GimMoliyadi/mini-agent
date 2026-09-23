"""Pure, provider-free simulation of proposed Phase 24 budget decisions.

Events are already classified from a completed model/tool round. This module
does not import or modify the Agent Runtime.
"""

from dataclasses import dataclass


BASE_TURNS = 8
HARD_CEILING = 11
FAIL_GRACE = 3
PASS_GRACE = 1


@dataclass(frozen=True)
class Outcome:
    status: str
    model_calls: int
    grace_trigger: str | None
    consumed_observations: tuple[int, ...]
    stranded_observation: int | None


def simulate(events: list[str]) -> Outcome:
    """Apply a single, nonrenewable boundary grant after each completed round.

    TEST_FAIL means an executed exact required test returned a nonzero exit.
    TEST_PASS_FRESH means the exact test succeeded after the last mutation.
    These classifications must be verified from Runtime facts in any future
    implementation; the simulation deliberately has no tool execution.
    """
    allowed = BASE_TURNS
    trigger = None
    consumed = []
    pending = None
    for turn, event in enumerate(events, 1):
        if turn > allowed or turn > HARD_CEILING:
            break
        if pending is not None:
            consumed.append(pending)
            pending = None
        if event == "FINISH_ACCEPTED":
            return Outcome("FINISHED", turn, trigger, tuple(consumed), None)
        if event in {"TEST_FAIL", "TEST_PASS_FRESH", "READ", "SEARCH"}:
            pending = turn
        if turn == BASE_TURNS:
            if event == "TEST_FAIL":
                trigger = event
                allowed = min(HARD_CEILING, BASE_TURNS + FAIL_GRACE)
            elif event == "TEST_PASS_FRESH":
                trigger = event
                allowed = min(HARD_CEILING, BASE_TURNS + PASS_GRACE)
        if turn == allowed:
            return Outcome("LIMIT_REACHED", turn, trigger, tuple(consumed), pending)
    raise ValueError("Script ended before a terminal budget decision")


def cases() -> dict[str, Outcome]:
    prefix = ["OTHER"] * 7
    return {
        "A_fail_consumed": simulate(prefix + ["TEST_FAIL", "PATCH", "TEST_PASS_FRESH", "FINISH_ACCEPTED"]),
        "B_pass_finish": simulate(prefix + ["TEST_PASS_FRESH", "FINISH_ACCEPTED"]),
        "C_search_stops": simulate(prefix + ["SEARCH", "SEARCH"]),
        "C_read_stops": simulate(prefix + ["READ", "READ"]),
        "D_repeat_fail_bounded": simulate(prefix + ["TEST_FAIL", "PATCH", "TEST_FAIL", "TEST_FAIL", "FINISH_ACCEPTED"]),
        "E_finish_at_hard_cap": simulate(prefix + ["TEST_FAIL", "PATCH", "TEST_PASS_FRESH", "FINISH_ACCEPTED"]),
        "finish_at_base_cap": simulate(prefix + ["FINISH_ACCEPTED"]),
        "patch_fail_stops": simulate(prefix + ["PATCH_FAIL", "FINISH_ACCEPTED"]),
        "policy_reject_stops": simulate(prefix + ["POLICY_REJECT", "FINISH_ACCEPTED"]),
        "stale_pass_stops": simulate(prefix + ["TEST_PASS_STALE", "FINISH_ACCEPTED"]),
    }


if __name__ == "__main__":
    results = cases()
    assert results["A_fail_consumed"].status == "FINISHED"
    assert 8 in results["A_fail_consumed"].consumed_observations
    assert results["B_pass_finish"].status == "FINISHED"
    assert results["B_pass_finish"].model_calls == 9
    assert results["C_search_stops"].model_calls == BASE_TURNS
    assert results["C_read_stops"].model_calls == BASE_TURNS
    assert results["D_repeat_fail_bounded"].model_calls == HARD_CEILING
    assert results["D_repeat_fail_bounded"].status == "LIMIT_REACHED"
    assert results["D_repeat_fail_bounded"].stranded_observation == HARD_CEILING
    assert results["E_finish_at_hard_cap"].status == "FINISHED"
    assert results["E_finish_at_hard_cap"].model_calls == HARD_CEILING
    assert results["finish_at_base_cap"].status == "FINISHED"
    assert all(results[name].model_calls == BASE_TURNS for name in (
        "patch_fail_stops", "policy_reject_stops", "stale_pass_stops"
    ))
    assert all(outcome.model_calls <= HARD_CEILING for outcome in results.values())
    for name, outcome in results.items():
        print(f"{name}: {outcome}")
