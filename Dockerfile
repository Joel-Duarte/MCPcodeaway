FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app
COPY pyproject.toml .python-version ./
COPY src ./src
COPY config ./config
RUN uv sync --no-dev --extra redis --extra postgres --extra otlp

FROM node:22-slim AS web-builder

WORKDIR /app/web
COPY web/package*.json ./
RUN npm ci
COPY web ./
ENV NUXT_TELEMETRY_DISABLED=1
RUN npm run generate

FROM python:3.13-slim-bookworm AS base-runtime

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/config /app/config
COPY --from=builder /app/pyproject.toml /app/pyproject.toml

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    GATEWAY_SQLITE_PATH=/tmp/gateway.db

# Target 1: Barebones production gateway (zero Node, zero SPA, zero examples)
FROM base-runtime AS gateway
ENV GATEWAY_PROFILE=production
USER appuser
EXPOSE 8000
CMD ["uvicorn", "gateway.server:app", "--host", "0.0.0.0", "--port", "8000"]

# Target 2: Egress-isolated Monty sandbox worker (WebSocket reverse-RPC)
FROM base-runtime AS sandbox-worker
USER appuser
EXPOSE 8002
CMD ["uvicorn", "gateway.sandbox_worker:app", "--host", "0.0.0.0", "--port", "8002"]

# Target 3: Gateway with compiled Nuxt + @ui-sdk/vue Admin Console SPA
FROM gateway AS gateway-ui
COPY --from=web-builder --chown=appuser:appuser /app/web/.output/public ./src/gateway/static/spa
ENV GATEWAY_PROFILE=staging
USER appuser
EXPOSE 8000
CMD ["uvicorn", "gateway.server:app", "--host", "0.0.0.0", "--port", "8000"]

# Target 4: Full local demo workbench (includes examples/ mock servers + Admin Console)
FROM gateway-ui AS gateway-demo
COPY --chown=appuser:appuser examples ./examples
ENV GATEWAY_PROFILE=demo
USER appuser
EXPOSE 8000
CMD ["uvicorn", "gateway.server:app", "--host", "0.0.0.0", "--port", "8000"]
