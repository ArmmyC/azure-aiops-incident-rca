"""Load and strictly validate the small, tracked RCA evaluation dataset."""

import json
import math
from pathlib import Path
from typing import Any

DATASET_PATH = Path(__file__).with_name("dataset.jsonl")
CATEGORY_LABELS = (
    None,
    "dependency_timeout",
    "application_exception",
    "dependency_latency",
)
CASES_PER_CATEGORY = 6
TOTAL_CASES = 24


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def _contains_operation_id(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in {"operation_id", "operationid"}
            or _contains_operation_id(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_operation_id(child) for child in value)
    return False


def _contains_nonfinite_number(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_nonfinite_number(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_nonfinite_number(child) for child in value)
    return isinstance(value, float) and not math.isfinite(value)


def _validate_case(case: object, line_number: int) -> dict[str, Any]:
    if not isinstance(case, dict) or set(case) != {"id", "incident", "expected"}:
        raise ValueError(f"Dataset line {line_number} must contain only id, incident, and expected.")
    case_id = case["id"]
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError(f"Dataset line {line_number} has an invalid id.")
    incident = case["incident"]
    expected = case["expected"]
    if not isinstance(incident, dict):
        raise ValueError(f"Dataset line {line_number} incident must be an object.")
    if _contains_nonfinite_number(incident):
        raise ValueError(f"Dataset line {line_number} contains a non-finite number.")
    if not isinstance(expected, dict) or set(expected) != {"status", "root_cause_category"}:
        raise ValueError(f"Dataset line {line_number} expected label has an invalid shape.")
    status = expected["status"]
    category = expected["root_cause_category"]
    if status == "diagnosed" and category not in CATEGORY_LABELS[1:]:
        raise ValueError(f"Dataset line {line_number} has an invalid diagnosis category.")
    if status == "insufficient_evidence" and category is not None:
        raise ValueError(f"Dataset line {line_number} abstention must have a null category.")
    if status not in {"diagnosed", "insufficient_evidence"}:
        raise ValueError(f"Dataset line {line_number} has an invalid expected status.")
    if _contains_operation_id(incident):
        raise ValueError(f"Dataset line {line_number} must not contain an operation ID.")
    return case


def load_dataset(path: str | Path = DATASET_PATH) -> list[dict[str, Any]]:
    """Read exactly 24 uniquely labeled cases with six examples per category."""
    dataset_path = Path(path)
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    try:
        lines = dataset_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("Could not read evaluation dataset.") from exc

    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise ValueError(f"Dataset line {line_number} is blank.")
        try:
            row = json.loads(line, parse_constant=_reject_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Dataset line {line_number} is not valid finite JSON.") from exc
        case = _validate_case(row, line_number)
        if case["id"] in seen_ids:
            raise ValueError(f"Dataset contains duplicate id {case['id']!r}.")
        seen_ids.add(case["id"])
        cases.append(case)

    counts = {category: 0 for category in CATEGORY_LABELS}
    for case in cases:
        counts[case["expected"]["root_cause_category"]] += 1
    if len(cases) != TOTAL_CASES or any(
        count != CASES_PER_CATEGORY for count in counts.values()
    ):
        raise ValueError("Dataset must contain exactly 24 cases with six per category.")
    return cases
