# Foundry Root Cause Analysis Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local, evidence-only RCA workflow using a versioned Microsoft Foundry prompt agent and strict response validation.

**Architecture:** Extend the existing Azure Monitor ETL command with an optional operation-ID selector while preserving its current default. Add an `rca` package that creates a managed prompt-agent version explicitly and invokes it through the stable project OpenAI client using an agent reference; validate response JSON and evidence rules locally before printing.

**Tech Stack:** Python 3.12+, uv, Azure AI Projects, Azure Identity, OpenAI Responses API, Pydantic, Pandas, pytest.

**Spec:** `docs/superpowers/specs/2026-10-11-foundry-rca-agent-design.md`

## Global Constraints

- Keep the default ETL behavior as the newest failed `/simulate` request; `--operation-id` selects an exact matching request and leaves the existing artifact untouched when no ID matches.
- Preserve the evidence-only incident report and Pandas left join; do not add injected-fault or expected-root-cause labels.
- The RCA report must contain exactly `status`, `root_cause`, `confidence`, `evidence`, and `recommended_checks`.
- Reject malformed JSON and extra response fields; `insufficient_evidence` requires a null root cause and `diagnosed` requires a non-empty root cause.
- Require every evidence item to use an exact `path=value` token derived from a scalar in the whitelisted input; reject unsupported evidence claims locally.
- An HTTP 404 without dependency evidence must not yield a diagnosed report. Treat both an absent dependency object and the ETL's `{ "name": null, ... }` representation as missing evidence.
- Whitelist telemetry fields sent to the agent; never send operation IDs or unrecognized labels.
- Use `AIProjectClient`, `PromptAgentDefinition`, and `DefaultAzureCredential`; invoke with `get_openai_client()` and a Responses API `agent_reference`.
- Add agent dependencies to a local-only `rca` dependency group. Leave `requirements.txt` unchanged.
- Keep `FOUNDRY_PROJECT_ENDPOINT` and all credentials out of tracked files; use the ignored local `.env` only.
- Run the full test suite before exactly one live analysis call. Do not call Foundry from unit tests or create agent versions from the analyzer.

---

### Task 1: Add exact operation-ID selection to the ETL

**Files:**
- Modify: `etl/extract_incidents.py`
- Create: `tests/test_incident_extraction.py`

**Interfaces:**
- `build_incident_report(requests: pd.DataFrame, dependencies: pd.DataFrame, operation_id: str | None = None) -> dict[str, object] | None`
- `main(argv: Sequence[str] | None = None) -> int`; parse `--operation-id` with `argparse`.
- Without an operation ID, select the newest failed row as today. With an ID, select the newest matching `/simulate` request for that exact operation ID, whether successful or failed, and left-join its matching dependencies.

- [ ] **Step 1: Add failing selector tests.** Create synthetic AppRequests and AppDependencies DataFrames. Include a newer failed 404 without a dependency, an older failed 504 with a failed `search_service` span, and a successful slow request with another ID. Assert default selection remains the 404; selecting the 504 ID returns the 504 and its dependency; selecting the successful ID returns that request; and an unknown ID returns `None`.

```python
def test_default_selects_newest_failure_and_explicit_id_selects_older_request():
    report = build_incident_report(requests, dependencies)
    assert report["request"]["status_code"] == 404
    assert report["dependency"]["name"] is None

    selected = build_incident_report(requests, dependencies, operation_id="op-504")
    assert selected["request"]["status_code"] == 504
    assert selected["dependency"] == {
        "name": "search_service",
        "duration_ms": 1004.0,
        "success": False,
    }


def test_unknown_operation_id_returns_no_report():
    assert build_incident_report(requests, dependencies, operation_id="missing") is None
```

- [ ] **Step 2: Run the targeted tests and confirm they fail** because the selector parameter and CLI option do not exist.

Run: `uv run --locked --group dev --group etl pytest -q tests/test_incident_extraction.py`

- [ ] **Step 3: Implement the selector.** Normalize `OperationId` to Pandas string values; filter to the requested ID before the left join; retain only that ID's request rows; and choose the newest matching timestamp. Keep the no-argument failed-row path unchanged. Add `--operation-id` to `main(argv=None)`. If selection returns `None`, return nonzero and do not replace `data/incidents/latest.json`.

- [ ] **Step 4: Add a CLI no-overwrite test.** Use a temporary working directory, a valid dummy workspace GUID, and a stubbed `load_query_frame` that returns rows without the requested ID. Pre-create `data/incidents/latest.json`, call `main(["--operation-id", "missing"])`, and assert a nonzero return plus unchanged file contents.

- [ ] **Step 5: Re-run targeted tests and commit.**

Run: `uv run --locked --group dev --group etl pytest -q tests/test_incident_extraction.py`

Commit: `feat: select incidents by operation id`

---

### Task 2: Build the managed RCA agent and strict local analyzer

**Files:**
- Create: `rca/__init__.py`
- Create: `rca/instructions.py`
- Create: `rca/create_agent.py`
- Create: `rca/analyze.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `README.md`
- Create: `tests/test_rca_agent.py`

**Interfaces:**
- `RCAReport(BaseModel)` has exactly the five spec fields and rejects extra fields.
- `build_agent_input(incident: dict[str, object]) -> dict[str, object]` returns only `timestamp`, `request`, `dependency`, and `window_summary`; it excludes `operation_id` and all unrecognized metadata.
- `validate_report(incident: dict[str, object], report: RCAReport) -> RCAReport` enforces response invariants and the 404-without-dependency abstention rule.
- `analyze_incident(incident: dict[str, object], openai_client: object, agent_name: str) -> RCAReport` serializes only the whitelisted input, calls `responses.create` with `extra_body={"agent_reference": {"name": agent_name, "type": "agent_reference"}}`, parses `output_text` with Pydantic JSON validation, and validates it against the incident.
- `create_agent.main() -> int` loads local configuration, creates exactly one version when explicitly run, and closes the project and credential.
- `analyze.main() -> int` loads `data/incidents/latest.json`, creates the project identity/client, analyzes once, prints only the validated JSON, and closes resources.

- [ ] **Step 1: Add failing tests before implementation.** Cover: a 404 whose dependency is absent or has a null name rejects `diagnosed`; `insufficient_evidence` requires `root_cause is None`; `diagnosed` requires a non-empty root cause; malformed JSON and extra keys are rejected; unsupported evidence tokens are rejected; the agent input omits `injected_fault`, `expected_root_cause`, `operation_id`, and unknown keys; and a fake OpenAI client receives the stable `agent_reference` request body.

```python
def test_404_without_dependency_name_rejects_diagnosis():
    incident = {
        "request": {"status_code": 404, "duration_ms": 1},
        "dependency": {"name": None, "duration_ms": None, "success": None},
    }
    report = RCAReport(
        status="diagnosed",
        root_cause="route failure",
        confidence="high",
        evidence=["HTTP 404"],
        recommended_checks=["Check route configuration"],
    )
    with pytest.raises(ValueError):
        validate_report(incident, report)


def test_agent_input_excludes_labels_and_operation_id():
    payload = build_agent_input({
        "operation_id": "op-1",
        "request": {"status_code": 504},
        "dependency": {"name": "search_service"},
        "injected_fault": "dependency_timeout",
        "expected_root_cause": "search timeout",
    })
    assert set(payload) == {"request", "dependency"}
    assert "injected_fault" not in json.dumps(payload)
    assert "expected_root_cause" not in json.dumps(payload)
    assert "op-1" not in json.dumps(payload)
```

- [ ] **Step 2: Run the targeted tests and confirm they fail** because the `rca` package and validation functions do not exist.

Run: `uv run --locked --group dev --group etl pytest -q tests/test_rca_agent.py`

- [ ] **Step 3: Add the local dependency group and lockfile.** Add `azure-ai-projects>=2.3.0` and `azure-identity` to `[dependency-groups].rca` using uv. Keep `requirements.txt` unchanged.

Run: `uv add --group rca "azure-ai-projects>=2.3.0" azure-identity`

- [ ] **Step 4: Implement the prompt and response model.** In `rca/instructions.py`, define `RCA_INSTRUCTIONS` with the seven evidence, abstention, untrusted-data, and investigation-only rules from the approved spec. In `rca/analyze.py`, define `RCAReport` with `ConfigDict(extra="forbid")`; add a Pydantic after-validator that requires a null root cause for `insufficient_evidence` and a non-empty root cause for `diagnosed`; use `model_validate_json` so invalid JSON fails closed.

- [ ] **Step 5: Implement the evidence guard and stable agent call.** `build_agent_input` must explicitly copy only the four approved evidence fields. Build an allowlist of exact `path=value` tokens from scalar leaves of that payload; `validate_report` must reject evidence items outside the allowlist, recognize missing dependency evidence when `dependency` is absent, null, or has no non-empty `name`, and reject a diagnosed 404 in that case. The prompt must require evidence items to use these exact tokens. `analyze_incident` must send `json.dumps(build_agent_input(incident))` as input and attach the exact `agent_reference` in `extra_body`.

- [ ] **Step 6: Implement the one-time agent creator and analyzer CLI.** Both commands load `.env` without printing values and require `FOUNDRY_PROJECT_ENDPOINT`. The creator uses `FOUNDRY_AGENT_NAME` or `aiops-rca-agent`, `FOUNDRY_MODEL` or `gpt-5-mini`, `AIProjectClient`, `PromptAgentDefinition`, and `DefaultAzureCredential`; it does not create agents on analyzer runs. The analyzer reads the ignored latest incident file, creates the stable project OpenAI client, calls the existing named agent once, validates the JSON, prints the validated report, and closes clients/credentials in `finally` blocks.

- [ ] **Step 7: Document local setup and run commands.** Add a README section distinguishing `FOUNDRY_PROJECT_ENDPOINT` from `FOUNDRY_ENDPOINT`, showing the project endpoint format, local environment variable names, and commands for operation-ID extraction, one-time `rca.create_agent`, and `rca.analyze`. State that agent creation is versioned and must not be run per incident.

- [ ] **Step 8: Run RCA tests, compile checks, and lock checks.**

Run:

```powershell
uv run --locked --group dev --group etl --group rca pytest -q tests/test_rca_agent.py
uv run --locked --group dev --group etl --group rca python -m compileall -q rca etl
uv lock --check
git diff --check
```

- [ ] **Step 9: Commit the implementation.**

Commit: `feat: add evidence-only Foundry RCA agent`

---

### Task 3: Full-suite and single live Foundry verification

**Files:**
- No tracked files; update the SDD progress ledger with command results.
- Local ignored configuration may be updated only in `.env`.

**Interfaces:**
- Required live configuration: `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_AGENT_NAME`, `FOUNDRY_MODEL`, and Azure CLI sign-in usable by `DefaultAzureCredential`.
- The current generated 404 report is the one live analysis input. Do not make additional model calls if the response fails validation.

- [ ] **Step 1: Run the full suite with test-only dummy environment values.** The worktree does not have the ignored main-checkout `.env`; provide dummy `APPLICATIONINSIGHTS_CONNECTION_STRING`, `FOUNDRY_ENDPOINT`, and `FOUNDRY_API_KEY` only to this test process. No live service is called by the test suite.

```powershell
$env:APPLICATIONINSIGHTS_CONNECTION_STRING = 'InstrumentationKey=test;IngestionEndpoint=https://example.invalid/'
$env:FOUNDRY_ENDPOINT = 'https://example.invalid/openai/v1/'
$env:FOUNDRY_API_KEY = 'test-only-key'
uv run --locked --group dev --group etl --group rca pytest -q
$testExitCode = $LASTEXITCODE
Remove-Item Env:APPLICATIONINSIGHTS_CONNECTION_STRING,Env:FOUNDRY_ENDPOINT,Env:FOUNDRY_API_KEY -ErrorAction SilentlyContinue
if ($testExitCode -ne 0) { exit $testExitCode }
```

- [ ] **Step 2: Resolve live configuration without displaying secrets.** Check only whether the main ignored `.env` contains the project endpoint. If it is absent, use signed-in Azure resource metadata to identify the existing Foundry project's endpoint and write only `FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_AGENT_NAME=aiops-rca-agent` to the ignored local configuration. If no project endpoint can be resolved, stop before any agent creation and report the exact missing setting.

- [ ] **Step 3: Make one explicit agent-version creation call.** Make sure no agent version was created during tests, then run `uv run --locked --group dev --group etl --group rca python -m rca.create_agent` exactly once. Confirm only the returned agent name and version; never print credentials or the endpoint.

- [ ] **Step 4: Make one real analysis call.** Copy the existing ignored `data/incidents/latest.json` into this worktree if absent, without printing its operation ID. Run `uv run --locked --group dev --group etl --group rca python -m rca.analyze` once. Confirm the 404/no-dependency input yields `insufficient_evidence` and a null root cause. If the model response is invalid or violates the guard, report the error and do not repeat the paid call.

- [ ] **Step 5: Record results and commit no local secrets.** Confirm `git status --short` is clean; confirm `.env` and `data/incidents/latest.json` remain ignored; record test output, agent version, one-call result, and any configuration limitation in the progress ledger.

## Execution Notes

- The user selected subagent-driven implementation in the preceding Step 8 work and that preference remains in effect. Use a fresh implementer for each code task, task-scoped review after each commit, and one whole-branch review after Task 2.
- Keep the live Foundry verification in the controller phase after code review and all tests. It is an authorized one-time external call that may incur usage charges.
- The Azure CLI directory may need to be prepended to the process-only `PATH` for `DefaultAzureCredential` on Windows. Do not modify the machine-wide PATH.
