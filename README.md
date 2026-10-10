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
usage charges. Operation IDs, ground-truth labels, and unknown top-level
metadata are excluded from model input. Evidence must use exact `path=value`
tokens with JSON scalar values, such as `dependency.success=false` and
`dependency.name="search_service"`. Unsupported tokens and malformed responses
are rejected. A 404 with no named dependency must yield `insufficient_evidence`
and a null root cause. Confidence is qualitative. Recommended checks are
investigation steps; this workflow does not execute remediation.
