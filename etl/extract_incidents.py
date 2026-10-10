"""Extract incident evidence from a bounded Azure Monitor workspace query."""

import json
import math
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pandas as pd
from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from azure.monitor.query import LogsQueryClient, LogsQueryPartialResult

QUERY_TIMESPAN = timedelta(hours=48)

REQUEST_QUERY = """
AppRequests
| where TimeGenerated > ago(48h)
| where Name contains "/simulate"
| project OperationId, TimeGenerated, ResultCode, DurationMs, Success, ItemCount
"""

DEPENDENCY_QUERY = """
AppDependencies
| where TimeGenerated > ago(48h)
| where Name contains "search_service"
| project OperationId, TimeGenerated, Name, DurationMs, Success
"""

QUERY_COLUMNS = {
    REQUEST_QUERY: [
        "OperationId", "TimeGenerated", "ResultCode", "DurationMs", "Success", "ItemCount"
    ],
    DEPENDENCY_QUERY: [
        "OperationId", "TimeGenerated", "Name", "DurationMs", "Success"
    ],
}


def load_query_frame(
    client: LogsQueryClient, workspace_id: str, query: str
) -> pd.DataFrame:
    """Return the first table, refusing incomplete query results."""
    result = client.query_workspace(workspace_id, query, timespan=QUERY_TIMESPAN)
    if isinstance(result, LogsQueryPartialResult):
        raise ValueError(f"Azure Monitor returned a partial query result: {result.partial_error}")
    if not result.tables:
        if query not in QUERY_COLUMNS:
            raise ValueError("Cannot determine columns for an empty result from an unknown query.")
        return pd.DataFrame(columns=QUERY_COLUMNS[query])
    table = result.tables[0]
    return pd.DataFrame(data=table.rows, columns=table.columns)


def _number_or_none(value: object) -> float | None:
    """Convert a scalar to a finite, JSON-safe number."""
    if pd.isna(value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def build_incident_report(
    requests: pd.DataFrame, dependencies: pd.DataFrame
) -> dict[str, object] | None:
    """Select the newest failed request and its longest matching dependency."""
    request_rows = requests.copy()
    dependency_rows = dependencies.copy()
    request_rows["TimeGenerated"] = pd.to_datetime(
        request_rows["TimeGenerated"], utc=True, errors="coerce"
    )
    request_rows["DurationMs"] = pd.to_numeric(
        request_rows["DurationMs"], errors="coerce"
    )
    request_rows["ResultCode"] = pd.to_numeric(
        request_rows["ResultCode"], errors="coerce"
    )
    request_rows["ItemCount"] = pd.to_numeric(
        request_rows["ItemCount"], errors="coerce"
    ).fillna(1)
    failed = request_rows["Success"].eq(False)
    total_requests = _number_or_none(request_rows["ItemCount"].sum())
    failed_requests = _number_or_none(request_rows.loc[failed, "ItemCount"].sum())
    p95_latency_ms = _number_or_none(request_rows["DurationMs"].dropna().quantile(0.95))
    if not failed.any():
        return None

    request_rows["OperationId"] = request_rows["OperationId"].astype("string")
    dependency_rows = dependency_rows.dropna(subset=["OperationId"])
    dependency_rows["OperationId"] = dependency_rows["OperationId"].astype("string")
    dependency_rows["TimeGenerated"] = pd.to_datetime(
        dependency_rows["TimeGenerated"], utc=True, errors="coerce"
    )
    dependency_rows["DurationMs"] = pd.to_numeric(
        dependency_rows["DurationMs"], errors="coerce"
    )
    request_rows = request_rows.rename(columns={
        "TimeGenerated": "request_timestamp",
        "DurationMs": "request_duration_ms",
        "Success": "request_success",
    })
    dependency_rows = dependency_rows.rename(columns={
        "TimeGenerated": "dependency_timestamp",
        "Name": "dependency_name",
        "DurationMs": "dependency_duration_ms",
        "Success": "dependency_success",
    })
    joined = request_rows.merge(dependency_rows, on="OperationId", how="left")
    incident = joined.loc[joined["request_success"].eq(False)].sort_values(
        ["request_timestamp", "dependency_duration_ms"], na_position="first"
    ).iloc[-1]

    timestamp = incident["request_timestamp"]
    status_code = _number_or_none(incident["ResultCode"])
    if pd.isna(timestamp) or status_code is None or not status_code.is_integer():
        raise ValueError("The latest failed request has an invalid timestamp or status code.")
    dependency_success = incident["dependency_success"]
    dependency_name = incident["dependency_name"]
    operation_id = incident["OperationId"]
    return {
        "operation_id": None if pd.isna(operation_id) else str(operation_id),
        "timestamp": timestamp.isoformat(),
        "request": {
            "status_code": int(status_code),
            "duration_ms": _number_or_none(incident["request_duration_ms"]),
        },
        "dependency": {
            "name": None if pd.isna(dependency_name) else str(dependency_name),
            "duration_ms": _number_or_none(incident["dependency_duration_ms"]),
            "success": None if pd.isna(dependency_success) else bool(dependency_success),
        },
        "window_summary": {
            "total_requests": total_requests,
            "failed_requests": failed_requests,
            "p95_latency_ms": p95_latency_ms,
        },
    }


def main() -> int:
    """Query the configured workspace and atomically write incident evidence."""
    credential = None
    client = None
    try:
        workspace_id = os.environ.get("LOG_ANALYTICS_WORKSPACE_ID", "").strip()
        if not workspace_id:
            raise ValueError("Set LOG_ANALYTICS_WORKSPACE_ID to the workspace GUID.")
        try:
            workspace_id = str(uuid.UUID(workspace_id))
        except ValueError as exc:
            raise ValueError("LOG_ANALYTICS_WORKSPACE_ID must be a valid workspace GUID.") from exc
        credential = DefaultAzureCredential()
        client = LogsQueryClient(credential)
        requests = load_query_frame(client, workspace_id, REQUEST_QUERY)
        dependencies = load_query_frame(client, workspace_id, DEPENDENCY_QUERY)
        report = build_incident_report(requests, dependencies)
        if report is None:
            print(
                "No failed /simulate requests found in the last 48 hours; no report was written.",
                file=sys.stderr,
            )
            return 1

        serialized = json.dumps(report, indent=2, allow_nan=False)
        output_path = Path("data/incidents/latest.json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_name("latest.json.tmp")
        temporary_path.write_text(serialized + "\n", encoding="utf-8")
        temporary_path.replace(output_path)
        print(output_path)
        return 0
    except AzureError as exc:
        print(
            f"Azure Monitor query failed: {exc}. Check Azure sign-in and workspace query permission.",
            file=sys.stderr,
        )
        return 1
    except (ValueError, OSError) as exc:
        print(f"Incident extraction failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            if credential is not None:
                credential.close()


if __name__ == "__main__":
    raise SystemExit(main())
