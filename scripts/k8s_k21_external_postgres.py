"""Exercise two gateway replicas against a disposable external TLS PostgreSQL endpoint."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import asyncpg
from local_acceptance import create_postgres_tls_certificates

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "mcpcodeaway-k21-external-postgres"
NAMESPACE = "mcpcodeaway-k1"
CONTEXT = "kind-mcpcodeaway-k21"
PORT = "55432"
SERVICE = "postgres-tls"


def run(
    command: list[str], *, env: dict[str, str], check: bool = True, input_text: str | None = None
) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=240,
    )
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(command)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def kubectl(
    *args: str, env: dict[str, str], check: bool = True, input_text: str | None = None
) -> str:
    return run(
        ["kubectl", "--context", CONTEXT, *args],
        env=env,
        check=check,
        input_text=input_text,
    )


def compose(env: dict[str, str], *args: str) -> None:
    run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "docker-compose.acceptance.yml"),
            "-f",
            str(ROOT / "docker-compose.postgres-tls.yml"),
            "-f",
            str(ROOT / "docker-compose.postgres-kind.yml"),
            "-p",
            PROJECT,
            *args,
        ],
        env=env,
    )


def current_gateway_secret(env: dict[str, str]) -> tuple[str, str]:
    payload = json.loads(
        kubectl("-n", NAMESPACE, "get", "secret", "gateway-secrets", "-o", "json", env=env)
    )
    dsn = base64.b64decode(payload["data"]["postgres-dsn"]).decode()
    password = base64.b64decode(payload["data"]["postgres-password"]).decode()
    return dsn, password


def update_gateway_dsn(env: dict[str, str], dsn: str) -> None:
    payload = json.dumps({"stringData": {"postgres-dsn": dsn}})
    kubectl(
        "-n",
        NAMESPACE,
        "patch",
        "secret/gateway-secrets",
        "--type=merge",
        "-p",
        payload,
        env=env,
    )


def gateway_pods(env: dict[str, str]) -> list[str]:
    payload = json.loads(
        kubectl(
            "-n",
            NAMESPACE,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=gateway",
            "-o",
            "json",
            env=env,
        )
    )
    return [
        pod["metadata"]["name"]
        for pod in payload["items"]
        if not pod["metadata"].get("deletionTimestamp")
        and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )
    ]


def running_gateway_pods(env: dict[str, str]) -> list[str]:
    payload = json.loads(
        kubectl(
            "-n",
            NAMESPACE,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=gateway",
            "-o",
            "json",
            env=env,
        )
    )
    return [
        pod["metadata"]["name"]
        for pod in payload["items"]
        if not pod["metadata"].get("deletionTimestamp")
        and pod.get("status", {}).get("phase") == "Running"
    ]


def restart_gateway(env: dict[str, str]) -> None:
    kubectl("-n", NAMESPACE, "rollout", "restart", "deployment/gateway", env=env)
    kubectl(
        "-n",
        NAMESPACE,
        "rollout",
        "status",
        "deployment/gateway",
        "--timeout=240s",
        env=env,
    )
    if len(gateway_pods(env)) != 2:
        raise RuntimeError("expected two ready gateway replicas after PostgreSQL configuration")


async def verify_journal(dsn: str, run_tag: str) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        for correlation, expected in (
            (f"k1-sensitive-{run_tag}", "succeeded"),
            (f"k1-independent-{run_tag}", "outcome_unknown"),
        ):
            rows = await connection.fetch(
                "SELECT status, count(*) AS row_count FROM sensitive_write_audits "
                "WHERE correlation_id=$1 GROUP BY status",
                correlation,
            )
            if len(rows) != 1 or rows[0]["status"] != expected or rows[0]["row_count"] != 1:
                raise AssertionError(f"unexpected external journal row for {correlation}: {rows}")
            print(f"external_journal={correlation}|{expected}|1")
    finally:
        await connection.close()


def run_k1_client(env: dict[str, str]) -> str:
    return run(
        [
            "uv",
            "run",
            "python",
            str(ROOT / "scripts/k8s_acceptance.py"),
            "--context",
            CONTEXT,
            "--namespace",
            NAMESPACE,
            "--client-only",
        ],
        env=env,
    )


def apply_external_service(env: dict[str, str], database_ip: str) -> None:
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": SERVICE, "namespace": NAMESPACE},
        "spec": {
            "type": "ClusterIP",
            "ports": [{"name": "postgres", "port": 5432, "targetPort": 5432}],
        },
    }
    endpoints = {
        "apiVersion": "v1",
        "kind": "Endpoints",
        "metadata": {"name": SERVICE, "namespace": NAMESPACE},
        "subsets": [
            {
                "addresses": [{"ip": database_ip}],
                "ports": [{"name": "postgres", "port": 5432, "protocol": "TCP"}],
            }
        ],
    }
    kubectl(
        "apply",
        "-f",
        "-",
        env=env,
        input_text=f"{json.dumps(service)}\n---\n{json.dumps(endpoints)}",
    )


def verify_during_outage(env: dict[str, str]) -> None:
    pods = running_gateway_pods(env)
    if len(pods) != 2:
        raise RuntimeError("expected two gateway pods before the external PostgreSQL interruption")
    for pod in pods:
        source = (
            "import urllib.error,urllib.request\n"
            "try:\n urllib.request.urlopen('http://127.0.0.1:8000/ready',timeout=4)\n"
            "except urllib.error.HTTPError as exc:\n"
            " print('ready_status='+str(exc.code)); raise SystemExit(0 if exc.code==503 else 1)\n"
            "raise SystemExit('readiness unexpectedly succeeded during PostgreSQL outage')\n"
        )
        result = kubectl("-n", NAMESPACE, "exec", pod, "--", "python", "-c", source, env=env)
        if "ready_status=503" not in result:
            raise RuntimeError(f"gateway did not fail readiness during database outage: {pod}")
        print(f"external_postgres_outage={pod}|{result}")


def wait_gateway_recovered(env: dict[str, str]) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if len(gateway_pods(env)) == 2:
            results = []
            for pod in gateway_pods(env):
                results.append(
                    kubectl(
                        "-n",
                        NAMESPACE,
                        "exec",
                        pod,
                        "--",
                        "python",
                        "-c",
                        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/ready',timeout=4).status)",
                        env=env,
                    )
                )
            if all(result.strip() == "200" for result in results):
                print("external_postgres_pool_recovery=both_replicas_ready")
                return
        time.sleep(1)
    raise TimeoutError("gateway pools did not recover after the external PostgreSQL restart")


def main() -> int:
    env = os.environ.copy()
    original_dsn, password = current_gateway_secret(env)
    if kubectl("-n", NAMESPACE, "get", "service", SERVICE, env=env, check=False):
        raise RuntimeError(f"Service/{SERVICE} already exists; refusing to overwrite it")
    run(["docker", "network", "inspect", "kind"], env=env)
    env["MCP_TEST_POSTGRES_PASSWORD"] = password
    env["MCP_TEST_POSTGRES_PORT"] = PORT
    env["MCP_TEST_POSTGRES_HOST_BIND"] = "0.0.0.0"
    env["MCP_TEST_POSTGRES_CERT_DIR"] = ""
    env["GATEWAY_EXTERNAL_TEST_DSN"] = ""
    tls_directory = Path(tempfile.mkdtemp(prefix="mcpcodeaway-k21-external-pg-"))
    tls_directory.chmod(0o755)
    try:
        ca_file, _untrusted_ca = create_postgres_tls_certificates(tls_directory)
        env["MCP_TEST_POSTGRES_CERT_DIR"] = str(tls_directory)
        run(
            [
                "docker",
                "compose",
                "-f",
                str(ROOT / "docker-compose.acceptance.yml"),
                "-f",
                str(ROOT / "docker-compose.postgres-tls.yml"),
                "-f",
                str(ROOT / "docker-compose.postgres-kind.yml"),
                "-p",
                PROJECT,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "90",
                "postgres",
            ],
            env=env,
        )
        dsn = (
            f"postgresql://gateway_test:{quote(password, safe='')}@"
            f"{SERVICE}.{NAMESPACE}.svc.cluster.local:5432/mcpcodeaway_acceptance?"
            + urlencode({"sslmode": "verify-full", "sslrootcert": "/run/postgres-ca/ca.crt"})
        )
        external_test_dsn = (
            f"postgresql://gateway_test:{quote(password, safe='')}@127.0.0.1:{PORT}/"
            "mcpcodeaway_acceptance?"
            + urlencode({"sslmode": "verify-full", "sslrootcert": str(ca_file)})
        )
        container_id = run(
            [
                "docker",
                "compose",
                "-f",
                str(ROOT / "docker-compose.acceptance.yml"),
                "-f",
                str(ROOT / "docker-compose.postgres-tls.yml"),
                "-f",
                str(ROOT / "docker-compose.postgres-kind.yml"),
                "-p",
                PROJECT,
                "ps",
                "-q",
                "postgres",
            ],
            env=env,
        )
        inspected_container = json.loads(run(["docker", "inspect", container_id], env=env))[0]
        database_ip = inspected_container["NetworkSettings"]["Networks"]["kind"]["IPAddress"]
        apply_external_service(env, database_ip)
        kubectl(
            "-n",
            NAMESPACE,
            "create",
            "secret",
            "generic",
            "postgres-ca",
            f"--from-file=ca.crt={ca_file}",
            env=env,
        )
        kubectl(
            "apply",
            "-f",
            str(ROOT / "deploy/k8s/local/gateway.yaml"),
            env=env,
        )
        update_gateway_dsn(env, dsn)
        restart_gateway(env)
        if not len(gateway_pods(env)) == 2:
            raise RuntimeError("both gateway replicas must be connected to the external database")
        output = run_k1_client(env)
        print(output)
        run_tag = next(
            line.split("=", 1)[1] for line in output.splitlines() if line.startswith("run_tag=")
        )
        awaitable = verify_journal(external_test_dsn, run_tag)
        asyncio.run(awaitable)

        compose(env, "stop", "--timeout", "2", "postgres")
        try:
            verify_during_outage(env)
        finally:
            compose(env, "start", "postgres")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                compose(env, "exec", "-T", "postgres", "pg_isready", "-U", "gateway_test")
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise TimeoutError("external PostgreSQL fixture did not restart")
        wait_gateway_recovered(env)
        print("external_postgres_tls_two_replicas=passed")
        return 0
    finally:
        try:
            compose(env, "start", "postgres")
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        try:
            update_gateway_dsn(env, original_dsn)
            restart_gateway(env)
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        kubectl(
            "-n",
            NAMESPACE,
            "delete",
            "secret",
            "postgres-ca",
            "--ignore-not-found=true",
            "--wait=false",
            env=env,
            check=False,
        )
        kubectl(
            "-n",
            NAMESPACE,
            "delete",
            "service",
            SERVICE,
            "--ignore-not-found=true",
            "--wait=false",
            env=env,
            check=False,
        )
        kubectl(
            "-n",
            NAMESPACE,
            "delete",
            "endpoints",
            SERVICE,
            "--ignore-not-found=true",
            "--wait=false",
            env=env,
            check=False,
        )
        compose(env, "down", "--volumes", "--remove-orphans")
        shutil.rmtree(tls_directory, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
