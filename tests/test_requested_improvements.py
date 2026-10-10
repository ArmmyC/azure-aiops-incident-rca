import importlib
import logging
import runpy
import sys
from pathlib import Path

import openai

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_telemetry_configures_one_stdout_handler_for_aiops(monkeypatch):
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=test;IngestionEndpoint=https://example.invalid/",
    )

    from azure.monitor import opentelemetry as azure_monitor

    monkeypatch.setattr(azure_monitor, "configure_azure_monitor", lambda **options: None)
    telemetry = importlib.import_module("telemetry")
    importlib.reload(telemetry)

    logger = logging.getLogger("aiops")
    stdout_handlers = [
        handler
        for handler in logger.handlers
        if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout
    ]

    assert logger.level == logging.INFO
    assert len(stdout_handlers) == 1
    assert stdout_handlers[0].formatter._fmt == (
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )


def test_llm_script_uses_foundry_environment_variables(monkeypatch):
    captured = {}

    class FakeResponse:
        output_text = "Hello!"
        status = "completed"
        usage = None

    class FakeClient:
        def __init__(self, *, base_url, api_key):
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            self.responses = self

        def create(self, *, model, input):
            captured["model"] = model
            captured["input"] = input
            return FakeResponse()

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://example.invalid/openai/v1/")
    monkeypatch.setenv("FOUNDRY_API_KEY", "test-key")
    monkeypatch.setenv("FOUNDRY_MODEL", "gpt-5-mini")
    for name in ("LLM_ENDPOINT", "LLM_DEPLOYMENT_NAME", "API_KEY"):
        monkeypatch.delenv(name, raising=False)

    runpy.run_path(str(PROJECT_ROOT / "LLM.py"), run_name="__main__")

    assert captured == {
        "base_url": "https://example.invalid/openai/v1/",
        "api_key": "test-key",
        "model": "gpt-5-mini",
        "input": "Hello!",
    }
