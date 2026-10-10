"""Validate one incident analysis from an existing managed Foundry agent."""

import json
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


class RCAReport(BaseModel):
    """The exact JSON response contract; unsupported responses fail closed."""

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


def build_agent_input(incident: dict[str, object]) -> dict[str, object]:
    """Copy only the approved evidence fields, excluding identifiers and labels."""
    return {
        key: incident[key]
        for key in ("timestamp", "request", "dependency", "window_summary")
        if key in incident
    }


def _evidence_tokens(value: object, path: str = "") -> set[str]:
    """Render exact scalar observations using JSON values and dotted paths."""
    tokens: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            tokens.update(_evidence_tokens(child, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            tokens.update(_evidence_tokens(child, f"{path}[{index}]"))
    elif value is None or isinstance(value, (str, int, float, bool)):
        tokens.add(f"{path}={json.dumps(value, allow_nan=False)}")
    return tokens


def validate_report(incident: dict[str, object], report: RCAReport) -> RCAReport:
    """Enforce grounded evidence and abstention for an uncorrelated HTTP 404."""
    # Revalidate even if the caller constructed or mutated a model without validation.
    RCAReport.model_validate(report.model_dump())
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
    return report


def analyze_incident(
    incident: dict[str, object], openai_client: object, agent_name: str
) -> RCAReport:
    """Call the existing named agent once and validate only its JSON output."""
    response = openai_client.responses.create(
        input=json.dumps(build_agent_input(incident)),
        extra_body={"agent_reference": {"name": agent_name, "type": "agent_reference"}},
    )
    report = RCAReport.model_validate_json(response.output_text)
    return validate_report(incident, report)


def main() -> int:
    """Analyze the local incident artifact and print only the validated report."""
    credential = project = openai_client = None
    try:
        load_dotenv(".env")
        endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
        if not endpoint:
            raise ValueError("Set FOUNDRY_PROJECT_ENDPOINT to the Foundry project endpoint.")
        agent_name = os.environ.get("FOUNDRY_AGENT_NAME", "").strip() or "aiops-rca-agent"
        incident = json.loads(Path("data/incidents/latest.json").read_text(encoding="utf-8"))
        if not isinstance(incident, dict):
            raise ValueError("The incident artifact must contain a JSON object.")
        credential = DefaultAzureCredential()
        project = AIProjectClient(endpoint=endpoint, credential=credential)
        openai_client = project.get_openai_client()
        report = analyze_incident(incident, openai_client, agent_name)
        print(report.model_dump_json(indent=2))
        return 0
    except (AzureError, OpenAIError):
        print("Foundry analysis failed. Check Azure sign-in and project access.", file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print(
            "Analysis failed. Check FOUNDRY_PROJECT_ENDPOINT, the incident artifact, "
            "and the agent JSON response and evidence.",
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
