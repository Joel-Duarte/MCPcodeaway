# Cloud & Sandbox Scalability Architecture

`MCPcodeaway` separates the **Control Plane** (stateless HTTP MCP gateway, OIDC/RBAC policy, delegated token exchange, VPC upstream routing) from the **Execution Plane** (Code Mode Monty sandbox) so each layer scales independently with zero vendor lock-in.

## 1. Gateway Control Plane Scaling

- **Stateless Streamable HTTP (`/mcp`)**: Every request carries its OIDC Bearer JWT and completes in a single stateless HTTP round-trip—no sticky sessions, no in-memory session pin across pods.
- **Externalized Cache & Rate Limiting (`GATEWAY_REDIS_URL`)**: Switching from `InMemoryCacheBackend` to `RedisCacheBackend` shares `(tenant_id, sub, audience, scope)` delegated token caches and sliding-window rate-limit counters across all gateway replicas.
- **Pooled Upstream Connections & Stateful `Mcp-Session-Id`**: `UpstreamClientManager` maintains a pooled HTTP keep-alive client (`max_connections=100`, `max_keepalive_connections=20`) closed cleanly on shutdown, injects W3C `traceparent` headers on outbound calls, and negotiates/forwards `Mcp-Session-Id` for stateful Streamable HTTP MCP upstreams (`stateful_session: true`).
- **Audit Persistence**: Local dev uses zero-dependency SQLite WAL (`SQLiteAuditStore`); multi-replica production clusters use `PostgresAuditStore` (`mcpcodeaway[postgres]`, `GATEWAY_POSTGRES_DSN`) implementing the same `AuditStore` protocol while exporting OpenTelemetry spans via OTLP (`mcpcodeaway[otlp]`, `GATEWAY_OTLP_ENDPOINT`) and Prometheus metrics on `/metrics`.
- **Barebones-First Composable Profiles (`GATEWAY_PROFILE`)**: `config/profiles.yaml` defaults custom profiles onto barebones `production` (real MCP + OIDC/OBO + Prometheus `/metrics` + tenant-isolated `/audit/*` + `/audit/value`, with zero demo routes, dev tokens, comparator, or `examples/` in the `gateway` Docker target). `staging` and `demo` extend `production` declaratively, and users can define custom profiles via `extends:`.

## 2. Where Code Mode Sandboxes Run (`SandboxBackend`)

| Deployment Model | How It Works | Strengths | Trade-offs |
| :--- | :--- | :--- | :--- |
| **`LocalMontyBackend`** (in-process Rust worker pool) | Embedded `pydantic-monty` worker pool inside gateway container/Lambda | Sub-ms cold start, zero network hop for host callbacks | CPU/memory of untrusted scripts share pod/Lambda limits |
| **`RemoteMontyWorkerBackend`** (`sandbox_worker.py` over WS reverse-RPC) | Egress-isolated `sandbox-worker` pods run Monty and stream external calls back to gateway `HostDispatcher` | Independent HPA scaling on CPU; zero OBO tokens, secrets, or VPC routes inside worker pods | ~1–2ms WebSocket hop per external tool batch |
| **Cloudflare `DynamicWorkerExecutor`** | V8 isolates spawned per execution via Worker Loader bindings | Edge global scale, native V8 isolate cold start | Ties execution to JS/TS & Cloudflare edge outside private VPC |
| **Pydantic Logfire Agent Sandboxes / MicroVMs** | Managed remote Monty / Firecracker execution service | Turnkey remote isolation + built-in Logfire traces | External SaaS/egress hop for private-VPC workloads |

### Why Reverse-RPC Over WebSocket Matters for Enterprise Security

In `RemoteMontyWorkerBackend`:
1. The gateway opens a WebSocket to `ws://sandbox-worker:8002/ws/execute` on an **internal-only** network (`internal: true` in `docker-compose.yml`, NetworkPolicy deny-all egress in Kubernetes), authenticated via `GATEWAY_SANDBOX_WORKER_SECRET` (`Authorization: Bearer` / `X-Worker-Secret`, fail-closed when `GATEWAY_APP_ENV=prod`).
2. `sandbox-worker` never sees the user's OIDC assertion, downstream delegated tokens, API keys, or internal upstream URLs.
3. When generated Python awaits `snowflake_ledger__query_debt_ledger(...)`, the worker sends `{type: "call", tool, args, kwargs}` back to `HostDispatcher`, which validates RBAC, JSON Schema, and call budgets before calling downstream.
4. **Host-Stamped Provenance & Grounding**: `HostDispatcher` hashes every downstream response (`output_sha256`) and builds a signed `_provenance` envelope (`tools_called`, `tool_output_digests`, `unverified_identifiers`, `provenance_hmac`) completely outside Monty. Enabling `GATEWAY_REQUIRE_TOOL_GROUNDING=true` (or `X-Require-Grounding: true`) blocks zero-call fabrications (`UngroundedResultError`) and hallucinated entity IDs (`UnverifiedEntityError`) while allowing legitimate Python arithmetic (such as computed deltas).

### Why Gateway-Side Monty Instead of Client-Side Local Execution

Client-side code execution (letting a local coding agent generate and run SDK scripts on a laptop) trades away four properties critical to an enterprise MCP Gateway:
- **Universal host compatibility:** Non-coding MCP clients (Copilot Studio, web chatbots, SaaS workflows) have no local Python runtime and speak only MCP JSON-RPC.
- **VPC token & secret containment:** OBO token exchange and API credentials remain strictly inside `HostDispatcher` inside the cluster.
- **WAN payload containment:** Bulk upstream responses (e.g., 200+ billing/ledger rows) are joined and filtered in Monty next to backends before crossing the network into the LLM context window.
- **Unified governance:** One host-enforced execution budget, redaction pipeline, OpenTelemetry trace, and cryptographic provenance record per run (no client-side SDK export by design).

## 3. Handling `mcp_stdio` vs. `mcp_http` / `openapi` at Scale

- **`mcp_http` & `openapi`**: Pure HTTP/JSON-RPC, naturally stateless and load-balanced across pods/PrivateLink/VPC endpoints.
- **`mcp_stdio`**: Spawns a local child process per call, so `GatewaySettings` blocks `mcp_stdio` when `GATEWAY_APP_ENV=prod` (`UpstreamRegistry` filters `mcp_stdio` and `POST /admin/upstreams` returns `403`). To run a stdio-only MCP server in Kubernetes, wrap it in a dedicated **stdio-to-HTTP sidecar container** inside its own Deployment and register its internal Service URL as `type: mcp_http`.
