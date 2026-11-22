"""Run focused local acceptance checks with optional disposable PostgreSQL."""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "docker-compose.acceptance.yml"
TLS_COMPOSE_FILE = ROOT / "docker-compose.postgres-tls.yml"
TESTS = [
    "tests/test_postgres_migrations.py",
    "tests/test_crypto_secrets.py",
    "tests/test_gateway_secrets_audit.py",
    "tests/test_user_connect.py",
    "tests/test_auth_obo.py",
    "tests/test_mock_oidc_provider.py",
    "tests/test_oidc_golden_flows.py",
    "tests/test_security_hardening.py",
    "tests/test_sensitive_write_audit.py",
    "tests/test_gateway_modes_e2e.py",
    "tests/test_adaptive_execution.py",
    "tests/test_upstream_recovery_golden.py",
    "tests/test_recovery_lab.py",
    "tests/test_postgres_acceptance.py",
]


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def create_postgres_tls_certificates(directory: Path) -> tuple[Path, Path]:
    ca_key = directory / "ca.key"
    ca_cert = directory / "ca.crt"
    server_key = directory / "server.key"
    server_csr = directory / "server.csr"
    server_cert = directory / "server.crt"
    extensions = directory / "server.ext"
    untrusted_ca_key = directory / "untrusted-ca.key"
    untrusted_ca_cert = directory / "untrusted-ca.crt"
    extensions.write_text(
        "subjectAltName=IP:127.0.0.1,DNS:postgres-tls,"
        "DNS:postgres-tls.mcpcodeaway-k1.svc.cluster.local\n"
        "extendedKeyUsage=serverAuth\n",
        encoding="utf-8",
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
            str(untrusted_ca_key),
            "-out",
            str(untrusted_ca_cert),
            "-days",
            "2",
            "-subj",
            "/CN=MCPcodeaway K2.1 untrusted test CA",
        ],
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
            str(ca_cert),
            "-days",
            "2",
            "-subj",
            "/CN=MCPcodeaway K2.1 local acceptance CA",
        ],
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(server_key),
            "-out",
            str(server_csr),
            "-subj",
            "/CN=127.0.0.1",
        ],
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(server_csr),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(server_cert),
            "-days",
            "2",
            "-sha256",
            "-extfile",
            str(extensions),
        ],
    ]
    for command in commands:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    return ca_cert, untrusted_ca_cert


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-postgres", action="store_true")
    parser.add_argument("--with-postgres-tls", action="store_true")
    parser.add_argument("--with-postgres-processes", action="store_true")
    parser.add_argument("--with-process-crash", action="store_true")
    parser.add_argument("--with-tcp-tls", action="store_true")
    args = parser.parse_args()
    env = os.environ.copy()
    project = f"mcpcodeaway-acceptance-{secrets.token_hex(8)}"
    use_postgres = (
        args.with_postgres
        or args.with_postgres_tls
        or args.with_postgres_processes
        or args.with_process_crash
    )
    tls_directory: Path | None = None
    compose_files = [COMPOSE_FILE]

    if use_postgres:
        env.setdefault("MCP_TEST_POSTGRES_PASSWORD", secrets.token_urlsafe(32))
        port = env.get("MCP_TEST_POSTGRES_PORT", "55432")
        if shutil.which("docker") is None:
            parser.error("Docker is required for PostgreSQL acceptance options")
        if args.with_postgres_tls:
            if shutil.which("openssl") is None:
                parser.error("OpenSSL is required for PostgreSQL TLS acceptance")
            tls_directory = Path(tempfile.mkdtemp(prefix="mcpcodeaway-postgres-tls-"))
            tls_directory.chmod(0o755)
            ca_file, untrusted_ca_file = create_postgres_tls_certificates(tls_directory)
            compose_files.append(TLS_COMPOSE_FILE)
            env["MCP_TEST_POSTGRES_CERT_DIR"] = str(tls_directory)
            env["MCP_TEST_POSTGRES_CA_FILE"] = str(ca_file)
            env["MCP_TEST_POSTGRES_UNTRUSTED_CA_FILE"] = str(untrusted_ca_file)
            tls_query = urlencode({"sslmode": "verify-full", "sslrootcert": str(ca_file)})
        else:
            tls_query = "sslmode=disable"
        database_auth = quote(env["MCP_TEST_POSTGRES_PASSWORD"], safe="")
        env["MCP_TEST_POSTGRES_MIGRATION_DSN"] = (
            f"postgresql://gateway_test:{database_auth}@"
            f"127.0.0.1:{port}/mcpcodeaway_acceptance?{tls_query}"
        )
        env["MCP_TEST_POSTGRES_BOOTSTRAP_DSN"] = (
            f"postgresql://acceptance_bootstrap:{database_auth}@"
            f"127.0.0.1:{port}/mcpcodeaway_acceptance?{tls_query}"
        )
        env["MCP_TEST_POSTGRES_DSN"] = (
            f"postgresql://gateway_limited:{database_auth}@"
            f"127.0.0.1:{port}/mcpcodeaway_acceptance?{tls_query}"
        )
        env["MCP_TEST_POSTGRES_COMPOSE_PROJECT"] = project
        env["MCP_TEST_POSTGRES_COMPOSE_FILES"] = os.pathsep.join(map(str, compose_files))

    try:
        if use_postgres:
            compose_command = ["docker", "compose"]
            for compose_file in compose_files:
                compose_command.extend(["-f", str(compose_file)])
            compose_command.extend(
                [
                    "-p",
                    project,
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "60",
                    "postgres",
                ]
            )
            result = subprocess.run(compose_command, cwd=ROOT, env=env, check=False)
            if result.returncode:
                logs_command = compose_command[: compose_command.index("up")] + [
                    "logs",
                    "--no-color",
                    "postgres",
                ]
                subprocess.run(logs_command, cwd=ROOT, env=env, check=False)
                raise subprocess.CalledProcessError(result.returncode, compose_command)
            migration_env = env.copy()
            migration_env["MCP_POSTGRES_MIGRATION_DSN"] = env["MCP_TEST_POSTGRES_MIGRATION_DSN"]
            run(
                [
                    "uv",
                    "run",
                    "--extra",
                    "postgres",
                    "python",
                    "-m",
                    "gateway.migrations",
                    "upgrade",
                ],
                env=migration_env,
            )
            runtime_setup_env = env.copy()
            runtime_setup_env["MCP_POSTGRES_MIGRATION_DSN"] = env["MCP_TEST_POSTGRES_MIGRATION_DSN"]
            run(
                [
                    "uv",
                    "run",
                    "--extra",
                    "postgres",
                    "python",
                    "scripts/grant_postgres_runtime.py",
                ],
                env=runtime_setup_env,
            )
        command = ["uv", "run"]
        if use_postgres:
            command.extend(["--extra", "postgres"])
        command.extend(["pytest", "-q", "-rs", *TESTS])
        if args.with_postgres_tls:
            command.append("tests/test_postgres_tls_acceptance.py")
        if args.with_tcp_tls:
            command.append("tests/test_tcp_tls_acceptance.py")
            env["MCP_RUN_TCP_TLS_TESTS"] = "1"
        if args.with_postgres_processes or args.with_process_crash:
            command.append("tests/test_postgres_process_acceptance.py")
            env["MCP_RUN_POSTGRES_PROCESS_TESTS"] = "1"
        if args.with_process_crash:
            env["MCP_RUN_POSTGRES_CRASH_TESTS"] = "1"
        run(command, env=env)
        if args.with_process_crash:
            crash_env = env.copy()
            crash_env["MCP_TEST_POSTGRES_DSN"] = env["MCP_TEST_POSTGRES_MIGRATION_DSN"]
            run(
                [
                    "uv",
                    "run",
                    "--extra",
                    "postgres",
                    "pytest",
                    "-q",
                    "-rs",
                    "tests/test_process_crash_acceptance.py",
                ],
                env=crash_env,
            )
    finally:
        if use_postgres:
            cleanup_command = ["docker", "compose"]
            for compose_file in compose_files:
                cleanup_command.extend(["-f", str(compose_file)])
            cleanup_command.extend(["-p", project, "down", "--volumes", "--remove-orphans"])
            subprocess.run(
                cleanup_command,
                cwd=ROOT,
                env=env,
                check=False,
                timeout=35,
            )
        if tls_directory is not None:
            shutil.rmtree(tls_directory, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
