import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rca.analyze import RCAReport, analyze_incident, build_agent_input, validate_report


def report_data(**changes):
    return {
        "status": "diagnosed",
        "root_cause": "Correlated dependency failure",
        "confidence": "medium",
        "evidence": ["request.status_code=504", "dependency.success=false"],
        "recommended_checks": ["Inspect dependency timeout configuration"],
        **changes,
    }


def incident_data():
    return {
        "operation_id": "op-1",
        "timestamp": "2026-10-11T00:00:00Z",
        "request": {"status_code": 504, "duration_ms": 5000},
        "dependency": {"name": "search_service", "duration_ms": 4999, "success": False},
        "window_summary": {"failed_requests": 1, "p95_latency_ms": 5000},
        "injected_fault": "dependency_timeout",
        "expected_root_cause": "search timeout",
        "unknown": "private metadata",
    }


@pytest.mark.parametrize("dependency", [None, {}, {"name": None}, {"name": ""}, {"name": "  "}])
def test_404_without_dependency_name_rejects_diagnosis(dependency):
    incident = {"request": {"status_code": 404}, "dependency": dependency}
    report = RCAReport(**report_data(evidence=["request.status_code=404"]))
    with pytest.raises(ValueError, match="404"):
        validate_report(incident, report)


def test_404_with_absent_dependency_rejects_diagnosis():
    with pytest.raises(ValueError, match="404"):
        validate_report({"request": {"status_code": 404}}, RCAReport(**report_data(evidence=[])))


def test_404_without_dependency_accepts_abstention():
    report = RCAReport(**report_data(status="insufficient_evidence", root_cause=None,
                                    evidence=["request.status_code=404"]))
    assert validate_report({"request": {"status_code": 404}}, report) is report


def test_404_with_named_dependency_accepts_supported_diagnosis():
    report = RCAReport(**report_data(evidence=["request.status_code=404"]))
    assert validate_report({"request": {"status_code": 404},
                            "dependency": {"name": "search_service"}}, report) is report


def test_insufficient_evidence_requires_null_root_cause():
    with pytest.raises(ValidationError):
        RCAReport(**report_data(status="insufficient_evidence"))


@pytest.mark.parametrize("root_cause", [None, "", "  "])
def test_diagnosed_requires_non_empty_root_cause(root_cause):
    with pytest.raises(ValidationError):
        RCAReport(**report_data(root_cause=root_cause))


@pytest.mark.parametrize("changes", [{"status": "maybe"}, {"confidence": "certain"},
                                      {"evidence": "HTTP 504"}, {"root_cause": 123},
                                      {"recommended_checks": [123]}])
def test_response_contract_rejects_invalid_field_types(changes):
    with pytest.raises(ValidationError):
        RCAReport(**report_data(**changes))


def test_response_rejects_extra_fields():
    with pytest.raises(ValidationError):
        RCAReport.model_validate_json(json.dumps(report_data(extra="not allowed")))


def test_agent_input_excludes_labels_and_operation_id():
    payload = build_agent_input(incident_data())
    assert set(payload) == {"timestamp", "request", "dependency", "window_summary"}
    serialized = json.dumps(payload)
    for excluded in ["operation_id", "op-1", "injected_fault", "expected_root_cause", "unknown"]:
        assert excluded not in serialized
    assert build_agent_input({"request": {"status_code": 504}}) == {"request": {"status_code": 504}}


@pytest.mark.parametrize("section", ["request", "dependency", "window_summary"])
def test_agent_input_excludes_nested_metadata_from_outgoing_request(section):
    incident = incident_data()
    incident[section].update({
        "operation_id": "private-op",
        "injected_fault": "hidden fault",
        "expected_root_cause": "hidden answer",
        "unknown": {"secret": "private metadata"},
    })
    payload = build_agent_input(incident)
    assert payload == build_agent_input(incident_data())
    client = fake_client(json.dumps(report_data()))
    analyze_incident(incident, client, "my-agent")
    outgoing = client.responses.create.call_args.kwargs["input"]
    assert json.loads(outgoing) == payload
    for excluded in ("private-op", "hidden fault", "hidden answer", "private metadata"):
        assert excluded not in outgoing


@pytest.mark.parametrize("section", ["request", "dependency", "window_summary"])
@pytest.mark.parametrize("label", ["operation_id", "injected_fault", "expected_root_cause", "unknown"])
def test_nested_metadata_cannot_be_report_evidence(section, label):
    incident = incident_data()
    incident[section][label] = "hidden answer"
    report = RCAReport(**report_data(evidence=[f'{section}.{label}="hidden answer"']))
    with pytest.raises(ValueError, match="evidence"):
        validate_report(incident, report)


def test_container_values_cannot_smuggle_metadata_through_scalar_fields():
    incident = incident_data()
    incident["timestamp"] = {"operation_id": "private-op"}
    incident["request"]["duration_ms"] = {"expected_root_cause": "hidden answer"}
    incident["dependency"]["duration_ms"] = ["hidden fault"]
    payload = build_agent_input(incident)
    assert "timestamp" not in payload
    assert "duration_ms" not in payload["request"]
    assert "duration_ms" not in payload["dependency"]
    report = RCAReport(**report_data(evidence=['request.duration_ms.expected_root_cause="hidden answer"']))
    with pytest.raises(ValueError, match="evidence"):
        validate_report(incident, report)


@pytest.mark.parametrize("token", ["HTTP 504", "request.status_code=500", "operation_id=op-1",
                                   "dependency.success=True", "unknown=private metadata"])
def test_unsupported_evidence_tokens_rejected(token):
    with pytest.raises(ValueError, match="evidence"):
        validate_report(incident_data(), RCAReport(**report_data(evidence=[token])))


def test_exact_scalar_evidence_tokens_accepted():
    incident = incident_data()
    incident["dependency"]["duration_ms"] = None
    report = RCAReport(**report_data(evidence=["dependency.name=\"search_service\"",
        "dependency.duration_ms=null", "request.duration_ms=5000",
        "dependency.success=false", "timestamp=\"2026-10-11T00:00:00Z\"",
        "window_summary.failed_requests=1"]))
    assert validate_report(incident, report) is report


def fake_client(output):
    return SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=SimpleNamespace(output_text=output))))


def test_analyzer_calls_stable_agent_reference_with_whitelisted_input():
    client = fake_client(json.dumps(report_data()))
    result = analyze_incident(incident_data(), client, "my-agent")
    assert result.root_cause == "Correlated dependency failure"
    client.responses.create.assert_called_once_with(
        input=json.dumps(build_agent_input(incident_data())),
        extra_body={"agent_reference": {"name": "my-agent", "type": "agent_reference"}},
    )


@pytest.mark.parametrize("output", ["not JSON", "```json\n{}\n```", "{}", json.dumps(report_data(extra=True))])
def test_analyzer_rejects_malformed_or_invalid_json(output):
    with pytest.raises(ValidationError):
        analyze_incident(incident_data(), fake_client(output), "my-agent")


def test_analyzer_validates_response_against_incident():
    with pytest.raises(ValueError, match="evidence"):
        analyze_incident(incident_data(), fake_client(json.dumps(report_data(evidence=["invented fact"]))), "my-agent")


@pytest.fixture
def cli_setup(monkeypatch, tmp_path):
    from rca import analyze, create_agent

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/test")
    monkeypatch.delenv("FOUNDRY_AGENT_NAME", raising=False)
    monkeypatch.delenv("FOUNDRY_MODEL", raising=False)
    path = tmp_path / "data/incidents/latest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(incident_data()), encoding="utf-8")
    credential = Mock()
    project = Mock()
    client = fake_client(json.dumps(report_data()))
    client.close = Mock()
    project.get_openai_client.return_value = client
    project.agents.create_version.return_value = SimpleNamespace(name="aiops-rca-agent", version="1")
    for module in (analyze, create_agent):
        monkeypatch.setattr(module, "load_dotenv", Mock())
        monkeypatch.setattr(module, "DefaultAzureCredential", Mock(return_value=credential))
        monkeypatch.setattr(module, "AIProjectClient", Mock(return_value=project))
    return analyze, create_agent, credential, project, client


def test_creator_creates_one_version_and_closes_resources(cli_setup, capsys):
    _, creator, credential, project, _ = cli_setup
    assert creator.main() == 0
    project.agents.create_version.assert_called_once()
    kwargs = project.agents.create_version.call_args.kwargs
    assert kwargs["agent_name"] == "aiops-rca-agent"
    assert kwargs["definition"].model == "gpt-5-mini"
    from rca.instructions import RCA_INSTRUCTIONS
    assert kwargs["definition"].instructions == RCA_INSTRUCTIONS
    assert "aiops-rca-agent" in capsys.readouterr().out
    project.close.assert_called_once()
    credential.close.assert_called_once()


def test_analyzer_cli_prints_only_validated_json_and_never_creates_version(cli_setup, capsys):
    analyzer, _, credential, project, client = cli_setup
    assert analyzer.main() == 0
    assert json.loads(capsys.readouterr().out) == report_data()
    project.agents.create_version.assert_not_called()
    project.get_openai_client.assert_called_once_with()
    client.close.assert_called_once()
    project.close.assert_called_once()
    credential.close.assert_called_once()


@pytest.mark.parametrize("which", [0, 1])
def test_cli_requires_project_endpoint_before_creating_identity(cli_setup, monkeypatch, which, capsys):
    module = cli_setup[which]
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT")
    assert module.main() == 1
    module.DefaultAzureCredential.assert_not_called()
    captured = capsys.readouterr()
    assert not captured.out
    assert "FOUNDRY_PROJECT_ENDPOINT" in captured.err


def test_analyzer_closes_all_resources_on_invalid_response(cli_setup, capsys):
    analyzer, _, credential, project, client = cli_setup
    client.responses.create.return_value.output_text = "not JSON"
    assert analyzer.main() == 1
    assert not capsys.readouterr().out
    client.close.assert_called_once()
    project.close.assert_called_once()
    credential.close.assert_called_once()


def test_creator_closes_resources_on_azure_failure(cli_setup, capsys):
    from azure.core.exceptions import AzureError
    _, creator, credential, project, _ = cli_setup
    project.agents.create_version.side_effect = AzureError("sensitive error detail")
    assert creator.main() == 1
    captured = capsys.readouterr()
    assert "sensitive error detail" not in captured.err
    assert not captured.out
    project.close.assert_called_once()
    credential.close.assert_called_once()
