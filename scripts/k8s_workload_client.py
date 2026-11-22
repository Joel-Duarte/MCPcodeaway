"""Workload generator sent to the opt-in K2.1 Kubernetes client pod."""

import concurrent.futures
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

if "cfg" not in globals():
    cfg = {}

BASE = "http://gateway.mcpcodeaway-k1.svc.cluster.local:8000"


def request(path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    method = "GET" if data is None else "POST"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw, status = response.read(), response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    elapsed = (time.perf_counter() - started) * 1000
    try:
        return status, json.loads(raw), elapsed
    except ValueError:
        return status, {}, elapsed


token_body = {
    "persona": "gateway_admin",
    "roles": ["gateway_admin", "platform_admin", "admin"],
    "scopes": ["mcp.tools", "mcp.admin", "mcp.audit", "mcp.write"],
}
status, auth, _ = request("/dev/auth/token", token_body, {"Content-Type": "application/json"})
if status != 200:
    raise SystemExit("token issue failed")
token = auth["access_token"]
execution = cfg["mode"]


def mcp(
    method,
    *,
    name=None,
    arguments=None,
    request_id=None,
    request_state=None,
    responses=None,
    elicitation=False,
):
    path = "/mcp?execution=" + urllib.parse.quote(execution)
    elicitation_capabilities = {"form": {}} if elicitation else {}
    metadata = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {"elicitation": elicitation_capabilities},
        "io.modelcontextprotocol/clientInfo": {"name": "k21-workload", "version": "1"},
    }
    params = {"_meta": metadata, "name": name, "arguments": arguments or {}}
    if request_state is not None:
        params["requestState"] = request_state
    if responses is not None:
        params["inputResponses"] = responses
    payload = {"jsonrpc": "2.0", "id": request_id or 1, "method": method, "params": params}
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }
    if name:
        headers["Mcp-Name"] = name
    if request_id:
        headers["X-Request-ID"] = request_id
    return request(path, payload, headers)


def percentile(samples, p):
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))
    return round(ordered[index], 3) if ordered else 0.0


started = time.perf_counter()
scenario = cfg["scenario"]
error_samples = []
if scenario == "write":
    operations = max(10, cfg["requests"] // 20)
    samples, successes = [], 0
    error_categories = {}
    fixture_keys = []
    for index in range(operations):
        key = f"k21-baseline-{time.time_ns()}-{index}"
        fixture_keys.append(key)
        arguments = {
            "idempotency_key": key,
            "resource_id": "local/k21-baseline",
            "change": "sizing acceptance write",
        }
        correlation = "baseline-correlation-" + key
        call_started = time.perf_counter()
        status, prompted, _ = mcp(
            "tools/call",
            name="recovery_workbench__submit_change",
            arguments=arguments,
            request_id=correlation,
            elicitation=True,
        )
        prompt = prompted.get("result", {})
        ok = status == 200 and prompt.get("resultType") == "input_required"
        if ok:
            response = {"confirm": {"action": "accept", "content": {"confirm": True}}}
            status, accepted, _ = mcp(
                "tools/call",
                name="recovery_workbench__submit_change",
                arguments=arguments,
                request_id=correlation,
                request_state=prompt.get("requestState"),
                responses=response,
                elicitation=True,
            )
            ok = status == 200 and "error" not in accepted
        samples.append((time.perf_counter() - call_started) * 1000)
        successes += int(ok)
        if not ok:
            error_categories[str(status)] = error_categories.get(str(status), 0) + 1
    request_count = operations * 2
    operation_count = operations
elif scenario == "discovery":
    samples, successes = [], 0
    error_categories = {}
    for index in range(cfg["requests"]):
        status, body, elapsed = mcp("tools/list", request_id="discover-" + str(index))
        samples.append(elapsed)
        result = body.get("result", {})
        ok = status == 200 and bool(result.get("tools")) and "error" not in body
        successes += int(ok)
        if not ok:
            category = str(status) + ("/rpc-error" if "error" in body else "")
            error_categories[category] = error_categories.get(category, 0) + 1
    request_count = len(samples)
    operation_count = request_count
else:
    error_categories = {}

    def invoke_read(index):
        key = cfg["read_key"] or "baseline-read-" + execution + "-" + str(index)
        correlation = "baseline-correlation-" + execution + "-" + str(index)
        if execution == "classic":
            status, body, elapsed = mcp(
                "tools/call",
                name="recovery_workbench__get_change_status",
                arguments={"idempotency_key": key},
                request_id=correlation,
            )
        else:
            code = f"await recovery_workbench__get_change_status(idempotency_key='{key}')"
            status, body, elapsed = mcp(
                "tools/call",
                name="execute_code",
                arguments={"code": code},
                request_id=correlation,
            )
        result = body.get("result", {})
        ok = status == 200 and "error" not in body and not result.get("isError", False)
        category = str(status)
        if "error" in body:
            category += "/rpc-error"
        elif result.get("isError", False):
            category += "/tool-error"
        details = body.get("result", {}).get("content", [])
        return elapsed, ok, category, json.dumps(details)[:500]

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["concurrency"]) as pool:
        measured = list(pool.map(invoke_read, range(cfg["requests"])))
    samples = [row[0] for row in measured]
    successes = sum(row[1] for row in measured)
    for row in measured:
        if not row[1]:
            error_categories[row[2]] = error_categories.get(row[2], 0) + 1
            if len(error_samples) < 3:
                error_samples.append(row[3])
    request_count = len(samples)
    operation_count = request_count

elapsed = max(time.perf_counter() - started, 0.000001)
metrics = {
    "request_count": request_count,
    "successful_operations": successes,
    "operation_count": operation_count,
    "error_count": max(operation_count - successes, 0),
    "error_categories": error_categories,
    "error_samples": error_samples if scenario != "write" else [],
    "duration_seconds": round(elapsed, 3),
    "throughput_per_second": round(successes / elapsed, 3),
    "latency_ms": {
        "p50": percentile(samples, 0.50),
        "p95": percentile(samples, 0.95),
        "p99": percentile(samples, 0.99),
    },
}
if scenario == "write":
    metrics["fixture_keys"] = fixture_keys
print(json.dumps({"scenario": scenario, "mode": execution, "metrics": metrics}))
