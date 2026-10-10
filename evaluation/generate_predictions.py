"""Generate and cache explicitly selected, version-pinned RCA predictions."""

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from openai import OpenAIError
from pydantic import ValidationError

from evaluation.dataset import DATASET_PATH, load_dataset
from rca.analyze import RCAReport, build_agent_input, validate_report

DEFAULT_CACHE = Path("data/evaluations/predictions.jsonl")


def _cache_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    keys: set[tuple[str, str]] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Prediction cache line {line_number} is invalid JSON.") from exc
        if not isinstance(record, dict):
            raise ValueError(f"Prediction cache line {line_number} must be an object.")
        case_id = record.get("case_id")
        version = record.get("agent_version")
        if not isinstance(case_id, str) or not isinstance(version, str):
            raise ValueError(f"Prediction cache line {line_number} has an invalid key.")
        key = (case_id, version)
        if key in keys:
            raise ValueError("Prediction cache contains duplicate case/version records.")
        keys.add(key)
        records.append(record)
    return records


def _write_cache(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    contents = "".join(
        json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n"
        for record in records
    )
    temporary.write_text(contents, encoding="utf-8")
    temporary.replace(path)


def _request_record(
    case: dict[str, Any], openai_client: object, agent_name: str, agent_version: str
) -> dict[str, Any]:
    payload = build_agent_input(case["incident"])
    response = openai_client.responses.create(
        input=json.dumps(payload, allow_nan=False),
        extra_body={
            "agent_reference": {
                "name": agent_name,
                "type": "agent_reference",
                "version": agent_version,
            }
        },
    )
    raw_output = response.output_text or ""
    record: dict[str, Any] = {
        "case_id": case["id"],
        "agent_version": agent_version,
        "outcome": "schema_invalid",
        "schema_valid": False,
        "validator_accepted": False,
        "candidate": None,
        "raw_output": raw_output,
    }
    try:
        report = RCAReport.model_validate_json(raw_output)
    except ValidationError:
        record["validation_error"] = "Invalid version 2 report schema or JSON"
        return record

    record["schema_valid"] = True
    candidate = report.model_dump(mode="json")
    record["candidate"] = candidate
    try:
        validate_report(case["incident"], report)
    except ValueError as exc:
        record["outcome"] = "validator_rejected"
        record["rejection"] = str(exc)
        return record
    record["outcome"] = "accepted"
    record["validator_accepted"] = True
    return record


def refresh_prediction_record(
    case: dict[str, Any], source_record: dict[str, Any]
) -> dict[str, Any]:
    """Reapply the current v2 schema and incident validator to a cached result."""
    record = dict(source_record)
    try:
        report = RCAReport.model_validate(record.get("candidate"))
    except ValidationError:
        if record.get("outcome") != "request_failed":
            record["outcome"] = "schema_invalid"
            record["schema_valid"] = False
            record["validator_accepted"] = False
            record["candidate"] = None
            record.pop("rejection", None)
        return record

    record["schema_valid"] = True
    record["candidate"] = report.model_dump(mode="json")
    try:
        validate_report(case["incident"], report)
    except ValueError as exc:
        record["outcome"] = "validator_rejected"
        record["validator_accepted"] = False
        record["rejection"] = str(exc)
    else:
        record["outcome"] = "accepted"
        record["validator_accepted"] = True
        record.pop("rejection", None)
    return record


def get_or_generate_prediction(
    case: dict[str, Any],
    openai_client: object,
    agent_name: str,
    agent_version: str,
    cache_path: str | Path = DEFAULT_CACHE,
) -> dict[str, Any]:
    """Return a cached case/version result or make exactly one inference request."""
    path = Path(cache_path)
    records = _cache_records(path)
    key = (case["id"], agent_version)
    for index, record in enumerate(records):
        if (record["case_id"], record["agent_version"]) == key:
            refreshed = refresh_prediction_record(case, record)
            if refreshed != record:
                records[index] = refreshed
                _write_cache(path, records)
            return refreshed

    try:
        record = _request_record(case, openai_client, agent_name, agent_version)
    except Exception as exc:
        # Cache ambiguous failures too, so a rerun cannot silently duplicate a paid request.
        record = {
            "case_id": case["id"],
            "agent_version": agent_version,
            "outcome": "request_failed",
            "schema_valid": False,
            "validator_accepted": False,
            "candidate": None,
            "error_type": type(exc).__name__,
        }
    records.append(record)
    _write_cache(path, records)
    return record


def _select_cases(
    cases: list[dict[str, Any]], all_cases: bool, limit: int | None, case_ids: list[str] | None
) -> list[dict[str, Any]]:
    if all_cases:
        return cases
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit must be a positive integer.")
        return cases[:limit]
    wanted = case_ids or []
    if len(wanted) != len(set(wanted)):
        raise ValueError("Case IDs must not be repeated.")
    indexed = {case["id"]: case for case in cases}
    missing = [case_id for case_id in wanted if case_id not in indexed]
    if missing:
        raise ValueError(f"Unknown case ID: {missing[0]}")
    return [indexed[case_id] for case_id in wanted]


def main(argv: Sequence[str] | None = None) -> int:
    """Run only an explicit case subset; full inference requires --all."""
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-version", default=os.environ.get("FOUNDRY_AGENT_VERSION", "2").strip()
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true", help="Run all uncached cases.")
    selection.add_argument("--limit", type=int, help="Run the first N cases in dataset order.")
    selection.add_argument("--case-id", action="append", help="Run one named case; repeatable.")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args(argv)

    credential = project = openai_client = None
    try:
        if not args.agent_version:
            raise ValueError("--agent-version must not be empty.")
        endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
        if not endpoint:
            raise ValueError("Set FOUNDRY_PROJECT_ENDPOINT in .env.")
        agent_name = os.environ.get("FOUNDRY_AGENT_NAME", "").strip() or "aiops-rca-agent"
        cases = _select_cases(
            load_dataset(args.dataset), args.all, args.limit, args.case_id
        )
        credential = DefaultAzureCredential()
        project = AIProjectClient(endpoint=endpoint, credential=credential)
        openai_client = project.get_openai_client()
        outcomes: Counter[str] = Counter()
        for case in cases:
            record = get_or_generate_prediction(
                case, openai_client, agent_name, args.agent_version, args.cache
            )
            outcomes[record["outcome"]] += 1
        print(json.dumps({"selected_cases": len(cases), "outcomes": outcomes}, default=dict))
        return 1 if outcomes["request_failed"] else 0
    except (AzureError, OpenAIError):
        print("Foundry prediction setup failed. Check Azure sign-in and project access.", file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print("Prediction generation failed. Check the dataset and local Foundry configuration.", file=sys.stderr)
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
