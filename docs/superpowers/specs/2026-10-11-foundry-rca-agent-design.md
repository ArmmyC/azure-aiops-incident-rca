# Foundry Root Cause Analysis Agent Design

## Goal

Add a local, evidence-only root-cause analysis workflow that sends one selected ETL incident to a versioned Microsoft Foundry prompt agent, validates the response, and abstains when the telemetry does not support a diagnosis.

## Approved approach

Use Microsoft Foundry Agent Service with `AIProjectClient`, `PromptAgentDefinition`, and `DefaultAzureCredential`. Create agent versions only through the explicit `python -m rca.create_agent` command. For inference, use `AIProjectClient.get_openai_client()` and the Responses API `agent_reference` payload. This follows the stable documented agent invocation path and avoids the `get_openai_client(agent_name=...)` preview opt-in.

Keep extraction and analysis local. Extend the existing ETL command with an optional `--operation-id` selector so a known historical request can be exported without changing the existing default, which remains the newest failed request. The selector can target any matching `/simulate` request, including a successful but slow request. A selector with no match fails without replacing the current artifact.

## Components and data flow

1. `etl.extract_incidents` queries the same bounded 48-hour request and `search_service` dependency tables. With no selector it continues to select the newest failed request. With `--operation-id`, it selects the exact request identified by that operation ID and left-joins matching dependency evidence.
2. The ETL writes the selected evidence to `data/incidents/latest.json`; the directory remains ignored by Git.
3. `rca.instructions` defines the agent's evidence-only behavior, JSON contract, prompt-injection boundary, and abstention rules.
4. `rca.create_agent` reads `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_AGENT_NAME` (default `aiops-rca-agent`), and `FOUNDRY_MODEL` (default `gpt-5-mini`), then creates one named prompt-agent version using the project identity. It prints the created name and version. It is a one-time command, not part of incident analysis.
5. `rca.analyze` reads `data/incidents/latest.json`, constructs a whitelist containing only timestamp, request, dependency, and window-summary evidence, calls the named agent through the project client, parses only JSON into a strict Pydantic report, applies evidence-dependent invariants, and prints the validated JSON. It does not execute recommendations or modify Azure resources.

## Response contract and evidence rules

The report has exactly these fields:

- `status`: `diagnosed` or `insufficient_evidence`.
- `root_cause`: a non-empty string for `diagnosed`, and `null` for `insufficient_evidence`.
- `confidence`: `low`, `medium`, or `high`; qualitative, not a calibrated probability.
- `evidence`: a list of observations present in the input.
- `recommended_checks`: a list of investigation steps.

Extra fields and malformed JSON are rejected. A 404 without correlated dependency evidence cannot produce a definitive report. “No dependency evidence” includes an absent/null `dependency` object and the ETL's current form where `dependency` exists but `name` is null. A failed dependency span may support a dependency-failure hypothesis but cannot establish the underlying infrastructure cause. A 500 without exception details must remain a symptom, not an invented exception diagnosis. A 200 selected by operation ID may be reported as a performance anomaly only when the provided durations support that interpretation.

The model input is built from a whitelist, so keys such as `injected_fault`, `expected_root_cause`, and other unrecognized incident metadata never reach the agent. The agent receives telemetry as untrusted data and may recommend checks only; it has no tools for automatic remediation.

## Configuration and dependencies

- Add `azure-ai-projects>=2.3.0` and `azure-identity` to the local-only RCA dependency group; reuse the existing `FOUNDRY_MODEL` setting and `DefaultAzureCredential`.
- `FOUNDRY_PROJECT_ENDPOINT` is distinct from `FOUNDRY_ENDPOINT` and must have the form `https://<resource>.services.ai.azure.com/api/projects/<project>`.
- `FOUNDRY_AGENT_NAME` defaults to `aiops-rca-agent`.
- Do not add project endpoint values or credentials to tracked files. Keep the Docker `requirements.txt` unchanged.

## Validation and live execution

Unit tests mock Foundry calls and cover:

1. A 404 with no dependency evidence cannot yield a definitive diagnosis.
2. `insufficient_evidence` requires a null root cause.
3. `diagnosed` requires a non-empty root cause.
4. Malformed JSON and extra response fields are rejected.
5. Ground-truth labels and unrecognized incident fields are excluded from agent input.
6. The operation-ID selector retrieves an older exact request while the no-argument default still selects the newest failure; a missing ID preserves the existing output.

Run the full existing pytest suite after these tests. Once tests pass and `FOUNDRY_PROJECT_ENDPOINT` is configured, create one agent version and make one live analysis call against the current report. The live inference may incur Foundry usage charges; do not run it repeatedly. No other cloud resources or scheduled processing are part of this change.

## Microsoft documentation

- [Create a prompt agent](https://learn.microsoft.com/azure/foundry/agents/quickstarts/prompt-agent)
- [Foundry runtime components and agent references](https://learn.microsoft.com/azure/foundry/agents/concepts/runtime-components)
- [AIProjectClient Python reference](https://learn.microsoft.com/python/api/azure-ai-projects/azure.ai.projects.aiprojectclient)
