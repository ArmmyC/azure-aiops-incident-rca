"""Run the sanitized, offline v2 RCA regression gate without model calls."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from evaluation.dataset import load_dataset
from evaluation.generate_predictions import DEFAULT_CACHE, _cache_records
from evaluation.score_predictions import evaluate_predictions

DEFAULT_DATASET = Path(__file__).with_name("fixtures") / "ci_dataset.jsonl"
DEFAULT_PREDICTIONS = Path(__file__).with_name("fixtures") / "agent_v2_predictions.jsonl"

GATES = {
    "diagnosis_accuracy": {"operator": ">=", "threshold": 0.80},
    "abstention_recall": {"operator": ">=", "threshold": 0.90},
    "false_diagnosis_rate": {"operator": "<=", "threshold": 0.00},
    "schema_validity": {"operator": ">=", "threshold": 1.00},
    "evidence_grounding": {"operator": ">=", "threshold": 1.00},
    "validator_rejection_rate": {"operator": "<=", "threshold": 0.00},
}


def evaluate_gate(report: dict[str, Any]) -> dict[str, Any]:
    """Return explicit pass/fail checks for complete predictions and each metric."""
    case_count = report["case_count"]
    counts = report["counts"]
    attempts = counts["prediction_attempts"]
    missing = counts["missing_predictions"]
    checks: dict[str, dict[str, Any]] = {
        "prediction_completeness": {
            "actual": {
                "case_count": case_count,
                "prediction_attempts": attempts,
                "missing_predictions": missing,
            },
            "passed": attempts == case_count and missing == 0,
        }
    }

    for name, rule in GATES.items():
        value = report["metrics"][name]
        threshold = rule["threshold"]
        passed = value is not None and (
            value >= threshold if rule["operator"] == ">=" else value <= threshold
        )
        checks[name] = {
            "actual": value,
            "operator": rule["operator"],
            "threshold": threshold,
            "passed": passed,
        }

    return {
        "passed": all(check["passed"] for check in checks.values()),
        "case_count": case_count,
        "checks": checks,
        "metrics": report["metrics"],
        "metric_counts": report["metric_counts"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Score checked-in predictions locally and return nonzero on any failed gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--agent-version", choices=("2",), default="2")
    args = parser.parse_args(argv)

    try:
        cases = load_dataset(args.dataset, require_full_benchmark=False)
        records = _cache_records(args.predictions)
        report = evaluate_predictions(cases, records, args.agent_version)
        result = evaluate_gate(report)
    except (OSError, ValueError):
        print("Offline RCA gate could not read its dataset or prediction fixture.", file=sys.stderr)
        return 1

    print(json.dumps(result, allow_nan=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
