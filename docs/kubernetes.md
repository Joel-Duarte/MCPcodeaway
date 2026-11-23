# Kubernetes Deployment & Architecture Guide

MCPcodeaway runs in Kubernetes by separating the stateless **Control Plane** (HTTP/WebSocket gateway) from the sandboxed **Execution Plane** (Monty Python sandbox worker) so each layer scales and isolates independently.

---

## 1. Architecture Topology

```
                  Internet / Private Network
                             │
                      [ Ingress / ALB ]
                             │ (Sessionless MCP HTTP / WS)
                             ▼
         ┌───────────────────────────────────────┐
         │     Gateway Pods (Control Plane)      │
         │  • OIDC RS256 Auth & Tenant Check     │
         │  • RFC 8693 / Entra OBO Token Exch    │
         │  • Dynamic Mode Routing (Classic/Code)│
         │  • ABAC Row Filtering & PII Redact    │
         └───────┬───────────────┬───────────────┘
                 │               │
  Internal WS    │               │ SQL / Cache
  Reverse-RPC    ▼               ▼
┌───────────────────────┐  ┌───────────────────────────────────┐
│ Sandbox-Worker Pods   │  │ Shared Infrastructure             │
│ • pydantic-monty Rust │  │ • Redis: rate limits & OBO cache  │
│ • NetworkPolicy:      │  │ • PostgreSQL: write journal &     │
│   DENY ALL EGRESS     │  │   audit persistence (DML-only)    │
└───────────────────────┘  └───────────────────────────────────┘
```

### Components

1. **Gateway Deployment (`gateway` Docker target):**
   - Pure Python/Uvicorn runtime (zero Node.js or frontend assets).
   - Stateless and sessionless: any replica can handle any modern MCP request.
   - Scale horizontally via Horizontal Pod Autoscaler (HPA) on HTTP RPS or latency.
   - Mounts `config/upstreams.yaml` and `config/profiles.yaml` via ConfigMaps (or syncs from PostgreSQL).

2. **Sandbox Worker Deployment (`sandbox-worker` Docker target):**
   - Runs ephemeral `pydantic-monty` sandboxes.
   - Communicates with gateway replicas exclusively via internal WebSocket reverse-RPC.
   - Protected by a Kubernetes `NetworkPolicy` denying all outbound network traffic (deny-all egress). Workers never touch upstream APIs, credentials, or the internet directly.
   - Scale independently via HPA based on CPU utilization.

3. **External Dependencies:**
   - **PostgreSQL**: Stores sensitive write journals, approvals, settings, and audit logs. Gateway pods use a restricted DML-only database role.
   - **Redis**: Shares sliding-window rate limits, OBO token caches, and catalog epoch reload events across replicas.

---

## 2. Local Kind Cluster Testing

A self-contained local Kubernetes acceptance suite is provided via `scripts/k8s-local.sh`, running inside a multi-node [kind](https://kind.sigs.k8s.io/) cluster.

### Prerequisites

- Docker Engine
- `kubectl`
- `kind` (>= 0.20.0)
- Python 3.12+ with `uv`

### Lifecycle Commands

```bash
# 1. Create the local kind cluster with ingress ports mapped
./scripts/k8s-local.sh create

# 2. Build local container images (gateway, worker, mock backends)
./scripts/k8s-local.sh build

# 3. Deploy manifests, PostgreSQL, secrets, and services
./scripts/k8s-local.sh deploy

# 4. Inspect running pods, deployments, and logs
./scripts/k8s-local.sh status

# 5. Run end-to-end integration and multi-replica journal tests
./scripts/k8s-local.sh test

# 6. Tear down the cluster and clean up all local state
./scripts/k8s-local.sh destroy
```

All resources, namespaces, and volumes are isolated to the `mcpcodeaway-k1` cluster and namespace.

---

## 3. Production State Boundaries & Hardening

| State Category | Storage Backend | Multi-Replica Behavior |
| :--- | :--- | :--- |
| **Sensitive Write Journal** | PostgreSQL (`sensitive_write_audits`) | Atomic dedupe key claims coordinate across replicas; prevents duplicate replay of in-flight mutations. |
| **Approvals** | PostgreSQL (`tool_approvals`) | Single-use approvals claimed atomically across replicas. |
| **Rate Limiting** | Redis (or process memory fallback) | Distributed sliding-window counters when Redis is configured. |
| **OIDC / OBO Tokens** | Redis cache (scoped by tenant, sub, audience) | Isolated by caller identity and token TTL; no cross-tenant leakage. |
| **JWKS Signing Keys** | Local memory cache (300s TTL) | Refreshes automatically on unknown `kid`; fails closed on fetch error. |
| **Admin Sessions** | Signed HMAC `HttpOnly` cookies | Stateless; all replicas verify consistently via shared `GATEWAY_ADMIN_SESSION_SECRET`. |
| **Sandbox Execution** | Ephemeral worker processes | Networkless, timeout-bounded, memory-capped execution inside Monty. |

### Network Policy Example (Sandbox Worker Egress Isolation)

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: sandbox-worker-deny-all-egress
  namespace: mcpcodeaway
spec:
  podSelector:
    matchLabels:
      app: sandbox-worker
  policyTypes:
    - Egress
  egress: []  # Zero egress allowed: cannot access internet or internal VPC
```

All host callbacks from sandbox scripts stream back to the gateway via the inbound WebSocket connection initiated by the gateway.
