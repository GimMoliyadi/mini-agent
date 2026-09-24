"""Repeat the saved turn-eight FAIL continuation after mutation-aware deduplication."""

import json
from pathlib import Path
import sys

from .phase24_1r import _run


if __name__ == "__main__":
    run = int(sys.argv[1])
    if run not in {1, 2}:
        raise ValueError("Run must be 1 or 2")
    output = Path(__file__).with_name(f"phase24_5_results_{run}.json")
    if output.exists():
        raise FileExistsError(output)

    result = _run("FAIL")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "fixed_prefix_id", "boundary", "runtime_errors", "total_model_calls",
        "recovery_total_tokens", "status", "accepted", "artifact_passed",
        "interaction_completed", "agent_self_verified",
    )}, ensure_ascii=False))
