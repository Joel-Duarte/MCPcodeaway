# Cloud & Sandbox Scalability Architecture

`MCPcodeaway` separates the **Control Plane** (stateless HTTP MCP gateway, OIDC/RBAC policy, delegated token exchange, VPC upstream routing) from the **Execution Plane** (Code Mode Monty sandbox) so each layer scales independently with zero vendor lock-in.

## 1. Gateway Control Plane Scaling

- **MCP 2026-07-28 Stateless Streamable HTTP (`/mcp`)**: The gateway advertises the current sessionless protocol revision. Every modern request carries protocol version and client capabilities in `_meta`, mirrored by validated HTTP routing headers. Requests are independently authorized and can land on any replica; the gateway does not issue or rely on client MCP session IDs.
- **Load Balancing (Compose Nginx or Cloud ALB / Kubernetes Ingress)**: The gateway uses sessionless HTTP MCP and WebSocket ASGI. Local/VM multi-replica Docker Compose uses [deploy/nginx/nginx.conf](../deploy/nginx/nginx.conf) (`lb` service on `:8080`, preserving `X-Forwarded-*` and SSE streams); AWS/GCP/Azure deployments point **AWS ALB**, **Cloud Load Balancer**, **Envoy Gateway**, or **Kubernetes Ingress** directly at the gateway target group/Service without running Nginx. Any gateway replica can handle any authenticated MCP request; no sticky routing is needed for MCP protocol state.
- **Externalized Cache & Multi-Replica Config Sync (`GATEWAY_REDIS_URL`)**: `RedisCacheBackend` shares `(tenant_id, sub, audience, scope)` OBO tokens, sliding-window rate limits, admin OIDC PKCE state (`pkce:{state}`), and a lightweight `gateway:catalog_epoch` counter across replicas so DB-managed upstream changes trigger automatic catalog reloads on sibling replicas. It stores no MCP session IDs or cookies. HTTP API calls receive credentials per request; cookie-authenticated OpenAPI integrations are rejected and cookies are never reused between calls.
- **Pooled Upstream Connections & Trace Propagation**: `UpstreamClientManager` maintains a pooled HTTP keep-alive client (`max_connections=100`, `max_keepalive_connections=20`) closed cleanly on shutdown and injects W3C `traceparent` headers on outbound calls.
- **Zero-Downtime Key Rotation**: `GATEWAY_SANDBOX_WORKER_SECRET`, `GATEWAY_PROVENANCE_SIGNING_SECRET`, and `GATEWAY_ADMIN_SESSION_SECRET` accept comma-separated `active,previous` secret lists — sending/signing with key `0` while verifying against all keys in constant time during rollout.
- **Audit Persistence & Retention**: Local dev uses SQLite WAL (`SQLiteAuditStore`); multi-replica production clusters use `PostgresAuditStore` (`mcpcodeaway[postgres]`, `GATEWAY_POSTGRES_DSN`), prune old rows via `POST /admin/audit/prune` (`audit_retention_days=90`), and stream high-volume observability via OTLP (`mcpcodeaway[otlp]`) and Prometheus `/metrics`.
- **Barebones Profiles & Standalone Frontend**: `config/profiles.yaml` defaults custom profiles onto barebones `production`. The `gateway` Docker target contains zero Node.js, SPA bundle, or `examples/`. The Nuxt `@ui-sdk/vue` console can either be served at `/dashboard` via the `gateway-ui` image target or deployed standalone on S3/CloudFront/CDN setting `NUXT_PUBLIC_API_BASE` and `GATEWAY_CORS_ALLOWED_ORIGINS`.

### Kubernetes Topology Reference
- **Control Plane Deployment (`gateway` image)**: Mount `config/upstreams.yaml` & `config/profiles.yaml` as **ConfigMaps** (or enable Postgres dynamic sync), inject secrets via **External Secrets Operator** (AWS Secrets Manager / Vault / Key Vault), expose via Kubernetes `Ingress` / AWS ALB, and scale via HPA on HTTP RPS/latency.
- **Execution Plane Deployment (`sandbox-worker` image)**: Dedicated Deployment inside a deny-all-egress `NetworkPolicy` namespace, scaled independently via HPA on CPU (`GET /health` reports live CPU % / RSS / active executions).

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
