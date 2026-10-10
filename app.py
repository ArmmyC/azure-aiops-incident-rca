import os
import time
import logging
import hmac
from typing import Literal

# Configure Azure Monitor before importing FastAPI so its instrumentor can patch it.
import telemetry  # noqa: F401

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from openai import OpenAI
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from prometheus_client import (
    Counter,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

# telemetry imports and loads the local .env before application configuration.
APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
IS_PRODUCTION = APP_ENV != "development"
API_ACCESS_KEY = os.getenv("AIOPS_API_KEY", "")

if IS_PRODUCTION and len(API_ACCESS_KEY) < 32:
    raise RuntimeError("AIOPS_API_KEY must be configured for production")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(key: str | None = Depends(api_key_header)):
    if not IS_PRODUCTION:
        return
    if not key or not hmac.compare_digest(key, API_ACCESS_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")


app = FastAPI(
    title="AIOps AI Service",
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)
logger = logging.getLogger("aiops")
tracer = trace.get_tracer(__name__)

client = OpenAI(
    base_url=os.environ["FOUNDRY_ENDPOINT"],
    api_key=os.environ["FOUNDRY_API_KEY"],
)

MODEL = os.getenv("FOUNDRY_MODEL", "gpt-5-mini")


# --- Prometheus metrics ---

REQUEST_COUNT = Counter(
    "ai_requests_total",
    "Total AI requests",
    ["status"],
)

REQUEST_LATENCY = Histogram(
    "ai_request_duration_seconds",
    "AI request latency in seconds",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 60),
)

INPUT_TOKENS = Counter(
    "ai_input_tokens_total",
    "Total input tokens",
)

OUTPUT_TOKENS = Counter(
    "ai_output_tokens_total",
    "Total output tokens",
)

INCIDENT_REQUESTS = Counter(
    "incident_requests_total",
    "Requests to the incident simulator",
    ["status_code"],
)
for status_code in ("200", "500", "504"):
    INCIDENT_REQUESTS.labels(status_code=status_code)

INCIDENT_LATENCY = Histogram(
    "incident_request_duration_seconds",
    "Incident simulator request duration",
    buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 10),
)


class AskRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)


class SimulateRequest(BaseModel):
    scenario: Literal[
        "healthy",
        "slow_dependency",
        "dependency_timeout",
        "application_error",
    ]


@app.get("/health")
def health():
    return {"status": "ok"}


if not IS_PRODUCTION:
    @app.get("/metrics")
    def metrics():
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )


@app.post("/ask", dependencies=[Depends(verify_api_key)])
def ask(request: AskRequest):
    start = time.perf_counter()
    status = "success"

    try:
        response = client.responses.create(
            model=MODEL,
            input=request.prompt,
            max_output_tokens=1024,
        )

        usage = response.usage
        input_tokens = usage.input_tokens if usage else 0
        output_tokens = usage.output_tokens if usage else 0

        INPUT_TOKENS.inc(input_tokens)
        OUTPUT_TOKENS.inc(output_tokens)

        return {
            "answer": response.output_text,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    except Exception as exc:
        status = "error"
        logger.exception("Foundry request failed")
        raise HTTPException(
            status_code=502,
            detail="Foundry model request failed",
        ) from exc

    finally:
        REQUEST_COUNT.labels(status=status).inc()
        REQUEST_LATENCY.observe(
            time.perf_counter() - start
        )


def simulate(request: SimulateRequest):
    start = time.perf_counter()
    status_code = 200

    try:
        if request.scenario == "application_error":
            with tracer.start_as_current_span("application.process") as span:
                status_code = 500
                span.set_status(
                    Status(StatusCode.ERROR, "Processing failed")
                )
                raise HTTPException(
                    status_code=500,
                    detail="Simulated application error",
                )

        with tracer.start_as_current_span("search_service") as span:
            span.set_attribute("dependency.name", "search_service")

            if request.scenario == "healthy":
                time.sleep(0.1)
            elif request.scenario == "slow_dependency":
                time.sleep(2)
            else:
                time.sleep(1)
                status_code = 504
                span.set_status(
                    Status(StatusCode.ERROR, "Search timeout")
                )
                span.add_event("search_service timed out")
                raise HTTPException(
                    status_code=504,
                    detail="Simulated search dependency timeout",
                )

        return {
            "status": "success",
            "message": "Simulation completed",
        }
    except HTTPException as exc:
        status_code = exc.status_code
        raise
    except Exception:
        status_code = 500
        raise
    finally:
        INCIDENT_REQUESTS.labels(status_code=str(status_code)).inc()
        INCIDENT_LATENCY.observe(time.perf_counter() - start)


if (
    not IS_PRODUCTION
    and os.getenv("ENABLE_SIMULATION", "false").lower() == "true"
):
    app.add_api_route("/simulate", simulate, methods=["POST"])
