"""Score cached RCA predictions locally without contacting Azure or Foundry."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv

from evaluation.dataset import DATASET_PATH, load_dataset
from evaluation.generate_predictions import (
    DEFAULT_CACHE,
    _cache_records,
    _select_cases,
    refresh_prediction_record,
)
from rca.analyze import _evidence_tokens, build_agent_input

TARGETS = {
    "diagnosis_accuracy": {"operator": ">=", "value": 0.80},
    "abstention_recall": {"operator": ">=", "value": 0.90},
    "schema_validity": {"operator": ">=", "value": 1.00},
    "false_diagnosis_rate": {"operator": "<=", "value": 0.00},
}
DEFAULT_REPORT = Path("data/evaluations/report.json")


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def evaluate_predictions(
    cases: list[dict[str, Any]],
    prediction_records: list[dict[str, Any]],
    agent_version: str,
) -> dict[str, Any]:
    """Calculate the evaluation metrics while keeping validator rejections visible."""
    records_by_id: dict[str, dict[str, Any]] = {}
    for record in prediction_records:
        if record.get("agent_version") != agent_version:
            continue
        case_id = record.get("case_id")
        if not isinstance(case_id, str):
            raise ValueError("Prediction record has an invalid case ID.")
        if case_id in records_by_id:
            raise ValueError("Multiple predictions exist for the same case and agent version.")
        records_by_id[case_id] = record

    diagnosable = sum(case["expected"]["status"] == "diagnosed" for case in cases)
    insufficient = len(cases) - diagnosable
    correct_diagnoses = correct_abstentions = false_diagnoses = 0
    schema_valid_count = validator_rejections = accepted_diagnoses = 0
    diagnosable_with_parseable_prediction = prediction_attempts = 0
    evidence_claims = supported_claims = 0
    per_case: list[dict[str, Any]] = []

    for case in cases:
        source_record = records_by_id.get(case["id"])
        record = (
            refresh_prediction_record(case, source_record)
            if source_record is not None
            else None
        )
        prediction_attempts += int(record is not None)
        candidate: dict[str, Any] | None = None
        schema_valid = bool(record and record.get("schema_valid"))
        if schema_valid and record is not None:
            candidate = record.get("candidate")
            schema_valid = isinstance(candidate, dict)
        if schema_valid:
            schema_valid_count += 1

        expected = case["expected"]
        candidate_status = candidate.get("status") if candidate else None
        candidate_category = candidate.get("root_cause_category") if candidate else None
        diagnosis_correct: bool | None = None
        abstention_correct: bool | None = None
        false_diagnosis = False

        if expected["status"] == "diagnosed":
            if schema_valid:
                diagnosable_with_parseable_prediction += 1
                diagnosis_correct = bool(
                    candidate_status == "diagnosed"
                    and candidate_category == expected["root_cause_category"]
                )
                correct_diagnoses += int(diagnosis_correct)
                accepted_diagnoses += int(bool(
                    record and record.get("outcome") == "accepted" and candidate_status == "diagnosed"
                ))
        else:
            abstention_correct = bool(
                schema_valid
                and candidate_status == "insufficient_evidence"
                and candidate_category is None
            )
            correct_abstentions += int(abstention_correct)
            false_diagnosis = bool(schema_valid and candidate_status == "diagnosed")
            false_diagnoses += int(false_diagnosis)

        claim_count = supported_count = 0
        if schema_valid and candidate is not None:
            evidence = candidate.get("evidence", [])
            allowed = _evidence_tokens(build_agent_input(case["incident"]))
            claim_count = len(evidence)
            supported_count = sum(item in allowed for item in evidence)
            evidence_claims += claim_count
            supported_claims += supported_count
        rejected = bool(record and record.get("outcome") == "validator_rejected")
        validator_rejections += int(rejected)

        per_case.append({
            "case_id": case["id"],
            "outcome": record.get("outcome") if record else "missing_prediction",
            "schema_valid": schema_valid,
            "expected_status": expected["status"],
            "expected_category": expected["root_cause_category"],
            "predicted_status": candidate_status,
            "predicted_category": candidate_category,
            "diagnosis_correct": diagnosis_correct,
            "abstention_correct": abstention_correct,
            "false_diagnosis": false_diagnosis,
            "evidence_claims": claim_count,
            "supported_evidence_claims": supported_count,
            "validator_rejected": rejected,
        })

    total = len(cases)
    metrics = {
        "diagnosis_accuracy": _rate(
            correct_diagnoses, diagnosable_with_parseable_prediction
        ),
        "valid_diagnosis_coverage": _rate(accepted_diagnoses, diagnosable),
        "abstention_recall": _rate(correct_abstentions, insufficient),
        "false_diagnosis_rate": _rate(false_diagnoses, insufficient),
        "schema_validity": _rate(schema_valid_count, prediction_attempts),
        "evidence_grounding": _rate(supported_claims, evidence_claims),
        "validator_rejection_rate": _rate(validator_rejections, prediction_attempts),
    }
    targets: dict[str, dict[str, Any]] = {}
    for name, target in TARGETS.items():
        value = metrics[name]
        target_value = target["value"]
        met = None if value is None else (
            value >= target_value if target["operator"] == ">=" else value <= target_value
        )
        targets[name] = {**target, "met": met}

    return {
        "agent_version": agent_version,
        "case_count": total,
        "metrics": metrics,
        "counts": {
            "diagnosable_cases": diagnosable,
            "diagnosable_cases_with_parseable_prediction": diagnosable_with_parseable_prediction,
            "correct_diagnoses": correct_diagnoses,
            "validator_accepted_diagnoses": accepted_diagnoses,
            "insufficient_evidence_cases": insufficient,
            "correct_abstentions": correct_abstentions,
            "false_diagnoses": false_diagnoses,
            "schema_valid_predictions": schema_valid_count,
            "prediction_attempts": prediction_attempts,
            "missing_predictions": sum(case["id"] not in records_by_id for case in cases),
            "evidence_claims": evidence_claims,
            "supported_evidence_claims": supported_claims,
            "validator_rejections": validator_rejections,
        },
        "metric_counts": {
            "diagnosis_accuracy": {
                "numerator": correct_diagnoses,
                "denominator": diagnosable_with_parseable_prediction,
            },
            "valid_diagnosis_coverage": {
                "numerator": accepted_diagnoses,
                "denominator": diagnosable,
            },
            "abstention_recall": {
                "numerator": correct_abstentions,
                "denominator": insufficient,
            },
            "false_diagnosis_rate": {
                "numerator": false_diagnoses,
                "denominator": insufficient,
            },
            "schema_validity": {
                "numerator": schema_valid_count,
                "denominator": prediction_attempts,
            },
            "evidence_grounding": {
                "numerator": supported_claims,
                "denominator": evidence_claims,
            },
            "validator_rejection_rate": {
                "numerator": validator_rejections,
                "denominator": prediction_attempts,
            },
        },
        "targets": targets,
        "per_case": per_case,
        "interpretation": (
            "A 24-case engineering benchmark is not a statistical reliability claim. "
            "Diagnosis accuracy uses parseable predictions for diagnosable cases; "
            "valid diagnosis coverage uses all diagnosable cases and counts only "
            "validator-accepted diagnoses. Validator rejections are reported separately."
        ),
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    """Score selected cached cases offline; no model or Azure client is created."""
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-version", default=os.environ.get("FOUNDRY_AGENT_VERSION", "2").strip())
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int)
    selection.add_argument("--case-id", action="append")
    args = parser.parse_args(argv)
    try:
        cases = load_dataset(args.dataset)
        if args.limit is not None or args.case_id is not None:
            cases = _select_cases(cases, False, args.limit, args.case_id)
        records = _cache_records(args.predictions)
        report = evaluate_predictions(cases, records, args.agent_version)
        _write_report(args.output, report)
        print(json.dumps({"case_count": report["case_count"], "metrics": report["metrics"]}, allow_nan=False))
        return 0
    except (ValueError, OSError):
        print("Scoring failed. Check the dataset and prediction cache.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
