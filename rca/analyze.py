"""Validate one incident analysis from a versioned managed Foundry agent."""

import json
import math
import os
import sys
from pathlib import Path
from typing import Literal, Self

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, model_validator

RootCauseCategory = Literal[
    "dependency_timeout", "application_exception", "dependency_latency"
]


class _RCAReportBase(BaseModel):
    """Shared strict JSON fields for the immutable v1 and v2 contracts."""

    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["diagnosed", "insufficient_evidence"]
    root_cause: str | None
    confidence: Literal["low", "medium", "high"]
    evidence: list[str]
    recommended_checks: list[str]

    @model_validator(mode="after")
    def validate_root_cause(self) -> Self:
        if self.status == "insufficient_evidence" and self.root_cause is not None:
            raise ValueError("insufficient_evidence requires a null root_cause")
        if self.status == "diagnosed" and (
            self.root_cause is None or not self.root_cause.strip()
        ):
            raise ValueError("diagnosed requires a non-empty root_cause")
        return self


class RCAReportV1(_RCAReportBase):
    """The preserved five-field response contract for agent version 1."""


class RCAReport(_RCAReportBase):
    """The version 2 response contract with a stable root-cause category."""

    root_cause_category: RootCauseCategory | None

    @model_validator(mode="after")
    def validate_category(self) -> Self:
        if self.status == "insufficient_evidence" and self.root_cause_category is not None:
            raise ValueError("insufficient_evidence requires a null root_cause_category")
        if self.status == "diagnosed" and self.root_cause_category is None:
            raise ValueError("diagnosed requires a non-null root_cause_category")
        return self


def build_agent_input(incident: dict[str, object]) -> dict[str, object]:
    """Copy only approved scalar telemetry, excluding labels and metadata."""
    fields = {
        "request": ("status_code", "duration_ms", "exception_type", "exception_code"),
        "dependency": (
            "name", "duration_ms", "success", "baseline_duration_ms",
        ),
        "window_summary": ("total_requests", "failed_requests", "p95_latency_ms"),
    }
    payload: dict[str, object] = {}
    if "timestamp" in incident and _is_scalar(incident["timestamp"]):
        payload["timestamp"] = incident["timestamp"]
    for section, allowed in fields.items():
        if section not in incident:
            continue
        value = incident[section]
        if isinstance(value, dict):
            payload[section] = {
                key: value[key] for key in allowed
                if key in value and _is_scalar(value[key])
            }
        elif value is None:
            payload[section] = None
    return payload


def _is_scalar(value: object) -> bool:
    """Prevent containers from hiding unapproved metadata in evidence fields."""
    return value is None or isinstance(value, (str, int, float, bool))


def _evidence_tokens(value: object, path: str = "") -> set[str]:
    """Render exact scalar observations using JSON values and dotted paths."""
    tokens: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            tokens.update(_evidence_tokens(child, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            tokens.update(_evidence_tokens(child, f"{path}[{index}]"))
    elif _is_scalar(value):
        tokens.add(f"{path}={json.dumps(value, allow_nan=False)}")
    return tokens


def _token(path: str, value: object) -> str:
    return f"{path}={json.dumps(value, allow_nan=False)}"


def _valid_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _category_evidence(
    incident: dict[str, object], report: RCAReport
) -> set[str]:
    """Return category-specific evidence requirements or reject unsupported labels."""
    request = incident.get("request")
    dependency = incident.get("dependency")
    request = request if isinstance(request, dict) else {}
    dependency = dependency if isinstance(dependency, dict) else {}
    category = report.root_cause_category

    if category == "dependency_timeout":
        name = dependency.get("name")
        if (
            request.get("status_code") != 504
            or not isinstance(name, str)
            or not name.strip()
            or dependency.get("success") is not False
        ):
            raise ValueError("dependency_timeout category requires HTTP 504 and a named failed dependency")
        return {
            _token("request.status_code", 504),
            _token("dependency.name", name),
            _token("dependency.success", False),
        }

    if category == "application_exception":
        exception_type = request.get("exception_type")
        if (
            request.get("status_code") != 500
            or not isinstance(exception_type, str)
            or not exception_type.strip()
        ):
            raise ValueError("application_exception category requires HTTP 500 and an exception type")
        return {
            _token("request.status_code", 500),
            _token("request.exception_type", exception_type),
        }

    if category == "dependency_latency":
        name = dependency.get("name")
        duration = dependency.get("duration_ms")
        baseline = dependency.get("baseline_duration_ms")
        if (
            request.get("status_code") != 200
            or not isinstance(name, str)
            or not name.strip()
            or dependency.get("success") is not True
            or not _valid_number(duration)
            or not _valid_number(baseline)
            or float(baseline) <= 0
            or float(duration) < 2 * float(baseline)
            or float(duration) - float(baseline) < 50
        ):
            raise ValueError(
                "dependency_latency category requires HTTP 200 and a successful dependency at least "
                "2x baseline and 50 ms above baseline"
            )
        return {
            _token("request.status_code", 200),
            _token("dependency.name", name),
            _token("dependency.success", True),
            _token("dependency.duration_ms", duration),
            _token("dependency.baseline_duration_ms", baseline),
        }

    raise ValueError("A diagnosed report requires a supported root-cause category")


def validate_report(
    incident: dict[str, object], report: RCAReport | RCAReportV1
) -> RCAReport | RCAReportV1:
    """Enforce evidence grounding, category support, and known abstention rules."""
    report_type = RCAReport if isinstance(report, RCAReport) else RCAReportV1
    report_type.model_validate(report.model_dump())
    allowed = _evidence_tokens(build_agent_input(incident))
    if any(item not in allowed for item in report.evidence):
        raise ValueError("Report contains unsupported evidence tokens")

    request = incident.get("request")
    dependency = incident.get("dependency")
    name = dependency.get("name") if isinstance(dependency, dict) else None
    has_dependency = isinstance(name, str) and bool(name.strip())
    if (
        isinstance(request, dict)
        and request.get("status_code") == 404
        and not has_dependency
        and report.status == "diagnosed"
    ):
        raise ValueError("HTTP 404 without dependency evidence requires abstention")

    if isinstance(report, RCAReport) and report.status == "diagnosed":
        required = _category_evidence(incident, report)
        if not required.issubset(set(report.evidence)):
            raise ValueError("Report category is missing required supporting evidence")
    return report


def analyze_incident(
    incident: dict[str, object],
    openai_client: object,
    agent_name: str,
    agent_version: str,
) -> RCAReport | RCAReportV1:
    """Call the explicitly selected agent version and validate its JSON output."""
    if agent_version not in {"1", "2"}:
        raise ValueError("agent_version must be '1' or '2'")
    response = openai_client.responses.create(
        input=json.dumps(build_agent_input(incident), allow_nan=False),
        extra_body={
            "agent_reference": {
                "name": agent_name,
                "type": "agent_reference",
                "version": agent_version,
            }
        },
    )
    report_type = RCAReportV1 if agent_version == "1" else RCAReport
    report = report_type.model_validate_json(response.output_text)
    return validate_report(incident, report)


def main() -> int:
    """Analyze the current local artifact with a pinned agent version."""
    credential = project = openai_client = None
    try:
        load_dotenv(".env")
        endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
        if not endpoint:
            raise ValueError("Set FOUNDRY_PROJECT_ENDPOINT to the Foundry project endpoint.")
        agent_name = os.environ.get("FOUNDRY_AGENT_NAME", "").strip() or "aiops-rca-agent"
        agent_version = os.environ.get("FOUNDRY_AGENT_VERSION", "2").strip()
        if agent_version not in {"1", "2"}:
            raise ValueError("FOUNDRY_AGENT_VERSION must be '1' or '2'.")
        incident = json.loads(Path("data/incidents/latest.json").read_text(encoding="utf-8"))
        if not isinstance(incident, dict):
            raise ValueError("The incident artifact must contain a JSON object.")
        credential = DefaultAzureCredential()
        project = AIProjectClient(endpoint=endpoint, credential=credential)
        openai_client = project.get_openai_client()
        report = analyze_incident(incident, openai_client, agent_name, agent_version)
        print(report.model_dump_json(indent=2))
        return 0
    except (AzureError, OpenAIError):
        print("Foundry analysis failed. Check Azure sign-in and project access.", file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print(
            "Analysis failed. Check FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_AGENT_VERSION, "
            "the incident artifact, and the agent JSON response and evidence.",
            file=sys.stderr,
        )
        return 1
    finally:
        try:
            if openai_client is not None:
                openai_client.close()
        finally:
            try:
                if project is not None:
                    project.close()
            finally:
                if credential is not None:
                    credential.close()


if __name__ == "__main__":
    raise SystemExit(main())
