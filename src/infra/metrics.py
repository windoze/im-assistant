"""Structured in-process counters emitted through JSON logs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from src.infra.log import get_logger

logger = get_logger("im_assistant.metrics")
_COUNTERS: defaultdict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)


def increment_counter(
    name: str,
    *,
    value: int = 1,
    labels: Mapping[str, object] | None = None,
) -> int:
    """Increment one labeled counter and emit the updated value as a structured log."""

    metric_name = _non_empty_string(name, "metric.name")
    if isinstance(value, bool) or value <= 0:
        raise ValueError("metric value must be a positive integer")

    metric_labels = _metric_labels(labels or {})
    key = (metric_name, tuple(sorted(metric_labels.items())))
    _COUNTERS[key] += value
    count = _COUNTERS[key]
    logger.info(
        "runtime_metric",
        extra={
            "metric_name": metric_name,
            "metric_value": value,
            "metric_count": count,
            "metric_labels": dict(metric_labels),
        },
    )
    return count


def reset_metrics_for_tests() -> None:
    """Clear in-memory counters for tests that need deterministic counts."""

    _COUNTERS.clear()


def _metric_labels(labels: Mapping[str, object]) -> dict[str, str]:
    return {
        _non_empty_string(key, "metric.label"): _label_value(value) for key, value in labels.items()
    }


def _label_value(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        text = str(value)
        return text if text.strip() else "empty"
    return type(value).__name__


def _non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()
