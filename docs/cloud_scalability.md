# Cloud & Sandbox Scalability Architecture

`MCPcodeaway` separates the **Control Plane** (stateless HTTP MCP gateway, OIDC/RBAC policy, delegated token exchange, VPC upstream routing) from the **Execution Plane** (Code Mode Monty sandbox) so each layer scales independently with zero vendor lock-in.

## 1. Gateway Control Plane Scaling

- **Stateless Streamable HTTP (`/mcp`)**: Every request carries its OIDC Bearer JWT and completes in a single stateless HTTP round-trip—no sticky sessions, no in-memory session pin across pods.
- **Externalized Cache & Rate Limiting (`GATEWAY_REDIS_URL`)**: Switching from `InMemoryCacheBackend` to `RedisCacheBackend` shares `(tenant_id, sub, audience, scope)` delegated token caches and sliding-window rate-limit counters across all gateway replicas.
- **Audit Persistence**: Local dev uses zero-dependency SQLite WAL (`SQLiteAuditStore`); multi-replica production clusters implement the same `AuditStore` protocol against Postgres or DynamoDB, while exporting OpenTelemetry spans via OTLP.

## 2. Where Code Mode Sandboxes Run (`SandboxBackend`)

| Deployment Model | How It Works | Strengths | Trade-offs |
| :--- | :--- | :--- | :--- |
| **`LocalMontyBackend`** (in-process Rust worker pool) | Embedded `pydantic-monty` worker pool inside gateway container/Lambda | Sub-ms cold start, zero network hop for host callbacks | CPU/memory of untrusted scripts share pod/Lambda limits |
| **`RemoteMontyWorkerBackend`** (`sandbox_worker.py` over WS reverse-RPC) | Egress-isolated `sandbox-worker` pods run Monty and stream external calls back to gateway `HostDispatcher` | Independent HPA scaling on CPU; zero OBO tokens, secrets, or VPC routes inside worker pods | ~1–2ms WebSocket hop per external tool batch |
| **Cloudflare `DynamicWorkerExecutor`** | V8 isolates spawned per execution via Worker Loader bindings | Edge global scale, native V8 isolate cold start | Ties execution to JS/TS & Cloudflare edge outside private VPC |
| **Pydantic Logfire Agent Sandboxes / MicroVMs** | Managed remote Monty / Firecracker execution service | Turnkey remote isolation + built-in Logfire traces | External SaaS/egress hop for private-VPC workloads |

### Why Reverse-RPC Over WebSocket Matters for Enterprise Security

In `RemoteMontyWorkerBackend`:
1. The gateway opens a WebSocket to `ws://sandbox-worker:8002/ws/execute` on an **internal-only** network (`internal: true` in `docker-compose.yml`, NetworkPolicy deny-all egress in Kubernetes).
2. `sandbox-worker` never sees the user's OIDC assertion, downstream delegated tokens, API keys, or internal upstream URLs.
3. When generated Python awaits `snowflake_ledger__query_debt_ledger(...)`, the worker sends `{type: "call", tool, args, kwargs}` back to `HostDispatcher`, which validates RBAC, JSON Schema, and call budgets before calling downstream.

## 3. Handling `mcp_stdio` vs. `mcp_http` / `openapi` at Scale

- **`mcp_http` & `openapi`**: Pure HTTP/JSON-RPC, naturally stateless and load-balanced across pods/PrivateLink/VPC endpoints.
- **`mcp_stdio`**: Spawns a local child process per call, so `GatewaySettings` blocks `mcp_stdio` when `GATEWAY_APP_ENV=prod` (`UpstreamRegistry` filters `mcp_stdio` and `POST /admin/upstreams` returns `403`). To run a stdio-only MCP server in Kubernetes, wrap it in a dedicated **stdio-to-HTTP sidecar container** inside its own Deployment and register its internal Service URL as `type: mcp_http`.
