#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="mcpcodeaway-k1"
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
    --from-literal="postgres-dsn=postgresql://gateway:${password}@postgres:5432/mcpcodeaway" \
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
  build) build_images ;;
  deploy) deploy_local ;;
  status) show_status ;;
  test)
    require kubectl
    python3 "${ROOT}/scripts/k8s_acceptance.py" --context "$CONTEXT" --namespace "$NAMESPACE"
    ;;
  destroy) destroy_local ;;
  *)
    echo "Usage: $0 {create|build|deploy|status|test|destroy}" >&2
    exit 2
    ;;
esac
