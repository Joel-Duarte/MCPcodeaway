FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app
COPY pyproject.toml .python-version ./
COPY src ./src
COPY config ./config
COPY examples ./examples
RUN uv sync --no-dev --extra redis

FROM python:3.13-slim-bookworm

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/config /app/config
COPY --from=builder /app/examples /app/examples
COPY --from=builder /app/pyproject.toml /app/pyproject.toml

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    GATEWAY_SQLITE_PATH=/tmp/gateway.db

USER appuser
EXPOSE 8000

CMD ["uvicorn", "gateway.server:app", "--host", "0.0.0.0", "--port", "8000"]
