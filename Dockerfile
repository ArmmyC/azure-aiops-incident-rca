FROM ghcr.io/astral-sh/uv:0.12.24 AS uv

FROM python:3.12-slim

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-default-groups --no-install-project

ENV PATH="/app/.venv/bin:$PATH"

# telemetry.py configures Azure Monitor before app.py imports FastAPI.
COPY app.py telemetry.py ./

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
