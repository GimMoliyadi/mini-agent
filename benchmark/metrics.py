"""Metrics for the reproducible, provider-free Benchmark v1.

The benchmark records scripted model turns. Provider usage is intentionally
unknown for those replies, so ``total_tokens`` stays null; a separate
``scripted_token_estimate`` is retained only for local diagnostics.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Iterable, Mapping


def _numbers(records: Iterable[Mapping[str, object]], key: str) -> list[float]:
    values: list[float] = []
    for record in records:
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        values.append(float(value))
    return values


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def percentile(values: Iterable[float], percentage: float) -> float | None:
    """Return a nearest-rank percentile without interpolation surprises."""

    ordered = sorted(values)
    if not ordered:
        return None
    if not 0 <= percentage <= 100:
        raise ValueError("percentage must be between 0 and 100")
    rank = max(1, math.ceil((percentage / 100) * len(ordered)))
    return ordered[rank - 1]


def aggregate(records: list[Mapping[str, object]]) -> dict[str, object]:
    """Aggregate the published Benchmark v1 metrics.

    Runtime-error rows are retained in the result and counted explicitly. They
    are excluded from call/token distributions so an infrastructure failure
    cannot look like a cheap successful agent run.
    """

    total = len(records)
    valid = [record for record in records if not bool(record.get("runtime_error"))]
    accepted = sum(bool(record.get("accepted")) for record in records)
    expected_positive = [
        record
        for record in records
        if bool(record.get("expected_acceptance"))
    ]
    positive_accepted = sum(bool(record.get("accepted")) for record in expected_positive)
    unexpected = sum(bool(record.get("unexpected_modification")) for record in records)
    max_steps = sum(bool(record.get("max_steps_reached")) for record in records)
    manual_review = sum(bool(record.get("manual_review_required")) for record in records)
    deterministic_match = sum(
        bool(record.get("deterministic_match")) for record in records
    )

    model_calls = _numbers(valid, "model_calls")
    tool_calls = _numbers(valid, "tool_calls")
    tokens = _numbers(valid, "total_tokens")
    scripted_tokens = _numbers(valid, "scripted_token_estimate")

    def middle(values: list[float]) -> float | int | None:
        if not values:
            return None
        value = median(values)
        return int(value) if value.is_integer() else value

    p95_tokens = percentile(tokens, 95)
    if isinstance(p95_tokens, float) and p95_tokens.is_integer():
        p95_tokens = int(p95_tokens)

    return {
        "tasks": total,
        "valid_tasks": len(valid),
        "acceptance_rate": _rate(accepted, total),
        "positive_acceptance_rate": _rate(positive_accepted, len(expected_positive)),
        "deterministic_match_rate": _rate(deterministic_match, total),
        "median_model_calls": middle(model_calls),
        "median_tool_calls": middle(tool_calls),
        "median_tokens": middle(tokens),
        "p95_tokens": p95_tokens,
        "median_scripted_tokens": middle(scripted_tokens),
        "max_step_rate": _rate(max_steps, total),
        "unexpected_modification_rate": _rate(unexpected, total),
        "runtime_error_rate": _rate(total - len(valid), total),
        "runtime_errors": total - len(valid),
        "manual_review_required": manual_review,
        "manual_review_completed": 0,
        "manual_review_pending": manual_review,
        "scripted_token_note": (
            "total_tokens is unavailable for scripted turns and remains null; "
            "scripted_token_estimate is a separate diagnostic only."
        ),
    }


def render_metrics_table(metrics: Mapping[str, object]) -> str:
    """Render the compact table used by the generated report."""

    rows = (
        ("Acceptance Rate", metrics.get("acceptance_rate")),
        ("Positive Acceptance Rate", metrics.get("positive_acceptance_rate")),
        ("Median Model Calls", metrics.get("median_model_calls")),
        ("Median Tool Calls", metrics.get("median_tool_calls")),
        ("Median Tokens", metrics.get("median_tokens")),
        ("P95 Tokens", metrics.get("p95_tokens")),
        ("Median Scripted Tokens (diagnostic)", metrics.get("median_scripted_tokens")),
        ("Max-step Rate", metrics.get("max_step_rate")),
        ("Unexpected Modification Rate", metrics.get("unexpected_modification_rate")),
        ("Runtime Error Rate", metrics.get("runtime_error_rate")),
    )
    lines = ["| Metric | Value |", "| --- | ---: |"]
    lines.extend(f"| {name} | `{('null' if value is None else value)}` |" for name, value in rows)
    return "\n".join(lines)


__all__ = ["aggregate", "percentile", "render_metrics_table"]
