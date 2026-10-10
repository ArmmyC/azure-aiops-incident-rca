import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "metrics,health")

from azure.monitor.opentelemetry import configure_azure_monitor

configure_azure_monitor(
    connection_string=os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"],
    logger_name="aiops",
    sampling_ratio=1.0,
)

logger = logging.getLogger("aiops")
logger.setLevel(logging.INFO)

has_console_handler = any(
    getattr(handler, "_aiops_console_handler", False)
    for handler in logger.handlers
)
if not has_console_handler:
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    console_handler._aiops_console_handler = True
    logger.addHandler(console_handler)
