import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.dataset import CATEGORY_LABELS, load_dataset
from evaluation.generate_predictions import get_or_generate_prediction
from evaluation.judge import JudgeResult, build_judge_input, get_or_run_judgment, run_judge_case
from evaluation.score_predictions import evaluate_predictions
from rca.analyze import RCAReport, analyze_incident, build_agent_input, validate_report


def diagnosed(category, evidence, **changes):
    return RCAReport(
        status="diagnosed",
        root_cause="Evidence-supported explanation",
        root_cause_category=category,
        confidence="high",
        evidence=evidence,
        recommended_checks=["Inspect the relevant service logs"],
        **changes,
    )


def timeout_incident():
    return {
        "operation_id": "private-operation",
        "request": {"status_code": 504, "duration_ms": 1007},
        "dependency": {"name": "search_service", "duration_ms": 1004, "success": False},
    }


def timeout_report():
    return {
        "status": "diagnosed",
        "root_cause": "Search service dependency timeout",
        "root_cause_category": "dependency_timeout",
        "confidence": "high",
        "evidence": [
            "request.status_code=504",
            'dependency.name="search_service"',
            "dependency.success=false",
        ],
        "recommended_checks": ["Inspect search service timeout logs"],
    }


def make_case(case_id, incident, status, category):
    return {
        "id": case_id,
        "incident": incident,
        "expected": {"status": status, "root_cause_category": category},
    }


def fake_client(output):
    return SimpleNamespace(
        responses=SimpleNamespace(
            create=Mock(return_value=SimpleNamespace(output_text=output))
        )
    )


def test_dataset_has_exact_balanced_scenario_counts_and_real_examples():
    cases = load_dataset()
    assert len(cases) == 24
    counts = {category: 0 for category in CATEGORY_LABELS}
    for case in cases:
        label = case["expected"]["root_cause_category"]
        counts[label] += 1
    assert counts == {
        "dependency_timeout": 6,
        "application_exception": 6,
        "dependency_latency": 6,
        None: 6,
    }
    assert len({case["id"] for case in cases}) == 24
    assert any(
        case["incident"].get("request", {}).get("status_code") == 504
        and case["incident"].get("request", {}).get("duration_ms") == 1007
        and case["incident"].get("dependency", {}).get("duration_ms") == 1004
        for case in cases
    )
    assert any(
        case["incident"].get("request", {}).get("status_code") == 404
        and not case["incident"].get("dependency", {}).get("name")
        and case["expected"]["status"] == "insufficient_evidence"
        for case in cases
    )


def test_dataset_rejects_duplicate_ids_and_invalid_category(tmp_path):
    path = tmp_path / "bad.jsonl"
    row = make_case("same", {
        "request": {"status_code": 504},
        "dependency": {"name": "search_service", "success": False},
    }, "diagnosed", "dependency_timeout")
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_dataset(path)
    row["expected"]["root_cause_category"] = "fabricated"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="category"):
        load_dataset(path)


def test_v2_report_requires_category_for_diagnosis_and_null_for_abstention():
    with pytest.raises(ValidationError):
        RCAReport.model_validate({
            "status": "diagnosed", "root_cause": "Timeout", "confidence": "high",
            "evidence": [], "recommended_checks": [],
        })
    with pytest.raises(ValidationError):
        RCAReport(
            status="insufficient_evidence", root_cause=None,
            root_cause_category="dependency_timeout", confidence="low",
            evidence=[], recommended_checks=[],
        )


def test_dependency_timeout_requires_matching_504_and_failed_dependency_evidence():
    report = diagnosed(
        "dependency_timeout",
        ["request.status_code=504", 'dependency.name="search_service"', "dependency.success=false"],
    )
    assert validate_report(timeout_incident(), report) is report
    with pytest.raises(ValueError, match="category"):
        validate_report(
            {"request": {"status_code": 504}, "dependency": {"name": None, "success": None}},
            diagnosed("dependency_timeout", [
                "request.status_code=504", "dependency.name=null", "dependency.success=null",
            ]),
        )


def test_application_exception_requires_http_500_and_exception_type():
    incident = {"request": {
        "status_code": 500, "exception_type": "KeyError", "exception_code": "missing_config",
    }}
    report = diagnosed(
        "application_exception",
        ["request.status_code=500", 'request.exception_type="KeyError"'],
    )
    assert validate_report(incident, report) is report
    with pytest.raises(ValueError, match="category"):
        validate_report(
            {"request": {"status_code": 500}},
            diagnosed("application_exception", ["request.status_code=500"]),
        )


def test_dependency_latency_requires_successful_two_times_baseline_and_50ms_delta():
    incident = {
        "request": {"status_code": 200, "duration_ms": 350},
        "dependency": {
            "name": "catalog_service", "duration_ms": 300,
            "baseline_duration_ms": 100, "success": True,
        },
    }
    report = diagnosed(
        "dependency_latency",
        [
            "request.status_code=200",
            'dependency.name="catalog_service"', "dependency.success=true",
            "dependency.duration_ms=300", "dependency.baseline_duration_ms=100",
        ],
    )
    assert validate_report(incident, report) is report
    with pytest.raises(ValueError, match="required supporting evidence"):
        validate_report(incident, diagnosed(
            "dependency_latency",
            [
                'dependency.name="catalog_service"', "dependency.success=true",
                "dependency.duration_ms=300", "dependency.baseline_duration_ms=100",
            ],
        ))
    incident["dependency"]["duration_ms"] = 149
    with pytest.raises(ValueError, match="category"):
        validate_report(incident, diagnosed(
            "dependency_latency",
            ['dependency.name="catalog_service"', "dependency.success=true"],
        ))


def test_whitelist_includes_only_approved_exception_and_baseline_scalars():
    incident = {
        "operation_id": "never-send",
        "request": {"status_code": 500, "exception_type": "KeyError", "exception_code": "missing_config",
                    "expected": "private"},
        "dependency": {"name": "catalog_service", "baseline_duration_ms": 100,
                       "operation_id": "hidden", "unknown": "private"},
        "expected": {"status": "diagnosed"},
    }
    payload = build_agent_input(incident)
    assert payload == {
        "request": {"status_code": 500, "exception_type": "KeyError", "exception_code": "missing_config"},
        "dependency": {"name": "catalog_service", "baseline_duration_ms": 100},
    }
    assert "never-send" not in json.dumps(payload)
    assert "private" not in json.dumps(payload)


def test_analyzer_pins_version_and_sends_only_incident_telemetry():
    case = make_case("timeout-001", timeout_incident(), "diagnosed", "dependency_timeout")
    client = fake_client(json.dumps(timeout_report()))
    report = analyze_incident(case["incident"], client, "my-agent", "2")
    assert report.root_cause_category == "dependency_timeout"
    args = client.responses.create.call_args.kwargs
    assert args["extra_body"]["agent_reference"] == {
        "name": "my-agent", "type": "agent_reference", "version": "2",
    }
    assert json.loads(args["input"]) == build_agent_input(case["incident"])
    assert "expected" not in args["input"]
    assert "private-operation" not in args["input"]


def test_prediction_cache_is_keyed_by_case_and_agent_version(tmp_path):
    case = make_case("timeout-001", timeout_incident(), "diagnosed", "dependency_timeout")
    client = fake_client(json.dumps(timeout_report()))
    cache = tmp_path / "predictions.jsonl"
    first = get_or_generate_prediction(case, client, "my-agent", "2", cache)
    second = get_or_generate_prediction(case, client, "my-agent", "2", cache)
    assert first == second
    client.responses.create.assert_called_once()
    assert first["case_id"] == "timeout-001"
    assert first["agent_version"] == "2"
    assert first["outcome"] == "accepted"


def test_generator_caches_validator_rejections_without_repeating_inference(tmp_path):
    case = make_case("timeout-001", timeout_incident(), "diagnosed", "dependency_timeout")
    invalid_evidence = timeout_report() | {"evidence": ["invented=claim"]}
    client = fake_client(json.dumps(invalid_evidence))
    cache = tmp_path / "predictions.jsonl"
    first = get_or_generate_prediction(case, client, "my-agent", "2", cache)
    second = get_or_generate_prediction(case, client, "my-agent", "2", cache)
    assert first["outcome"] == second["outcome"] == "validator_rejected"
    client.responses.create.assert_called_once()


def test_scorer_reports_requested_metrics_and_keeps_rejection_separate():
    diagnosed_case = make_case("exception-001", {
        "request": {"status_code": 500, "exception_type": "KeyError"},
    }, "diagnosed", "application_exception")
    timeout_case = make_case("timeout-001", timeout_incident(), "diagnosed", "dependency_timeout")
    abstain_case = make_case("abstain-001", {"request": {"status_code": 404}},
                             "insufficient_evidence", None)
    false_case = make_case("abstain-002", {"request": {"status_code": 500}},
                           "insufficient_evidence", None)
    exception_prediction = {
        "status": "diagnosed", "root_cause": "KeyError", "root_cause_category": "application_exception",
        "confidence": "high", "evidence": [
            "request.status_code=500", 'request.exception_type="KeyError"',
        ],
        "recommended_checks": [],
    }
    timeout_prediction = timeout_report() | {"evidence": ["request.status_code=504", "invented=claim"]}
    abstention_prediction = {
        "status": "insufficient_evidence", "root_cause": None, "root_cause_category": None,
        "confidence": "low", "evidence": ["request.status_code=404"], "recommended_checks": [],
    }
    false_prediction = {
        "status": "diagnosed", "root_cause": "Exception", "root_cause_category": "application_exception",
        "confidence": "low", "evidence": ["request.status_code=500"], "recommended_checks": [],
    }
    records = [
        {"case_id": "exception-001", "agent_version": "2", "outcome": "accepted",
         "schema_valid": True, "candidate": exception_prediction},
        {"case_id": "timeout-001", "agent_version": "2", "outcome": "accepted",
         "schema_valid": True, "candidate": timeout_prediction},
        {"case_id": "abstain-001", "agent_version": "2", "outcome": "accepted",
         "schema_valid": True, "candidate": abstention_prediction},
        {"case_id": "abstain-002", "agent_version": "2", "outcome": "accepted",
         "schema_valid": True, "candidate": false_prediction},
    ]
    result = evaluate_predictions(
        [diagnosed_case, timeout_case, abstain_case, false_case], records, "2"
    )
    assert result["metrics"]["diagnosis_accuracy"] == 1.0
    assert result["metrics"]["abstention_recall"] == 0.5
    assert result["metrics"]["false_diagnosis_rate"] == 0.5
    assert result["metrics"]["schema_validity"] == 1.0
    assert result["metrics"]["evidence_grounding"] == pytest.approx(5 / 6)
    assert result["metrics"]["validator_rejection_rate"] == 0.5
    assert result["metrics"]["valid_diagnosis_coverage"] == 0.5
    assert result["counts"]["validator_rejections"] == 2
    assert result["per_case"][1]["outcome"] == "validator_rejected"


def test_scorer_uses_attempt_denominators_and_excludes_missing_from_accuracy():
    parsed_case = make_case("exception-001", {
        "request": {"status_code": 500, "exception_type": "KeyError"},
    }, "diagnosed", "application_exception")
    missing_case = make_case("exception-002", {
        "request": {"status_code": 500, "exception_type": "ValueError"},
    }, "diagnosed", "application_exception")
    insufficient_case = make_case("abstain-001", {"request": {"status_code": 404}},
                                  "insufficient_evidence", None)
    valid_exception = {
        "status": "diagnosed", "root_cause": "KeyError",
        "root_cause_category": "application_exception", "confidence": "high",
        "evidence": ['request.status_code=500', 'request.exception_type="KeyError"'],
        "recommended_checks": [],
    }
    records = [
        {"case_id": "exception-001", "agent_version": "2", "outcome": "accepted",
         "schema_valid": True, "candidate": valid_exception},
        {"case_id": "abstain-001", "agent_version": "2", "outcome": "schema_invalid",
         "schema_valid": False, "candidate": None},
    ]

    result = evaluate_predictions(
        [parsed_case, missing_case, insufficient_case], records, "2"
    )

    assert result["metrics"]["diagnosis_accuracy"] == 1.0
    assert result["metrics"]["valid_diagnosis_coverage"] == 0.5
    assert result["metrics"]["schema_validity"] == 0.5
    assert result["metrics"]["validator_rejection_rate"] == 0.0
    assert result["counts"]["prediction_attempts"] == 2
    assert result["counts"]["diagnosable_cases_with_parseable_prediction"] == 1
    assert result["counts"]["missing_predictions"] == 1


def test_judge_receives_telemetry_prediction_expected_label_and_rubric_only():
    case = make_case("timeout-001", timeout_incident(), "diagnosed", "dependency_timeout")
    prediction = timeout_report()
    payload = build_judge_input(case, prediction)
    assert payload["incident"] == build_agent_input(case["incident"])
    assert payload["prediction"] == prediction
    assert payload["expected"] == case["expected"]
    assert "private-operation" not in json.dumps(payload)
    client = fake_client(json.dumps({
        "score": 5, "explanation": "Category and evidence match the incident.",
        "unsupported_claims": [],
    }))
    result = run_judge_case(case, prediction, client, "gpt-5-mini")
    assert isinstance(result, JudgeResult)
    assert result.score == 5
    args = client.responses.create.call_args.kwargs
    assert args["model"] == "gpt-5-mini"
    judge_input = json.loads(args["input"])
    assert judge_input["expected"] == case["expected"]


def test_judge_cache_is_separate_and_keyed_by_agent_version_and_model(tmp_path):
    case = make_case("timeout-001", timeout_incident(), "diagnosed", "dependency_timeout")
    client = fake_client(json.dumps({
        "score": 5, "explanation": "Correct and grounded.", "unsupported_claims": [],
    }))
    cache = tmp_path / "judgments.jsonl"
    first = get_or_run_judgment(case, timeout_report(), client, "2", "gpt-5-mini", cache)
    second = get_or_run_judgment(case, timeout_report(), client, "2", "gpt-5-mini", cache)
    assert first == second
    assert first["agent_version"] == "2"
    assert first["judge_model"] == "gpt-5-mini"
    client.responses.create.assert_called_once()


@pytest.mark.parametrize("score", [0, 6, "5"])
def test_judge_rejects_invalid_ordinal_scores(score):
    with pytest.raises(ValidationError):
        JudgeResult.model_validate({
            "score": score, "explanation": "out of range", "unsupported_claims": [],
        })
