import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.ci_gate import DEFAULT_DATASET, DEFAULT_PREDICTIONS, main


def test_synthetic_v2_fixture_passes_the_offline_gate(capsys):
    assert main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["passed"] is True
    assert result["case_count"] == 8
    assert all(check["passed"] for check in result["checks"].values())


def test_gate_fails_when_a_prediction_becomes_ungrounded(tmp_path, capsys):
    rows = [
        json.loads(line)
        for line in DEFAULT_PREDICTIONS.read_text(encoding="utf-8").splitlines()
    ]
    prediction = next(row for row in rows if row["candidate"]["status"] == "diagnosed")
    prediction["candidate"]["evidence"] = ["request.status_code=999"]
    broken_predictions = tmp_path / "broken.jsonl"
    broken_predictions.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    assert main(["--predictions", str(broken_predictions)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["passed"] is False
    assert result["checks"]["validator_rejection_rate"]["passed"] is False
    assert result["checks"]["evidence_grounding"]["passed"] is False
    assert Path(DEFAULT_DATASET).exists()


def test_gate_fails_when_any_fixture_prediction_is_missing(tmp_path, capsys):
    rows = [
        json.loads(line)
        for line in DEFAULT_PREDICTIONS.read_text(encoding="utf-8").splitlines()
    ]
    incomplete_predictions = tmp_path / "incomplete.jsonl"
    incomplete_predictions.write_text(
        "".join(json.dumps(row) + "\n" for row in rows[:-1]), encoding="utf-8"
    )

    assert main(["--predictions", str(incomplete_predictions)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["checks"]["prediction_completeness"]["passed"] is False
