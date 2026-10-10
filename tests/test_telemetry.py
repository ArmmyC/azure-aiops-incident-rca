import os
import subprocess
import sys


def test_azure_monitor_bootstrap_precedes_fastapi_and_uses_app_configuration():
    connection_string = "InstrumentationKey=test;IngestionEndpoint=https://example.invalid/"
    environment = os.environ.copy()
    environment["APPLICATIONINSIGHTS_CONNECTION_STRING"] = connection_string
    script = """
import sys
from azure.monitor import opentelemetry as azure_monitor

configured = {}
def capture_configuration(**options):
    assert "fastapi" not in sys.modules
    configured.update(options)

azure_monitor.configure_azure_monitor = capture_configuration
import telemetry
assert configured == {
    "connection_string": "InstrumentationKey=test;IngestionEndpoint=https://example.invalid/",
    "logger_name": "aiops",
    "sampling_ratio": 1.0,
}
"""

    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        env=environment,
        capture_output=True,
        text=True,
    )
