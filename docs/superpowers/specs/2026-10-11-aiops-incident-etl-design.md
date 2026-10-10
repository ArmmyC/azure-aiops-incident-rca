# AIOps Incident ETL Design

## Goal

Add a local command that reads recent FastAPI incident telemetry from the existing Azure Log Analytics workspace, correlates requests with dependency spans using Pandas, and writes the latest failed `/simulate` request as an evidence-only JSON report.

The command is read-only against Azure. It does not provision Azure resources or call Microsoft Foundry.

## Chosen approach

Run two workspace queries through `azure-monitor-query`: one for `/simulate` request records and one for `search_service` dependency records. Use Pandas to left-join the result sets on `OperationId`. This makes the requested Pandas correlation explicit and retains failed requests that have no dependency span.

The alternative considered was a single KQL left join followed by Pandas shaping. That reduces transfer of raw rows but performs the correlation in KQL, which does not meet the goal of demonstrating the Pandas join.

## Components and data flow

1. `etl/extract_incidents.py` reads `LOG_ANALYTICS_WORKSPACE_ID` and validates it before querying.
2. `DefaultAzureCredential` authenticates using the developer's configured Azure identity, including Azure CLI sign-in.
3. `LogsQueryClient` reads `AppRequests` and `AppDependencies` over the last 48 hours. Requests include all `/simulate` records for the window; dependency records are filtered to `search_service`.
4. Pandas normalizes the query tables and left-joins dependencies to requests by `OperationId`.
5. The window summary is calculated from the request rows before joining, so multiple dependency spans do not inflate counts. It reports request count, failed request count, and p95 request latency.
6. The newest failed request is selected. If it has multiple matching `search_service` spans, the longest span is reported. If it has no dependency span, the request remains and dependency fields are `null`.
7. The report is written to `data/incidents/latest.json` only after both query results are complete and transformation succeeds.

## Output and privacy

The JSON contains the operation ID, timestamp, request status and duration, dependency name, duration and success value, plus the window summary. It contains evidence only and must not include injected-fault labels or expected root-cause labels.

`data/incidents/` is added to `.gitignore` because the report contains operational identifiers and metadata. If no failed request exists in the query window, the command reports that condition and does not replace the existing report.

## Dependencies and local setup

Add `azure-identity`, `azure-monitor-query`, and `pandas` to a local `etl` dependency group in `pyproject.toml` and update `uv.lock`. Keep the Docker-focused `requirements.txt` unchanged so the local ETL dependencies are not added to the deployed API image. The workspace ID remains an environment variable and is not stored in source control.

## Errors and validation

- Missing or malformed workspace ID: fail before making a query and explain the expected environment variable.
- Authentication, authorization, or query error: fail with an actionable message and do not write a partial report.
- Partial Azure query response: reject it rather than producing a report from incomplete telemetry.
- No matching failed request: report the empty result and leave any existing output untouched.
- Successful run: execute `python -m etl.extract_incidents`, then inspect the generated JSON for request/dependency evidence and confirm that no fault or expected-cause label is present.

The identity must have permission to query the selected Log Analytics workspace. Dependency management and ETL execution remain local; Azure resources and Foundry configuration are outside this change.
