"""Measure representative K1 gateway traffic and cgroup usage in an opt-in kind cluster."""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CLIENT = "mcpcodeaway-k21-baseline-client"
SNAPSHOT_PATH = ROOT / "scripts" / "k8s_resource_snapshot.py"
SNAPSHOT_SPEC = importlib.util.spec_from_file_location("k8s_resource_snapshot", SNAPSHOT_PATH)
if SNAPSHOT_SPEC is None or SNAPSHOT_SPEC.loader is None:
    raise RuntimeError(f"Could not load resource sampler from {SNAPSHOT_PATH}")
SNAPSHOT_MODULE = importlib.util.module_from_spec(SNAPSHOT_SPEC)
sys.modules[SNAPSHOT_SPEC.name] = SNAPSHOT_MODULE
SNAPSHOT_SPEC.loader.exec_module(SNAPSHOT_MODULE)
sample_cgroups = SNAPSHOT_MODULE.sample


def kubectl(
    context: str,
    namespace: str,
    *args: str,
    input_text: str | None = None,
    check: bool = True,
) -> str:
    result = subprocess.run(
        ["kubectl", "--context", context, "-n", namespace, *args],
        cwd=ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if check and result.returncode:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def workload_source(
    scenario: str,
    mode: str,
    requests: int,
    concurrency: int,
    read_key: str | None = None,
) -> str:
    config = json.dumps(
        {
            "scenario": scenario,
            "mode": mode,
            "requests": requests,
            "concurrency": concurrency,
            "read_key": read_key,
        }
    )
    return (
        "import json\ncfg = json.loads("
        + repr(config)
        + ")\n"
        + (ROOT / "scripts" / "k8s_workload_client.py").read_text()
    )


def run_client(context: str, namespace: str, source: str) -> dict[str, Any]:
    result = kubectl(
        context, namespace, "exec", "-i", CLIENT, "--", "python", "-", input_text=source
    )
    return json.loads(result.splitlines()[-1])


def measured(
    context: str,
    namespace: str,
    scenario: str,
    mode: str,
    requests: int,
    concurrency: int,
    duration: float,
    read_key: str | None = None,
) -> dict[str, Any]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        metrics = pool.submit(
            run_client,
            context,
            namespace,
            workload_source(scenario, mode, requests, concurrency, read_key),
        )
        cgroups = pool.submit(sample_cgroups, context, namespace, 1.0, duration)
        result = metrics.result(timeout=300)
        result["cgroups"] = cgroups.result(timeout=duration + 90)
    return result


def reset_gateway_rate_window(context: str, namespace: str) -> None:
    kubectl(context, namespace, "rollout", "restart", "deployment/gateway")
    kubectl(
        context,
        namespace,
        "rollout",
        "status",
        "deployment/gateway",
        "--timeout=240s",
    )


def postgres_latency(context: str, namespace: str, requests: int) -> dict[str, Any]:
    source = f"""import asyncio, json, math, os, time, asyncpg
async def main():
 c = await asyncpg.connect(os.environ["GATEWAY_POSTGRES_DSN"])
 samples = []
 for _ in range({requests}):
  t = time.perf_counter()
  await c.fetchval("SELECT 1")
  samples.append((time.perf_counter()-t)*1000)
 count = await c.fetchval("SELECT count(*) FROM pg_stat_activity WHERE usename=current_user")
 await c.close(); ordered = sorted(samples)
 def p(q): return round(ordered[max(0,min(len(ordered)-1,math.ceil(q*len(ordered))-1))],3)
 print(json.dumps({{"request_count":len(samples),"successful_operations":len(samples),"error_count":0,"latency_ms":{{"p50":p(.5),"p95":p(.95),"p99":p(.99)}},"postgres_connections_including_probe":count}}))
asyncio.run(main())
""".replace("\n+", "\n")
    pod = kubectl(
        context,
        namespace,
        "get",
        "pods",
        "-l",
        "app.kubernetes.io/name=gateway",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )
    result = subprocess.run(
        ["kubectl", "--context", context, "-n", namespace, "exec", "-i", pod, "--", "python", "-"],
        cwd=ROOT,
        input=source,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout.splitlines()[-1])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--duration", type=float, default=6)
    parser.add_argument("--output", default=".k8s-local/k21-workload-baseline.json")
    args = parser.parse_args(argv)
    if args.requests < 100 or args.concurrency < 1 or args.duration < 1:
        parser.error("requests must be at least 100; concurrency and duration must be positive")
    if kubectl(args.context, args.namespace, "get", "pod", CLIENT, check=False):
        raise RuntimeError(f"pod/{CLIENT} already exists; refusing to overwrite it")
    deployment = json.loads(
        kubectl(args.context, args.namespace, "get", "deployment", "gateway", "-o", "json")
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    results: dict[str, Any] = {
        "image": container["image"],
        "replica_count": deployment["spec"].get("replicas", 1),
        "resource_requests_and_limits": container.get("resources", {}),
        "test_configuration": {
            "requests_per_read_or_discovery_mode": args.requests,
            "concurrency": args.concurrency,
            "sensitive_write_operations": max(10, args.requests // 20),
            "cgroup_sample_duration_seconds": args.duration,
            "upstream_fixture": "deterministic local recovery_workbench mock",
        },
        "workloads": {},
    }
    kubectl(
        args.context,
        args.namespace,
        "run",
        CLIENT,
        "--image=python:3.13-alpine",
        "--restart=Never",
        "--command",
        "--",
        "sleep",
        "1800",
    )
    try:
        kubectl(
            args.context,
            args.namespace,
            "wait",
            "--for=condition=Ready",
            f"pod/{CLIENT}",
            "--timeout=180s",
        )
        idle_start = time.monotonic()
        results["workloads"]["idle"] = {
            "cgroups": sample_cgroups(args.context, args.namespace, 1.0, args.duration),
            "observed_seconds": round(time.monotonic() - idle_start, 3),
        }
        startup_started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            startup_sample = pool.submit(
                sample_cgroups, args.context, args.namespace, 1.0, args.duration
            )
            kubectl(args.context, args.namespace, "rollout", "restart", "deployment/gateway")
            kubectl(
                args.context,
                args.namespace,
                "rollout",
                "status",
                "deployment/gateway",
                "--timeout=240s",
            )
            results["workloads"]["startup_and_initialization"] = {
                "rollout_seconds": round(time.monotonic() - startup_started, 3),
                "cgroups_during_rollout": startup_sample.result(timeout=args.duration + 90),
            }
        write_result = measured(
            args.context, args.namespace, "write", "classic", args.requests, 1, args.duration
        )
        results["workloads"]["sensitive_writes_with_journal"] = write_result
        fixture_keys = write_result["metrics"].get("fixture_keys", [])
        if not fixture_keys:
            raise RuntimeError("Sensitive-write workload did not produce a read fixture")
        write_result["metrics"].pop("fixture_keys", None)
        read_key = fixture_keys[0]
        results["workloads"]["discovery"] = {
            mode: measured(
                args.context,
                args.namespace,
                "discovery",
                mode,
                args.requests,
                args.concurrency,
                args.duration,
            )
            for mode in ("classic", "codemode", "hybrid", "auto")
        }
        read_workloads = {}
        for mode in ("classic", "codemode", "hybrid", "auto"):
            reset_gateway_rate_window(args.context, args.namespace)
            read_workloads[mode] = measured(
                args.context,
                args.namespace,
                "read",
                mode,
                args.requests,
                args.concurrency,
                args.duration,
                read_key,
            )
        results["workloads"]["read_only_invocation"] = read_workloads
        reset_gateway_rate_window(args.context, args.namespace)
        results["workloads"]["concurrent_read_only"] = measured(
            args.context,
            args.namespace,
            "read",
            "classic",
            args.requests,
            args.concurrency,
            args.duration,
            read_key,
        )
        results["workloads"]["postgres_select_latency"] = postgres_latency(
            args.context, args.namespace, args.requests
        )
        pod_items = json.loads(
            kubectl(
                args.context,
                args.namespace,
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/name=gateway",
                "-o",
                "json",
            )
        )["items"]
        results["pod_status"] = [
            {
                "name": item["metadata"]["name"],
                "phase": item["status"].get("phase"),
                "images": [
                    {
                        "name": status.get("name"),
                        "image": status.get("image"),
                        "image_id": status.get("imageID"),
                    }
                    for status in item["status"].get("containerStatuses", [])
                ],
                "restart_count": sum(
                    status.get("restartCount", 0)
                    for status in item["status"].get("containerStatuses", [])
                ),
                "last_terminated_reason": [
                    status.get("lastState", {}).get("terminated", {}).get("reason")
                    for status in item["status"].get("containerStatuses", [])
                    if status.get("lastState", {}).get("terminated")
                ],
            }
            for item in pod_items
        ]
        report = json.dumps(results, indent=2)
        output_path = ROOT / args.output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report + "\n", encoding="utf-8")
        print(report)
        print(f"baseline_report={output_path}")
        return 0
    finally:
        kubectl(
            args.context,
            args.namespace,
            "delete",
            "pod",
            CLIENT,
            "--ignore-not-found=true",
            "--grace-period=0",
            "--force",
            "--wait=false",
            check=False,
        )


if __name__ == "__main__":
    raise SystemExit(main())
