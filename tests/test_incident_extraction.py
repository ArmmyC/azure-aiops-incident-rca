import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etl import extract_incidents


def sample_frames():
    requests = pd.DataFrame(
        [
            {"OperationId": "op-404", "TimeGenerated": "2026-10-11T10:00:00Z", "ResultCode": 404,
             "DurationMs": 30, "Success": False, "ItemCount": 1},
            {"OperationId": "op-504", "TimeGenerated": "2026-10-11T09:00:00Z", "ResultCode": 504,
             "DurationMs": 1000, "Success": False, "ItemCount": 1},
            {"OperationId": "op-slow", "TimeGenerated": "2026-10-11T08:00:00Z", "ResultCode": 200,
             "DurationMs": 2000, "Success": True, "ItemCount": 1},
        ]
    )
    dependencies = pd.DataFrame(
        [
            {"OperationId": "op-504", "TimeGenerated": "2026-10-11T09:00:01Z", "Name": "search_service",
             "DurationMs": 1004, "Success": False},
        ]
    )
    return requests, dependencies


def test_default_selects_newest_failure_and_explicit_id_selects_older_request():
    requests, dependencies = sample_frames()

    report = extract_incidents.build_incident_report(requests, dependencies)
    assert report["request"]["status_code"] == 404
    assert report["dependency"]["name"] is None

    selected = extract_incidents.build_incident_report(requests, dependencies, operation_id="op-504")
    assert selected["request"]["status_code"] == 504
    assert selected["dependency"] == {
        "name": "search_service",
        "duration_ms": 1004.0,
        "success": False,
    }


def test_explicit_id_selects_successful_request():
    requests, dependencies = sample_frames()
    selected = extract_incidents.build_incident_report(requests, dependencies, operation_id="op-slow")
    assert selected["request"]["status_code"] == 200
    assert selected["operation_id"] == "op-slow"


def test_unknown_operation_id_returns_no_report():
    requests, dependencies = sample_frames()
    assert extract_incidents.build_incident_report(requests, dependencies, operation_id="missing") is None


def test_cli_unknown_operation_id_does_not_overwrite_latest(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_ANALYTICS_WORKSPACE_ID", "00000000-0000-0000-0000-000000000001")
    output_path = tmp_path / "data" / "incidents" / "latest.json"
    output_path.parent.mkdir(parents=True)
    output_path.write_text('{"existing": true}\n', encoding="utf-8")

    requests, dependencies = sample_frames()
    monkeypatch.setattr(extract_incidents, "load_query_frame", lambda client, workspace_id, query:
                        requests if query == extract_incidents.REQUEST_QUERY else dependencies)
    monkeypatch.setattr(extract_incidents, "DefaultAzureCredential", lambda: _Closable())
    monkeypatch.setattr(extract_incidents, "LogsQueryClient", lambda credential: _Closable())

    result = extract_incidents.main(["--operation-id", "missing"])
    assert result != 0
    assert output_path.read_text(encoding="utf-8") == '{"existing": true}\n'
    assert "No matching /simulate request" in capsys.readouterr().err


class _Closable:
    def close(self):
        pass
