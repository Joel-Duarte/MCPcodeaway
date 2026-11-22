# Kubernetes K1: local kind acceptance

K1 is an opt-in local deployment and acceptance exercise. It demonstrates two gateway
processes using a shared PostgreSQL journal in kind. It is not a production Kubernetes
deployment recommendation.

## Prerequisites and resource use

Use Docker Engine, `kubectl`, `kind` 0.33.0, OpenSSL, Python 3, and `rg`. The cluster pins
kind's Kubernetes 1.37.0 node image by digest. The local cluster uses one control-plane node,
two gateway replicas, a single disposable PostgreSQL pod, and one local mock-upstream pod.
Allow about 4 CPU cores and 4 GiB RAM for the cluster and image builds. Images and base
images are fetched on the first run; application images stay local and are loaded into kind.

The default developer and CI paths do not call these scripts and do not require kind.

## Create, build, deploy, inspect, test, remove

```bash
./scripts/k8s-local.sh create
./scripts/k8s-local.sh build
./scripts/k8s-local.sh deploy
./scripts/k8s-local.sh status
./scripts/k8s-local.sh test
./scripts/k8s-local.sh destroy
```

The commands use only cluster `mcpcodeaway-k1` and namespace `mcpcodeaway-k1`. `destroy`
deletes that namespace and named kind cluster, which removes their node containers, network,
and in-cluster disposable PostgreSQL data. It removes only generated key material under the
ignored `.k8s-local/mcpcodeaway-k1/` directory. It does not run broad Docker or Kubernetes
cleanup commands.

## Configuration, credentials, and network paths

The K1 ConfigMap sets the local `demo` profile, `sandbox` environment, developer OIDC mode,
the upstream configuration path, and the local-only test instance header. The deployment
reads the PostgreSQL DSN, database password, admin session key, provenance key, task dedupe
key, and shared local OIDC signing key from a namespace Secret.
`deploy/k8s/local/secrets.example.yaml` contains placeholders only. The deploy command
generates random local values and a private
RSA signing key in the ignored local state directory; it does not print them or put them in
the image.

Both gateway pods connect to the same in-namespace `postgres` ClusterIP service. PostgreSQL
uses `emptyDir`, has one replica, and is disposable. Its main process runs as UID/GID 70;
a short-lived UID 0 init container assigns ownership with only the `CHOWN` capability. This
avoids fragile kind-to-host routing and does not reuse the repository's host-published Compose
database. The gateway journal uses the existing `GATEWAY_POSTGRES_DSN` setting and schema
creation code. A
PostgreSQL outage during gateway startup causes startup to fail and Kubernetes to restart the
pod; `/ready` returns 503 for a lost PostgreSQL connection after startup. Once PostgreSQL is
available, the pod retries startup and initializes the existing schema.

The gateway Service is ClusterIP. Pod traffic is limited to the cluster network: gateway to
PostgreSQL on TCP 5432, gateway to the mock upstream on TCP 8001, and acceptance client to
gateway HTTP on TCP 8000. DNS is provided by the cluster. There is no ingress, host-published
PostgreSQL, service mesh, or NetworkPolicy in this local setup. No claim is made that
NetworkPolicy enforcement is active.

For an actual deployment, provide real OIDC/OBO configuration, rotated secrets through the
deployment environment's approved secret delivery mechanism, a managed PostgreSQL service,
durable database backups, real upstream endpoints and network controls, and environment-
appropriate resource and availability settings. The local dev OIDC signing key and mock
upstream are for acceptance only. Entra OIDC/OBO behavior is not changed by K1.

## Replicas and process-local state

Sensitive-write audit rows, approval state, dynamic upstream records, settings, and other
store-backed state use PostgreSQL. The acceptance workflow checks a sensitive-write row
through PostgreSQL after the request, a replacement pod, and a rolling restart. It also inserts
an `executing` audit row using the gateway's dedupe algorithm to simulate a process stopping
immediately after claim. A retry through a different replica is rejected, with no new upstream
side effect, and the row remains `executing` through later pod lifecycle changes. This is a
simulated post-claim crash point; the repository's process-crash acceptance suite separately
hard-kills a process at that lifecycle point. Experiment
bucket selection hashes experiment ID, tenant, subject, and request ID, so it is deterministic
across pods for a stable request. Authorization and approval policy decisions still run in
each gateway process using the same configuration and shared database-backed policy data.

Some runtime data remains process-local: metrics, process statistics, discovery samples,
the in-memory tool catalog, and the default in-memory cache. In particular, local rate-limit
windows and catalog-epoch notifications are not shared without the existing optional Redis
backend. K1 does not add Redis. Do not treat per-pod metrics or rate limits as a cluster-wide
view or quota. A restart also clears each pod's process-local observations. No authorization
decision or approval claim is based on a process-local cache in the K1 fixture.

The local-only `X-Gateway-Instance` response header is disabled unless
`GATEWAY_EXPOSE_INSTANCE_ID=true`; K1 enables it to prove which pods served ClusterIP
requests. It contains only the pod name and is intended for acceptance, not as an external
identity or security signal.

## Probes, shutdown, and writable paths

The startup and liveness probes use the existing `/health` endpoint. Readiness uses existing
`/ready`, which includes a PostgreSQL health check when PostgreSQL storage is selected. The
gateway runs as UID/GID 10001, drops Linux capabilities, denies privilege escalation, uses
RuntimeDefault seccomp, and has a read-only root filesystem. `/tmp` is the only writable
gateway path; the SQLite path points there only as an unused local fallback. No Docker socket
or privileged workload is mounted. The 45-second gateway termination grace period allows
Uvicorn to finish the ASGI lifespan and the gateway to cancel background tasks and close
clients and database pools.

The acceptance check identifies two pod names through requests to the ClusterIP Service,
checks discovery and read-only invocation in all four execution modes, confirms protected-write
approval, a read-only-only Auto experiment, and a cross-replica retry, checks aggregate side
effects from the local mock, and reads journal rows from PostgreSQL. It verifies a seeded
`executing` claim stays blocked across a
different-pod retry and later pod lifecycle changes. It replaces a pod, verifies readiness and
shared-journal recovery, then performs a rolling restart while issuing readiness requests.
During a controlled PostgreSQL outage, it checks `/ready` returns 503, observes a new gateway
pod fail startup, and verifies gateway startup recovers after PostgreSQL returns. It also
confirms that Kubernetes OOM-kills an isolated 256 MiB allocator limited to 64 MiB. The Docker
smoke check confirms `/health` and `/ready` and observes Uvicorn's ASGI shutdown completion after
SIGTERM. The suite does not assert zero downtime, time a hard kill between upstream commit and
HTTP response in Kubernetes, test exhaustion in the gateway process, or exercise a real
corporate identity provider.
