import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = """
import dotenv
dotenv.load_dotenv = lambda: None
from azure.monitor import opentelemetry as azure_monitor
azure_monitor.configure_azure_monitor = lambda **options: None
"""


def run_app_script(script, *, app_env, api_key):
    environment = os.environ.copy()
    environment.update(
        {
            "FOUNDRY_ENDPOINT": "https://example.invalid/openai/v1/",
            "FOUNDRY_API_KEY": "test-foundry-key",
            "APPLICATIONINSIGHTS_CONNECTION_STRING": (
                "InstrumentationKey=test;"
                "IngestionEndpoint=https://example.invalid/"
            ),
            "AIOPS_API_KEY": api_key,
            "ENABLE_SIMULATION": "false",
        }
    )
    if app_env is None:
        environment.pop("APP_ENV", None)
    else:
        environment["APP_ENV"] = app_env

    return subprocess.run(
        [sys.executable, "-c", BOOTSTRAP + script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("api_key", ["", "short"])
def test_production_default_rejects_missing_or_short_api_key(api_key):
    result = run_app_script(
        """
try:
    import app
except RuntimeError as error:
    assert str(error) == "AIOPS_API_KEY must be configured for production"
else:
    raise AssertionError("production startup accepted an invalid API key")
""",
        app_env=None,
        api_key=api_key,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_production_routes_require_key_and_keep_health_public():
    result = run_app_script(
        """
from fastapi.testclient import TestClient
import app

class Usage:
    input_tokens = 4
    output_tokens = 2

class FakeResponse:
    output_text = "authorized response"
    usage = Usage()

calls = []
def fake_create(**arguments):
    calls.append(arguments)
    return FakeResponse()

app.client.responses.create = fake_create
api_key = "0123456789abcdef0123456789abcdef"
with TestClient(app.app) as client:
    assert client.get("/health").status_code == 200
    for path in ("/docs", "/redoc", "/openapi.json", "/metrics"):
        assert client.get(path).status_code == 404, path
    assert client.post("/simulate", json={"scenario": "healthy"}).status_code == 404

    payload = {"prompt": "hello"}
    assert client.post("/ask", json=payload).status_code == 401
    assert client.post(
        "/ask", json=payload, headers={"X-API-Key": "wrong-key"}
    ).status_code == 401
    assert calls == []

    headers = {"X-API-Key": api_key}
    assert client.post("/ask", json={"prompt": ""}, headers=headers).status_code == 422
    assert client.post(
        "/ask", json={"prompt": "x" * 2001}, headers=headers
    ).status_code == 422
    assert calls == []

    response = client.post("/ask", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "answer": "authorized response",
        "input_tokens": 4,
        "output_tokens": 2,
    }
    assert calls == [
        {
            "model": "gpt-5-mini",
            "input": "hello",
            "max_output_tokens": 1024,
        }
    ]
""",
        app_env="production",
        api_key="0123456789abcdef0123456789abcdef",
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_development_without_simulation_flag_hides_simulator_but_keeps_local_tools():
    result = run_app_script(
        """
from fastapi.testclient import TestClient
import app

class FakeResponse:
    output_text = "local response"
    usage = None

app.client.responses.create = lambda **arguments: FakeResponse()

with TestClient(app.app) as client:
    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/metrics").status_code == 200
    assert client.post("/simulate", json={"scenario": "healthy"}).status_code == 404
    response = client.post("/ask", json={"prompt": "hello"})
    assert response.status_code == 200
    assert response.json()["answer"] == "local response"
""",
        app_env="development",
        api_key="",
    )

    assert result.returncode == 0, result.stderr or result.stdout
