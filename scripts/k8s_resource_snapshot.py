"""Sample cgroup CPU and memory for ready local Kubernetes gateway pods."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections.abc import Sequence


def kubectl(context: str, namespace: str, *args: str) -> str:
    result = subprocess.run(
        ["kubectl", "--context", context, "-n", namespace, *args],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def parse_cpu_usage(stats: str) -> int:
    for line in stats.splitlines():
        name, _, value = line.partition(" ")
        if name == "usage_usec":
            return int(value)
    raise ValueError("cgroup cpu.stat did not contain usage_usec")


def sample(
    context: str, namespace: str, interval_seconds: float, duration_seconds: float
) -> list[dict[str, int | str]]:
    initial: dict[str, tuple[int, float]] = {}
    memory_peaks: dict[str, int] = {}
    restart_counts: dict[str, int] = {}

    def running_pods() -> list[str]:
        return kubectl(
            context,
            namespace,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=gateway",
            "-o",
            "jsonpath={.items[?(@.status.phase=='Running')].metadata.name}",
        ).split()

    def observe(pod: str) -> tuple[int, int]:
        stats = kubectl(context, namespace, "exec", pod, "--", "cat", "/sys/fs/cgroup/cpu.stat")
        memory = int(
            kubectl(context, namespace, "exec", pod, "--", "cat", "/sys/fs/cgroup/memory.current")
        )
        return parse_cpu_usage(stats), memory

    deadline = time.monotonic() + duration_seconds
    while time.monotonic() < deadline:
        for pod in running_pods():
            try:
                cpu_usage, memory = observe(pod)
                restarts = int(
                    kubectl(
                        context,
                        namespace,
                        "get",
                        "pod",
                        pod,
                        "-o",
                        'jsonpath={.status.containerStatuses[?(@.name=="gateway")].restartCount}',
                    )
                    or "0"
                )
            except subprocess.CalledProcessError:
                continue
            if pod not in initial:
                initial[pod] = (cpu_usage, time.monotonic())
                memory_peaks[pod] = memory
            memory_peaks[pod] = max(memory_peaks[pod], memory)
            restart_counts[pod] = restarts
        time.sleep(min(interval_seconds, max(deadline - time.monotonic(), 0)))

    rows: list[dict[str, int | str]] = []
    for pod, (initial_usage, started_at) in initial.items():
        try:
            usage, memory = observe(pod)
        except subprocess.CalledProcessError:
            continue
        sampled_at = time.monotonic()
        memory_peaks[pod] = max(memory_peaks[pod], memory)
        elapsed = sampled_at - started_at
        usage_delta = max(usage - initial_usage, 0)
        rows.append(
            {
                "pod": pod,
                "interval_seconds": round(elapsed, 2),
                "average_cpu_millicores": round(usage_delta / (elapsed * 1000), 1),
                "memory_bytes": memory,
                "peak_memory_bytes": memory_peaks[pod],
                "container_restarts": restart_counts.get(pod, 0),
                "sample_duration_seconds": round(elapsed, 2),
            }
        )
    if not rows:
        raise RuntimeError("No running gateway pods were sampled")
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--duration", type=float, default=10.0)
    args = parser.parse_args(argv)
    if args.interval <= 0 or args.duration <= 0:
        parser.error("--interval and --duration must be positive")
    rows = sample(args.context, args.namespace, args.interval, args.duration)
    print(json.dumps({"sample_interval_seconds": args.interval, "pods": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
