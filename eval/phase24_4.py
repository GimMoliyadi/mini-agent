"""One live stage-aware continuation of the saved fixed FAIL prefix."""

import json
from pathlib import Path
import sys

from .phase24_1r import _run


OUTPUT = Path(__file__).with_name("phase24_4_results.json")


if __name__ == "__main__":
    result = _run("FAIL")
    output = OUTPUT if len(sys.argv) == 1 else OUTPUT.with_name("phase24_4_results_2.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        key: result[key] for key in (
            "fixed_prefix_id", "boundary", "runtime_errors", "total_model_calls",
            "recovery_total_tokens", "status", "accepted", "artifact_passed",
            "interaction_completed", "agent_self_verified",
        )
    }, ensure_ascii=False))
