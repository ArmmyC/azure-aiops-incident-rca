# Foundry Learning AIOps API

## Local development

The local `.env` file should set `APP_ENV=development` and
`ENABLE_SIMULATION=true`. Keep Foundry and Application Insights credentials in
that ignored file; `.dockerignore` excludes it from the image.

Run the service with:

```powershell
uv run uvicorn app:app --reload
```

Development mode exposes `/docs`, `/metrics`, and `/simulate`. `/ask` is open
locally and accepts prompts from 1 to 2,000 characters; model output is capped
at 1,024 tokens.

## Production configuration

Production is the default whenever `APP_ENV` is unset or has a value other than
`development`. It requires `AIOPS_API_KEY` to contain at least 32 characters.
Send it to `/ask` in the `X-API-Key` header. Production keeps `/health`
available and does not register `/docs`, `/redoc`, `/openapi.json`, `/metrics`,
or `/simulate`.

For a future Azure Container Apps deployment, configure `APP_ENV=production`,
`ENABLE_SIMULATION=false`, and provide `FOUNDRY_ENDPOINT`, `FOUNDRY_MODEL`,
`FOUNDRY_API_KEY`, `AIOPS_API_KEY`, and
`APPLICATIONINSIGHTS_CONNECTION_STRING` through Container Apps configuration
and secrets. Do not upload `.env` or bake credentials into the Docker image.
The planned initial footprint is Consumption, zero minimum replicas, one maximum
replica, 0.25 vCPU, 0.5 GiB memory, port 8000, and HTTPS ingress. Set a budget
and alert before creating Azure resources.

## Local Foundry root cause analysis

The local RCA workflow uses a managed prompt agent and Azure identity through
`DefaultAzureCredential` (for example, an Azure CLI sign-in). Install the local
dependencies with `uv sync --group etl --group rca`. Keep settings in the ignored
local `.env` file:

```dotenv
FOUNDRY_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
FOUNDRY_AGENT_NAME=aiops-rca-agent
FOUNDRY_MODEL=gpt-5-mini
LOG_ANALYTICS_WORKSPACE_ID=<workspace-guid>
FOUNDRY_AGENT_VERSION=2
```

`FOUNDRY_PROJECT_ENDPOINT` identifies the Foundry project for agent management
and analysis. It is distinct from `FOUNDRY_ENDPOINT`, the model endpoint used
by the API service. Agent name and model default to the values shown above;
the project endpoint is required. Keep real endpoint values and credentials
out of tracked files. RCA dependencies are local only; Docker
`requirements.txt` is unchanged.

Extract the newest failed `/simulate` request, or select a historical request
by its exact operation ID (including a successful but slow request):

```powershell
uv run --env-file .env --group etl python -m etl.extract_incidents
uv run --env-file .env --group etl python -m etl.extract_incidents --operation-id <operation-id>
```

The extractor writes `data/incidents/latest.json`. A missing operation ID
leaves the existing artifact intact. The `--env-file .env` option supplies
`LOG_ANALYTICS_WORKSPACE_ID` to extraction from local configuration.

Create the agent once, then analyze the current local artifact:

```powershell
uv run --group rca python -m rca.create_agent
uv run --group rca python -m rca.analyze
```

Agent creation is versioned: every explicit creator invocation creates a new
version. **Do not run creation per incident.** The analyzer calls the existing
named agent once and prints only validated JSON; inference may incur Foundry
usage charges. Operation IDs, ground-truth labels, and unknown metadata at
every level are excluded from model input. Evidence must use exact `path=value`
tokens with JSON scalar values, such as `dependency.success=false` and
`dependency.name="search_service"`. Unsupported tokens and malformed responses
are rejected. A 404 with no named dependency must yield `insufficient_evidence`
and a null root cause. Confidence is qualitative. Recommended checks are
investigation steps; this workflow does not execute remediation.

## Local RCA evaluation

The evaluation dataset has 24 labeled cases: six dependency timeouts, six
application exceptions with exception-type evidence, six dependency-latency
regressions with baselines, and six insufficient-evidence cases. It includes
sanitized telemetry from the real 404 and 504 incidents; operation IDs are
kept out of the tracked dataset. Only the incident telemetry whitelist is sent
to the RCA agent. Expected labels are used by the offline scorer and the
separately invoked judge, never by the RCA prediction request.

Version 2 adds `root_cause_category` while version 1 keeps its original
response contract. Check the managed agent's version list before creating
version 2. `rca.create_agent` creates version 2 only when version 1 is the
sole existing version; it stops if the remote agent already has another version.

```powershell
uv run --group rca python -m rca.create_agent
```

Start with the real 504 and 404 cases. The generator pins the requested agent
version and caches each response by case ID and version:

```powershell
uv run --group rca python -m evaluation.generate_predictions `
  --agent-version 2 `
  --case-id timeout-001 `
  --case-id abstain-001
```

Then run a five-case pilot that covers all four categories. The two real cases
are reused from cache:

```powershell
uv run --group rca python -m evaluation.generate_predictions `
  --agent-version 2 `
  --case-id timeout-001 `
  --case-id abstain-001 `
  --case-id exception-001 `
  --case-id latency-001 `
  --case-id abstain-002
uv run --group rca python -m evaluation.score_predictions `
  --agent-version 2 `
  --case-id timeout-001 `
  --case-id abstain-001 `
  --case-id exception-001 `
  --case-id latency-001 `
  --case-id abstain-002
```

After inspecting the pilot, `--all` generates any remaining predictions once.
Scoring is local and can be repeated without model calls:

```powershell
uv run --group rca python -m evaluation.generate_predictions --agent-version 2 --all
uv run --group rca python -m evaluation.score_predictions --agent-version 2
```

To request an LLM-as-a-Judge assessment, invoke its one-case command explicitly.
That is a separate Foundry model call and may incur usage charges:

```powershell
uv run --group rca python -m evaluation.judge --case-id timeout-001 --agent-version 2
```

Judge scores are not ground truth. Manually review cases where the judge and
deterministic category scorer disagree.

Predictions, judge results, reports, and full local Azure fixtures are stored
under ignored `data/evaluations/`. The scorer reports category accuracy,
valid-diagnosis coverage, abstention recall, false-diagnosis rate, schema
validity, evidence grounding, and validator rejections, with the numerator and
denominator for each metric. The 24-case dataset is an engineering benchmark,
not a statistical reliability guarantee.

## GitHub Actions CI

The CI workflow runs the test suite, compiles the Python modules, scores an
offline RCA fixture, and builds the Docker image. It uses the locked `uv`
dependencies and test-only placeholder environment values; it needs no Azure
credentials and makes no Foundry calls. The eight-case fixture in
`evaluation/fixtures/` contains synthetic incidents and parsed v2 reports from
the prior evaluation; raw model text and operational IDs are excluded. It
exercises the scorer and regression gate, not live model behavior or
generalization. The separate 24-case dataset remains available for local
evaluation.

Run the same offline gate locally:

```powershell
uv run --group dev --group etl --group rca python -m evaluation.ci_gate
```

Azure deployment and live Foundry evaluation remain manual operations.
