"""Bounded, deterministic continuation after a turn-eight required-test failure."""

from dataclasses import dataclass


HARD_CEILING = 15


@dataclass
class Recovery:
    stage: str = "REPAIR_NEEDED"
    repairs: int = 0
    verifications: int = 0
    finishes: int = 0
    nonprogress: int = 0

    def observe(self, events: list[str], turn: int) -> bool:
        """Apply one model reply's executed events; return whether to stop."""
        advanced = False
        for event in events:
            if event == "MUTATION":
                self.repairs += 1
                self.stage = "VERIFY_NEEDED"
                advanced = True
            elif event in {"TEST_PASS", "TEST_FAIL"} and self.stage == "VERIFY_NEEDED":
                self.verifications += 1
                self.stage = "FINISH_NEEDED" if event == "TEST_PASS" else "REPAIR_NEEDED"
                advanced = True
            elif event == "TEST_FAIL" and self.stage == "FINISH_NEEDED":
                self.stage = "REPAIR_NEEDED"
                advanced = True
            elif event == "FINISH_ACCEPTED":
                self.finishes += int(self.stage == "FINISH_NEEDED")
                self.stage = "FINISHED"
                return False
            elif event == "FINISH_REJECTED" and self.stage == "FINISH_NEEDED":
                self.finishes += 1

        if not advanced:
            self.nonprogress += 1
        return (
            turn >= HARD_CEILING
            or self.nonprogress > 2
            or self.repairs > 2
            or self.verifications > 2
            or self.finishes >= 1
            or (self.stage == "REPAIR_NEEDED" and (self.repairs >= 2 or self.verifications >= 2))
            or (self.stage == "VERIFY_NEEDED" and self.verifications >= 2)
        )
