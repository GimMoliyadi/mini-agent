"""Bounded, deterministic continuation after a turn-eight required-test failure."""

from dataclasses import dataclass, field


HARD_CEILING = 15
MAX_REPAIRS = 2
MAX_VERIFICATIONS = 2
MAX_FINISHES = 1
MAX_NONPROGRESS_ROUNDS = 2
TEST_EVENTS = frozenset({"TEST", "TEST_PASS", "TEST_FAIL", "TEST_UNKNOWN"})
FINISH_EVENTS = frozenset({"FINISH", "FINISH_ACCEPTED", "FINISH_REJECTED"})


@dataclass
class Recovery:
    stage: str = "REPAIR_NEEDED"
    repairs: int = 0
    verifications: int = 0
    finishes: int = 0
    nonprogress: int = 0
    test_attempts: int = 0
    _round_advanced: bool = field(default=False, init=False, repr=False)
    _round_rejected: bool = field(default=False, init=False, repr=False)
    _stopped: bool = field(default=False, init=False, repr=False)

    def can_execute(self, event_kind: str) -> bool:
        if self._stopped or self._round_rejected or self.stage == "FINISHED":
            return False
        if self.finishes >= MAX_FINISHES or self.nonprogress > MAX_NONPROGRESS_ROUNDS:
            return False
        if self.stage != "FINISH_NEEDED" and self.test_attempts >= MAX_VERIFICATIONS:
            return False
        if self.stage == "REPAIR_NEEDED" and self.repairs >= MAX_REPAIRS:
            return False
        if event_kind == "MUTATION":
            return self.repairs < MAX_REPAIRS and self.test_attempts < MAX_VERIFICATIONS
        if event_kind in TEST_EVENTS:
            return self.test_attempts < MAX_VERIFICATIONS
        if event_kind == "FINISH_ACCEPTED":
            return self.stage == "FINISH_NEEDED"
        return True

    def record_event(self, event_kind: str) -> None:
        if not self.can_execute(event_kind):
            raise ValueError(f"恢复额度不允许执行事件：{event_kind}")
        if event_kind in {"TEST", "FINISH"}:
            raise ValueError(f"恢复记账需要实际执行结果：{event_kind}")
        if event_kind == "MUTATION":
            self.repairs += 1
            self.stage = "VERIFY_NEEDED"
            self._round_advanced = True
        elif event_kind in TEST_EVENTS:
            self._record_test(event_kind)
        elif event_kind in FINISH_EVENTS:
            self.finishes += int(self.stage == "FINISH_NEEDED")
            if event_kind == "FINISH_ACCEPTED":
                self.stage = "FINISHED"
                self._round_advanced = True

    def _record_test(self, event_kind: str) -> None:
        self.test_attempts += 1
        if event_kind == "TEST_UNKNOWN":
            if self.stage == "FINISH_NEEDED":
                self.stage = "VERIFY_NEEDED"
            return
        if self.stage == "VERIFY_NEEDED":
            self.verifications += 1
            self.stage = "FINISH_NEEDED" if event_kind == "TEST_PASS" else "REPAIR_NEEDED"
            self._round_advanced = True
        elif event_kind == "TEST_FAIL" and self.stage == "FINISH_NEEDED":
            self.stage = "REPAIR_NEEDED"
            self._round_advanced = True

    def end_round(self, turn: int) -> bool:
        if not self._round_advanced:
            self.nonprogress += 1
        self._round_advanced = False
        self._stopped = (
            self._stopped
            or self._round_rejected
            or turn > HARD_CEILING
            or (self.stage != "FINISHED" and (
                turn >= HARD_CEILING
                or self.finishes >= MAX_FINISHES
                or self.nonprogress > MAX_NONPROGRESS_ROUNDS
                or (self.stage == "REPAIR_NEEDED" and self.repairs >= MAX_REPAIRS)
                or (self.stage == "REPAIR_NEEDED" and self.verifications >= MAX_VERIFICATIONS)
                or (self.stage == "VERIFY_NEEDED" and self.test_attempts >= MAX_VERIFICATIONS)
            ))
        )
        return self._stopped

    def observe(self, events: list[str], turn: int) -> bool:
        for event in events:
            if self.can_execute(event):
                self.record_event(event)
            else:
                self._round_rejected = True
        return self.end_round(turn)
