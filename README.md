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
