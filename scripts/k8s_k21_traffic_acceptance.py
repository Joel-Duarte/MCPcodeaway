"""Opt-in ingress TLS and enforced NetworkPolicy traffic acceptance for the K1 fixture."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "mcpcodeaway-k1"
INGRESS_NAMESPACE = "ingress-nginx"
PROBE_NAMESPACE = "mcpcodeaway-k21-probe"
HOST = "gateway.example.invalid"
INGRESS_MANIFEST = (
    "https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/"
    "deploy/static/provider/cloud/deploy.yaml"
)


def run(*args: str, check: bool = True, input_text: str | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def kubectl(*args: str, check: bool = True, input_text: str | None = None) -> str:
    return run("kubectl", *args, check=check, input_text=input_text)


def exec_gateway(pod: str, source: str, *, expect_success: bool) -> str:
    result = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "exec", pod, "--", "python", "-c", source],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if expect_success and result.returncode:
        raise RuntimeError(result.stderr.strip())
    if not expect_success and result.returncode == 0:
        raise RuntimeError("a prohibited gateway connection unexpectedly succeeded")
    return (result.stdout + result.stderr).strip()


def wait_port(host: str, port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("kubectl port-forward exited before accepting traffic")
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(f"port-forward did not open {host}:{port}")


def verify_ingress(ca_file: Path, bad_ca_file: Path, cert_file: Path, key_file: Path) -> None:
    kubectl("apply", "-f", INGRESS_MANIFEST)
    kubectl(
        "-n",
        INGRESS_NAMESPACE,
        "rollout",
        "status",
        "deployment/ingress-nginx-controller",
        "--timeout=240s",
    )
    kubectl(
        "-n",
        NAMESPACE,
        "create",
        "secret",
        "tls",
        "gateway-tls",
        f"--cert={cert_file}",
        f"--key={key_file}",
    )
    kubectl(
        "-n",
        NAMESPACE,
        "apply",
        "-f",
        str(ROOT / "deploy/k8s/local/ingress-tls.example.yaml"),
    )
    kubectl(
        "-n",
        NAMESPACE,
        "apply",
        "-f",
        str(ROOT / "deploy/k8s/local/network-policy.yaml"),
    )
    service = json.loads(kubectl("-n", NAMESPACE, "get", "service", "gateway", "-o", "json"))
    spec = service["spec"]
    if (
        spec.get("type") != "ClusterIP"
        or spec.get("externalIPs")
        or any(port.get("nodePort") for port in spec.get("ports", []))
    ):
        raise AssertionError("gateway Service has an unintended external exposure")
    process = subprocess.Popen(
        [
            "kubectl",
            "-n",
            INGRESS_NAMESPACE,
            "port-forward",
            "service/ingress-nginx-controller",
            "18443:443",
            "18080:80",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        wait_port("127.0.0.1", 18443, process)
        resolve_tls = ["--resolve", f"{HOST}:18443:127.0.0.1"]
        health_command = (
            "curl",
            "--noproxy",
            "*",
            "--fail",
            "--silent",
            "--show-error",
            "--cacert",
            str(ca_file),
            *resolve_tls,
            f"https://{HOST}:18443/health",
        )
        health_error: RuntimeError | None = None
        for _ in range(30):
            try:
                health = run(*health_command)
                break
            except RuntimeError as exc:
                health_error = exc
                time.sleep(0.5)
        else:
            handshake = subprocess.run(
                [
                    "openssl",
                    "s_client",
                    "-connect",
                    "127.0.0.1:18443",
                    "-servername",
                    HOST,
                    "-showcerts",
                ],
                input="",
                text=True,
                capture_output=True,
                timeout=10,
            )
            certificate_text = handshake.stdout.split("-----BEGIN CERTIFICATE-----")
            if len(certificate_text) > 1:
                presented = (
                    "-----BEGIN CERTIFICATE-----"
                    + certificate_text[1].split("-----END CERTIFICATE-----")[0]
                    + "-----END CERTIFICATE-----\n"
                )
                details = subprocess.run(
                    ["openssl", "x509", "-noout", "-subject", "-issuer", "-ext", "subjectAltName"],
                    input=presented,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                sys.stderr.write("presented_ingress_certificate=" + details.stdout)
            sys.stderr.write(
                "ingress_controller_logs="
                + kubectl(
                    "-n",
                    INGRESS_NAMESPACE,
                    "logs",
                    "deployment/ingress-nginx-controller",
                    "--tail=40",
                    check=False,
                )
                + "\n"
            )
            raise health_error or RuntimeError("HTTPS ingress health check did not complete")
        if json.loads(health).get("status") != "ok":
            raise AssertionError("HTTPS health request did not reach the gateway")
        token_body = run(
            "curl",
            "--noproxy",
            "*",
            "--fail",
            "--silent",
            "--show-error",
            "--cacert",
            str(ca_file),
            *resolve_tls,
            "-X",
            "POST",
            f"https://{HOST}:18443/dev/auth/token",
            "-H",
            "Content-Type: application/json",
            "-d",
            '{"persona":"gateway_admin","roles":["gateway_admin","platform_admin","admin"],"scopes":["mcp.tools","mcp.admin","mcp.audit","mcp.write"]}',
        )
        token = json.loads(token_body)["access_token"]
        mcp_result = run(
            "curl",
            "--noproxy",
            "*",
            "--fail",
            "--silent",
            "--show-error",
            "--cacert",
            str(ca_file),
            *resolve_tls,
            "-X",
            "POST",
            f"https://{HOST}:18443/mcp?execution=classic",
            "-H",
            f"Authorization: Bearer {token}",
            "-H",
            "Content-Type: application/json",
            "-H",
            "Accept: application/json, text/event-stream",
            "-H",
            "MCP-Protocol-Version: 2026-07-28",
            "-H",
            "Mcp-Method: tools/list",
            "-d",
            '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"k21-ingress","version":"1"}}}}',
        )
        if "tools" not in mcp_result:
            raise AssertionError("MCP tools/list response was missing through ingress")
        try:
            run(
                "curl",
                "--noproxy",
                "*",
                "--fail",
                "--silent",
                "--show-error",
                "--cacert",
                str(bad_ca_file),
                *resolve_tls,
                f"https://{HOST}:18443/health",
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("client accepted an untrusted ingress certificate")
        wrong_host = subprocess.run(
            [
                "curl",
                "--noproxy",
                "*",
                "--output",
                "/dev/null",
                "--silent",
                "--show-error",
                "--cacert",
                str(ca_file),
                "--resolve",
                "wrong.example.invalid:18443:127.0.0.1",
                "https://wrong.example.invalid:18443/health",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if wrong_host.returncode == 0:
            raise AssertionError("client accepted an ingress hostname mismatch")
        http_status = run(
            "curl",
            "--noproxy",
            "*",
            "--silent",
            "--show-error",
            "--max-redirs",
            "0",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            "--resolve",
            f"{HOST}:18080:127.0.0.1",
            f"http://{HOST}:18080/health",
        )
        if http_status not in {"301", "302", "307", "308"}:
            raise AssertionError(f"HTTP policy expected a redirect, received {http_status}")
        print(
            "ingress_tls=passed invalid_ca=rejected hostname_mismatch=checked http_status="
            + http_status
        )
        print("mcp_tools_list=passed gateway_service=ClusterIP")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def verify_policy() -> None:
    gateway_pod = kubectl(
        "-n",
        NAMESPACE,
        "get",
        "pods",
        "-l",
        "app.kubernetes.io/name=gateway",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )
    postgres_check = (
        "import socket; socket.create_connection("
        "('postgres.mcpcodeaway-k1.svc.cluster.local',5432),timeout=3).close(); "
        "print('connected')"
    )
    print("allowed_postgres=" + exec_gateway(gateway_pod, postgres_check, expect_success=True))
    upstream_check = (
        "import socket; socket.create_connection("
        "('mock-backends.mcpcodeaway-k1.svc.cluster.local',8001),timeout=3).close(); "
        "print('connected')"
    )
    print("allowed_upstream=" + exec_gateway(gateway_pod, upstream_check, expect_success=True))
    pod_yaml = f"""apiVersion: v1
kind: Pod
metadata:
  name: sink
  namespace: {PROBE_NAMESPACE}
  labels:
    app: sink
spec:
  containers:
    - name: sink
      image: python:3.13-alpine
      command: [python, -m, http.server, "18080"]
---
apiVersion: v1
kind: Service
metadata:
  name: sink
  namespace: {PROBE_NAMESPACE}
spec:
  selector:
    app: sink
  ports:
    - port: 18080
      targetPort: 18080
"""
    kubectl("apply", "-f", "-", input_text=pod_yaml)
    kubectl("-n", PROBE_NAMESPACE, "wait", "--for=condition=Ready", "pod/sink", "--timeout=180s")
    egress_result = exec_gateway(
        gateway_pod,
        "import socket; socket.create_connection("
        f"('sink.{PROBE_NAMESPACE}.svc.cluster.local',18080),timeout=2).close()",
        expect_success=False,
    )
    print(
        "blocked_unapproved_egress=" + (egress_result or "connection timed out/failed as required")
    )
    service_ip = kubectl(
        "-n", NAMESPACE, "get", "service", "gateway", "-o", "jsonpath={.spec.clusterIP}"
    )
    probe_result = subprocess.run(
        [
            "kubectl",
            "-n",
            PROBE_NAMESPACE,
            "exec",
            "sink",
            "--",
            "python",
            "-c",
            f"import socket; socket.create_connection(('{service_ip}',8000),timeout=2).close()",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if probe_result.returncode == 0:
        raise AssertionError("prohibited cross-namespace gateway ingress unexpectedly connected")
    print(
        "blocked_unapproved_ingress="
        + (probe_result.stderr.strip() or "connection timed out/failed as required")
    )
    pg_pod = kubectl(
        "-n",
        NAMESPACE,
        "get",
        "pods",
        "-l",
        "app.kubernetes.io/name=postgres",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )
    pg_probe = subprocess.run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "exec",
            pg_pod,
            "--",
            "wget",
            "-T",
            "2",
            "-qO-",
            f"http://gateway.{NAMESPACE}.svc.cluster.local:8000/health",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if pg_probe.returncode == 0:
        raise AssertionError("PostgreSQL pod unexpectedly initiated a gateway connection")
    print(
        "blocked_postgres_to_gateway="
        + (pg_probe.stderr.strip() or "connection timed out/failed as required")
    )
    print("networkpolicy_permitted_and_prohibited_traffic=passed cni=calico-v3.33.0")


def main() -> int:
    for command in ("kubectl", "openssl", "curl"):
        if shutil.which(command) is None:
            raise RuntimeError(f"required command not found: {command}")
    context = kubectl("config", "current-context")
    if not context.startswith("kind-mcpcodeaway-k21"):
        raise RuntimeError("select the dedicated kind-mcpcodeaway-k21 context")
    if kubectl("get", "namespace", INGRESS_NAMESPACE, check=False):
        raise RuntimeError(
            f"namespace {INGRESS_NAMESPACE} already exists; refusing cleanup ownership"
        )
    if kubectl("get", "namespace", PROBE_NAMESPACE, check=False):
        raise RuntimeError(
            f"namespace {PROBE_NAMESPACE} already exists; refusing cleanup ownership"
        )
    for kind, name in (
        ("ingress", "gateway"),
        ("secret", "gateway-tls"),
        ("networkpolicy", "gateway-restricted-traffic"),
    ):
        if kubectl("-n", NAMESPACE, "get", kind, name, check=False):
            raise RuntimeError(f"{kind}/{name} already exists; refusing to overwrite it")
    ingress_namespace_created = False
    probe_namespace_created = False
    with tempfile.TemporaryDirectory(prefix="mcpcodeaway-k21-ingress-") as temp:
        directory = Path(temp)
        ca_key, ca_file = directory / "ca.key", directory / "ca.crt"
        bad_key, bad_ca = directory / "bad-ca.key", directory / "bad-ca.crt"
        key, csr, cert, extensions = (
            directory / "server.key",
            directory / "server.csr",
            directory / "server.crt",
            directory / "server.ext",
        )
        extensions.write_text(
            f"subjectAltName=DNS:{HOST}\nextendedKeyUsage=serverAuth\n", encoding="utf-8"
        )
        commands = [
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(ca_key),
                "-out",
                str(ca_file),
                "-days",
                "2",
                "-subj",
                "/CN=MCPcodeaway K2.1 ingress CA",
            ],
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(bad_key),
                "-out",
                str(bad_ca),
                "-days",
                "2",
                "-subj",
                "/CN=MCPcodeaway K2.1 untrusted CA",
            ],
            [
                "openssl",
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(csr),
                "-subj",
                f"/CN={HOST}",
            ],
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(ca_file),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-out",
                str(cert),
                "-days",
                "2",
                "-sha256",
                "-extfile",
                str(extensions),
            ],
        ]
        for command in commands:
            subprocess.run(command, check=True, capture_output=True, timeout=30)
        try:
            kubectl("create", "namespace", INGRESS_NAMESPACE)
            ingress_namespace_created = True
            verify_ingress(ca_file, bad_ca, cert, key)
            kubectl("create", "namespace", PROBE_NAMESPACE)
            probe_namespace_created = True
            verify_policy()
        finally:
            kubectl(
                "-n",
                NAMESPACE,
                "delete",
                "ingress",
                "gateway",
                "--ignore-not-found=true",
                check=False,
            )
            kubectl(
                "-n",
                NAMESPACE,
                "delete",
                "secret",
                "gateway-tls",
                "--ignore-not-found=true",
                check=False,
            )
            kubectl(
                "-n",
                NAMESPACE,
                "delete",
                "networkpolicy",
                "gateway-restricted-traffic",
                "--ignore-not-found=true",
                check=False,
            )
            if probe_namespace_created:
                kubectl(
                    "delete",
                    "namespace",
                    PROBE_NAMESPACE,
                    "--ignore-not-found=true",
                    "--wait=false",
                    check=False,
                )
            if ingress_namespace_created:
                kubectl(
                    "delete",
                    "namespace",
                    INGRESS_NAMESPACE,
                    "--ignore-not-found=true",
                    "--wait=false",
                    check=False,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
