"""Run focused local acceptance checks with optional disposable PostgreSQL."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "docker-compose.acceptance.yml"
PROJECT = "mcpcodeaway-local-acceptance"
TESTS = [
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-postgres", action="store_true")
    parser.add_argument("--with-postgres-processes", action="store_true")
    parser.add_argument("--with-process-crash", action="store_true")
    parser.add_argument("--with-tcp-tls", action="store_true")
    args = parser.parse_args()
    env = os.environ.copy()
    use_postgres = args.with_postgres or args.with_postgres_processes or args.with_process_crash

    if use_postgres:
        if shutil.which("docker") is None:
            parser.error("Docker is required for PostgreSQL acceptance options")

    try:
        if use_postgres:
            run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(COMPOSE_FILE),
                    "-p",
                    PROJECT,
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "60",
                    "postgres",
                ]
            )
            port = env.get("MCP_TEST_POSTGRES_PORT", "55432")
            env.setdefault(
                "MCP_TEST_POSTGRES_DSN",
                f"postgresql://gateway_test:local_acceptance_only@127.0.0.1:{port}/mcpcodeaway_acceptance",
            )
            env["MCP_TEST_POSTGRES_COMPOSE_PROJECT"] = PROJECT
            if args.with_postgres_processes or args.with_process_crash:
                env["MCP_TEST_POSTGRES_DSN"] = (
                    f"postgresql://gateway_test:local_acceptance_only@127.0.0.1:"
                    f"{port}/mcpcodeaway_acceptance"
                )
        command = ["uv", "run"]
        if use_postgres:
            command.extend(["--extra", "postgres"])
        command.extend(["pytest", "-q", *TESTS])
        if args.with_tcp_tls:
            command.append("tests/test_tcp_tls_acceptance.py")
            env["MCP_RUN_TCP_TLS_TESTS"] = "1"
        if args.with_postgres_processes or args.with_process_crash:
            command.append("tests/test_postgres_process_acceptance.py")
            env["MCP_RUN_POSTGRES_PROCESS_TESTS"] = "1"
        if args.with_process_crash:
            command.append("tests/test_process_crash_acceptance.py")
            env["MCP_RUN_POSTGRES_CRASH_TESTS"] = "1"
        run(command, env=env)
    finally:
        if use_postgres:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(COMPOSE_FILE),
                    "-p",
                    PROJECT,
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ],
                cwd=ROOT,
                check=False,
                timeout=35,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
