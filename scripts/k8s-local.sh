#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${MCP_K8S_CLUSTER:-mcpcodeaway-k1}"
NAMESPACE="mcpcodeaway-k1"
CONTEXT="kind-${CLUSTER}"
LOCAL_STATE="${ROOT}/.k8s-local/${CLUSTER}"
MANIFESTS="${ROOT}/deploy/k8s/local"

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    exit 2
  }
}

cluster_exists() {
  kind get clusters 2>/dev/null | rg -qx "$CLUSTER"
}

ensure_local_secrets() {
  mkdir -p "$LOCAL_STATE"
  chmod 700 "$LOCAL_STATE"
  if [[ ! -s "${LOCAL_STATE}/dev-oidc-private.pem" ]]; then
    openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
      -out "${LOCAL_STATE}/dev-oidc-private.pem" >/dev/null 2>&1
    chmod 600 "${LOCAL_STATE}/dev-oidc-private.pem"
  fi
  if [[ ! -s "${LOCAL_STATE}/postgres-password" ]]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))' \
      > "${LOCAL_STATE}/postgres-password"
    chmod 600 "${LOCAL_STATE}/postgres-password"
  fi
  for key in admin-session-secret provenance-signing-secret task-dedupe-secret; do
    if [[ ! -s "${LOCAL_STATE}/${key}" ]]; then
      python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > "${LOCAL_STATE}/${key}"
      chmod 600 "${LOCAL_STATE}/${key}"
    fi
  done
}

create_cluster() {
  require docker
  require kind
  if cluster_exists; then
    echo "kind cluster '${CLUSTER}' already exists"
    return
  fi
  kind create cluster --name "$CLUSTER" --config "${MANIFESTS}/cluster.yaml" --wait 180s
  kubectl config use-context "$CONTEXT" >/dev/null
}

create_netpol_cluster() {
  require docker
  require kind
  if cluster_exists; then
    echo "kind cluster '${CLUSTER}' already exists"
    return
  fi
  kind create cluster --name "$CLUSTER" --config "${MANIFESTS}/cluster-netpol.yaml" --wait 0s
  kubectl config use-context "$CONTEXT" >/dev/null
  python3 -c 'from urllib.request import urlopen; p="https://raw.githubusercontent.com/projectcalico/calico/v3.33.0/manifests/calico.yaml"; s=urlopen(p, timeout=30).read().decode(); old="            # - name: CALICO_IPV4POOL_CIDR\n            #   value: \"192.168.0.0/16\""; new="            - name: CALICO_IPV4POOL_CIDR\n              value: \"10.244.0.0/16\""; assert old in s; print(s.replace(old, new))' \
    | kubectl --context "$CONTEXT" apply -f -
  kubectl --context "$CONTEXT" -n kube-system rollout status daemonset/calico-node --timeout=240s
  kubectl --context "$CONTEXT" wait --for=condition=Ready nodes --all --timeout=240s
}

build_images() {
  require docker
  require kind
  cluster_exists || {
    echo "Create the '${CLUSTER}' cluster first: $0 create" >&2
    exit 2
  }
  docker build --target gateway -t mcpcodeaway:k1 "$ROOT"
  docker build --target gateway-demo -t mcpcodeaway-k1-fixture:k1 "$ROOT"
  kind load docker-image mcpcodeaway:k1 mcpcodeaway-k1-fixture:k1 --name "$CLUSTER"
}

deploy_local() {
  require kubectl
  require openssl
  ensure_local_secrets
  kubectl --context "$CONTEXT" apply -f "${MANIFESTS}/namespace.yaml"
  local password
  password="$(cat "${LOCAL_STATE}/postgres-password")"
  kubectl --context "$CONTEXT" -n "$NAMESPACE" create secret generic gateway-secrets \
    --from-literal="postgres-password=${password}" \
    --from-literal="postgres-dsn=postgresql://gateway:${password}@postgres:5432/mcpcodeaway?sslmode=disable" \
    --from-literal="admin-session-secret=$(cat "${LOCAL_STATE}/admin-session-secret")" \
    --from-literal="provenance-signing-secret=$(cat "${LOCAL_STATE}/provenance-signing-secret")" \
    --from-literal="task-dedupe-secret=$(cat "${LOCAL_STATE}/task-dedupe-secret")" \
    --from-file="dev-oidc-private.pem=${LOCAL_STATE}/dev-oidc-private.pem" \
    --dry-run=client -o yaml | kubectl --context "$CONTEXT" apply -f -
  kubectl --context "$CONTEXT" apply -f "${MANIFESTS}/gateway-config.yaml"
  kubectl --context "$CONTEXT" apply -f "${MANIFESTS}/postgres.yaml"
  kubectl --context "$CONTEXT" -n "$NAMESPACE" rollout status deployment/postgres --timeout=180s
  kubectl --context "$CONTEXT" apply -f "${MANIFESTS}/mock-backends.yaml"
  kubectl --context "$CONTEXT" -n "$NAMESPACE" rollout status deployment/mock-backends --timeout=180s
  kubectl --context "$CONTEXT" apply -f "${MANIFESTS}/gateway.yaml"
  kubectl --context "$CONTEXT" -n "$NAMESPACE" rollout restart deployment/gateway >/dev/null
  kubectl --context "$CONTEXT" -n "$NAMESPACE" rollout status deployment/gateway --timeout=240s
}

show_status() {
  require kubectl
  kubectl --context "$CONTEXT" -n "$NAMESPACE" get all -o wide
}

validate_optional() {
  require kubectl
  kubectl --context "$CONTEXT" apply --dry-run=server \
    -f "${MANIFESTS}/network-policy.yaml" \
    -f "${MANIFESTS}/pdb.yaml" \
    -f "${MANIFESTS}/ingress-tls.example.yaml"
}

resource_snapshot() {
  require kubectl
  python3 "${ROOT}/scripts/k8s_resource_snapshot.py" --context "$CONTEXT" --namespace "$NAMESPACE"
}

destroy_local() {
  require kind
  if cluster_exists; then
    kubectl --context "$CONTEXT" delete namespace "$NAMESPACE" --wait=true --timeout=90s \
      --ignore-not-found=true || true
    kind delete cluster --name "$CLUSTER"
  else
    echo "kind cluster '${CLUSTER}' does not exist; no unrelated clusters were changed"
  fi
  if [[ -d "$LOCAL_STATE" ]]; then
    rm -rf -- "$LOCAL_STATE"
  fi
  rmdir "${ROOT}/.k8s-local" 2>/dev/null || true
}

case "${1:-}" in
  create) create_cluster ;;
  create-netpol) create_netpol_cluster ;;
  build) build_images ;;
  deploy) deploy_local ;;
  status) show_status ;;
  validate-optional) validate_optional ;;
  resource-snapshot) resource_snapshot ;;
  test)
    require kubectl
    python3 "${ROOT}/scripts/k8s_acceptance.py" --context "$CONTEXT" --namespace "$NAMESPACE"
    ;;
  destroy) destroy_local ;;
  *)
    echo "Usage: $0 {create|create-netpol|build|deploy|status|test|validate-optional|resource-snapshot|destroy}" >&2
    exit 2
    ;;
esac
