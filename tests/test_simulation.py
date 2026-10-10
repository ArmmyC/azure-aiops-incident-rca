import os

import pytest
from fastapi.testclient import TestClient
from azure.monitor import opentelemetry as azure_monitor

os.environ["ENABLE_SIMULATION"] = "true"
os.environ["APP_ENV"] = "development"

configure_azure_monitor = azure_monitor.configure_azure_monitor
azure_monitor.configure_azure_monitor = lambda **options: None
try:
    from app import app
finally:
    azure_monitor.configure_azure_monitor = configure_azure_monitor


@pytest.fixture
def client():
    return TestClient(app)


def test_incident_status_series_start_at_zero(client):
    metrics = client.get("/metrics").text

    for status_code in ("200", "500", "504"):
        assert f'incident_requests_total{{status_code="{status_code}"}} 0.0' in metrics


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        ("healthy", 200),
        ("slow_dependency", 200),
        ("dependency_timeout", 504),
        ("application_error", 500),
    ],
)
def test_simulation_returns_expected_status(client, scenario, expected_status):
    response = client.post("/simulate", json={"scenario": scenario})

    assert response.status_code == expected_status
    if expected_status == 200:
        assert response.json() == {
            "status": "success",
            "message": "Simulation completed",
        }


def test_simulation_metrics_are_exported_separately(client):
    response = client.post("/simulate", json={"scenario": "healthy"})
    metrics = client.get("/metrics").text

    assert response.status_code == 200
    assert 'incident_requests_total{status_code="200"}' in metrics
    assert "incident_request_duration_seconds_bucket" in metrics
