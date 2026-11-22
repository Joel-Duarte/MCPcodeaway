#!/usr/bin/env python3
"""Opt-in ClusterIP and lifecycle checks for the local K1 kind environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = "mcpcodeaway-k1-acceptance-client"


def kubectl(*args: str, check: bool = True, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["kubectl", *args],
        cwd=ROOT,
        text=True,
        input=input_text,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if check and result.returncode:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def client_source() -> str:
    return r"""import json, time, urllib.error, urllib.parse, urllib.request
BASE = "http://gateway.mcpcodeaway-k1.svc.cluster.local:8000"
FIXTURE = "http://mock-backends.mcpcodeaway-k1.svc.cluster.local:8001"
run_tag = str(time.time_ns())
instances = set()

def request(path, *, method="GET", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read()
            instance = response.headers.get("X-Gateway-Instance")
            if instance:
                instances.add(instance)
            return response.status, json.loads(raw) if raw else {}, instance
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        instance = exc.headers.get("X-Gateway-Instance")
        if instance:
            instances.add(instance)
        try: payload = json.loads(raw)
        except Exception: payload = {"detail": raw.decode(errors="replace")}
        return exc.code, payload, instance

def check(ok, message):
    if not ok: raise AssertionError(message)

for _ in range(80):
    status, body, _ = request("/health")
    check(status == 200 and body.get("status") == "ok", "health endpoint failed")
    if len(instances) >= 2: break
check(len(instances) >= 2, "ClusterIP did not reach two distinct gateway pods")
print("service_pods=" + ",".join(sorted(instances)))

status, token_body, _ = request("/dev/auth/token", method="POST",
    body={"persona":"gateway_admin", "roles":["gateway_admin","platform_admin","admin"],
          "scopes":["mcp.tools","mcp.admin","mcp.audit","mcp.write"]},
    headers={"Content-Type":"application/json"})
check(status == 200 and token_body.get("access_token"), "shared dev OIDC token issuance failed")
token = token_body["access_token"]

def mcp(method, *, name=None, arguments=None, execution=None, request_id=None,
        request_state=None, input_responses=None, token_value=token, capabilities=None):
    path = "/mcp" + ("?execution=" + urllib.parse.quote(execution) if execution else "")
    params = {"_meta": {
        "io.modelcontextprotocol/protocolVersion":"2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": capabilities or {},
        "io.modelcontextprotocol/clientInfo":{"name":"k1-acceptance","version":"1"}},
        "name":name, "arguments":arguments or {}}
    if request_state is not None: params["requestState"] = request_state
    if input_responses is not None: params["inputResponses"] = input_responses
    payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
    headers = {"Authorization":"Bearer " + token_value,
        "Content-Type":"application/json","Accept":"application/json, text/event-stream",
        "MCP-Protocol-Version":"2026-07-28","Mcp-Method":method}
    if name: headers["Mcp-Name"] = name
    if request_id: headers["X-Request-ID"] = request_id
    status, body, instance = request(path, method="POST", body=payload, headers=headers)
    return status, body, instance

# Discovery and invocation use each existing execution mode; this does not alter mode policy.
service_pods = set(instances)
for mode in ("classic", "codemode", "hybrid", "auto"):
    discovery_pods = set()
    for _ in range(80):
        status, body, pod = mcp("tools/list", execution=mode)
        check(status == 200 and "result" in body,
            "tools/list failed in " + mode + ": " + str(body))
        check(bool(body["result"].get("tools", [])), "discovery returned no tools in " + mode)
        if pod:
            discovery_pods.add(pod)
        if service_pods.issubset(discovery_pods):
            break
    check(service_pods.issubset(discovery_pods),
        "discovery in " + mode + " did not reach both service pods")
    denied_status, _, _ = mcp("tools/list", execution=mode, token_value="invalid-token")
    check(denied_status in (401, 403), "invalid caller was not rejected in " + mode)
    smoke_key = "k1-mode-smoke-" + mode
    invocation_pods = set()
    for _ in range(80):
        if mode in {"codemode", "hybrid", "auto"}:
            code = f"await recovery_workbench__get_change_status(idempotency_key='{smoke_key}')"
            status, invoked, pod = mcp(
                "tools/call", name="execute_code", arguments={"code": code}, execution=mode
            )
        else:
            status, invoked, pod = mcp(
                "tools/call", name="recovery_workbench__get_change_status",
                arguments={"idempotency_key": smoke_key}, execution=mode
            )
        check(status == 200 and not invoked.get("error"),
            "read-only invocation failed in " + mode + ": " + str(invoked))
        if pod:
            invocation_pods.add(pod)
        if service_pods.issubset(invocation_pods):
            break
    check(service_pods.issubset(invocation_pods),
        "read-only invocation in " + mode + " did not reach both service pods")
print("execution_modes=classic,codemode,hybrid,auto discovery_and_invocation=passed")

# The protected write prompts first. Its accepted request is routed through ClusterIP, then
# the exact correlated retry is sent until it reaches a different pod.
tool = "recovery_workbench__submit_change"
args = {"idempotency_key":"k1-cross-replica-acceptance",
        "resource_id":"local/k1-acceptance","change":"acceptance side effect"}
capabilities = {"elicitation":{"form":{}}}
correlation = "k1-sensitive-" + run_tag
args["idempotency_key"] = "k1-cross-replica-" + run_tag
status, prompted, prompt_pod = mcp("tools/call", name=tool, arguments=args,
    execution="classic", request_id=correlation, capabilities=capabilities)
check(status == 200 and prompted.get("result", {}).get("resultType") == "input_required",
      "protected write did not require requester confirmation: " + str(prompted))
check(prompt_pod, "prompt response did not identify its serving pod")
prompt_result = prompted["result"]
request_state = prompt_result["requestState"]
metrics_before = json.load(urllib.request.urlopen(FIXTURE + "/recovery/test/metrics", timeout=5))

# Auto mode must preserve both the write approval and its role/scope authorization gate.
auto_request_id = "k1-auto-write-" + run_tag
status, auto_prompt, _ = mcp("tools/call", name=tool, arguments=args,
    execution="auto", request_id=auto_request_id, capabilities=capabilities)
check(status == 200 and auto_prompt.get("result", {}).get("resultType") == "input_required",
      "Auto routing bypassed requester confirmation for a protected write")
status, readonly_token_body, _ = request("/dev/auth/token", method="POST",
    body={"persona":"readonly_auditor"}, headers={"Content-Type":"application/json"})
check(status == 200 and readonly_token_body.get("access_token"),
      "read-only caller token was unavailable")
status, auto_denial, _ = mcp("tools/call", name=tool, arguments=args,
    execution="auto", request_id="k1-auto-denied-" + run_tag,
    token_value=readonly_token_body["access_token"], capabilities=capabilities)
check(status in (200, 401, 403) and not (
    auto_denial.get("result", {}).get("resultType") == "input_required"
), "Auto routing changed read-only authorization into an approval prompt")
auto_metrics = json.load(urllib.request.urlopen(FIXTURE + "/recovery/test/metrics", timeout=5))
check(auto_metrics == metrics_before,
      "unauthorized Auto write reached the upstream or changed approval side effects")
print("auto_write_approval=required read_only_authorization=preserved")

status, result, accepted_pod = mcp("tools/call", name=tool, arguments=args,
    execution="classic", request_id=correlation, request_state=request_state,
    input_responses={"confirm":{"action":"accept","content":{"confirm":True}}},
    capabilities=capabilities)
check(status == 200 and not result.get("error"), "approved sensitive write failed: " + str(result))
check(accepted_pod, "approved write response did not identify its serving pod")
print("approval_prompt_pod=" + prompt_pod + " dispatched_write_pod=" + accepted_pod)

retry = None
for _ in range(80):
    retry = mcp("tools/call", name=tool, arguments=args, execution="classic",
        request_id=correlation, request_state=request_state,
        input_responses={"confirm":{"action":"accept","content":{"confirm":True}}},
        capabilities=capabilities)
    if retry[2] != accepted_pod: break
check(retry is not None and retry[2] != accepted_pod,
      "same-correlation retry did not reach the other replica")
check(retry[0] == 200 and retry[1].get("error"),
      "same-correlation retry was not rejected: " + str(retry[1]))

# A unique operation remains independent. A pending upstream outcome is durable and cannot
# be dispatched again after a retry reaches the other replica.
independent_correlation = "k1-independent-" + run_tag
independent = {**args,"idempotency_key":"k1-independent-" + run_tag,"simulate_pending":True}
status, independent_prompt, independent_pod = mcp("tools/call", name=tool,
    arguments=independent, execution="classic", request_id=independent_correlation,
    capabilities=capabilities)
check(status == 200 and independent_prompt.get("result", {}).get("resultType") == "input_required",
      "distinct correlation did not create an independent approval request")
status, pending, pending_pod = mcp("tools/call", name=tool,
    arguments=independent, execution="classic",
    request_id=independent_correlation, request_state=independent_prompt["result"]["requestState"],
    input_responses={"confirm":{"action":"accept","content":{"confirm":True}}},
    capabilities=capabilities)
check(status == 200, "pending-operation request transport failed")
blocked = None
for _ in range(80):
    blocked = mcp("tools/call", name=tool, arguments=independent,
        execution="classic", request_id=independent_correlation,
        request_state=independent_prompt["result"]["requestState"],
        input_responses={"confirm":{"action":"accept","content":{"confirm":True}}},
        capabilities=capabilities)
    if blocked[2] != pending_pod: break
check(blocked is not None and blocked[2] != pending_pod,
      "unresolved retry did not reach a different pod")
check(blocked[0] == 200 and blocked[1].get("error"),
      "unresolved operation was not blocked across replicas")

metrics_after = json.load(urllib.request.urlopen(FIXTURE + "/recovery/test/metrics", timeout=5))
check(metrics_after["commits"] - metrics_before["commits"] == 1,
      "expected exactly one upstream side effect for the accepted operation")
check(metrics_after["operations"] - metrics_before["operations"] == 2,
      "upstream did not record one completed and one unresolved operation")
with open("/tmp/k1-completed-write.json", "w") as state_file:
    json.dump({"correlation":correlation,"request_state":request_state,
        "arguments":args,"write_pod":accepted_pod,"metrics":metrics_after}, state_file)
print("upstream_commits_delta=1 retry_rejected=true unresolved_blocked=true")
print("run_tag=" + run_tag)
print("k8s_client_checks=passed")
"""


def wait_client_ready(context: str, namespace: str) -> None:
    kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "run",
        CLIENT,
        "--image=python:3.13-alpine",
        "--restart=Never",
        "--command",
        "--",
        "sleep",
        "1800",
    )
    kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "wait",
        "--for=condition=Ready",
        f"pod/{CLIENT}",
        "--timeout=180s",
    )


def run_client(context: str, namespace: str) -> str:
    output = kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "exec",
        "-i",
        CLIENT,
        "--",
        "python",
        "-",
        input_text=client_source(),
    )
    for line in output.splitlines():
        print(line)
    return output


def run_health_probe(context: str, namespace: str) -> None:
    source = r"""import urllib.request
url = "http://gateway.mcpcodeaway-k1.svc.cluster.local:8000/ready"
with urllib.request.urlopen(url, timeout=5) as response:
    if response.status != 200: raise SystemExit("service readiness failed")
    print("service_ready=" + response.headers.get("X-Gateway-Instance", "unknown"))
"""
    output = kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "exec",
        "-i",
        CLIENT,
        "--",
        "python",
        "-",
        input_text=source,
    )
    print(output)


def run_termination_retry(context: str, namespace: str, write_pod: str) -> None:
    source = r"""import json, urllib.error, urllib.request
BASE = "http://gateway.mcpcodeaway-k1.svc.cluster.local:8000"
FIXTURE = "http://mock-backends.mcpcodeaway-k1.svc.cluster.local:8001/recovery/test/metrics"
saved = json.load(open("/tmp/k1-completed-write.json", encoding="utf-8"))
def call(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, method="GET" if data is None else "POST",
        headers={} if data is None else {"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response), response.headers.get("X-Gateway-Instance")
token_data, _ = call("/dev/auth/token", {"persona":"gateway_admin",
    "roles":["gateway_admin","platform_admin","admin"],
    "scopes":["mcp.tools","mcp.admin","mcp.audit","mcp.write"]})
meta = {"io.modelcontextprotocol/protocolVersion":"2026-07-28",
    "io.modelcontextprotocol/clientCapabilities":{"elicitation":{"form":{}}},
    "io.modelcontextprotocol/clientInfo":{"name":"k1-termination-retry","version":"1"}}
params = {"_meta":meta,"name":"recovery_workbench__submit_change",
    "arguments":saved["arguments"],"requestState":saved["request_state"],
    "inputResponses":{"confirm":{"action":"accept","content":{"confirm":True}}}}
body = {"jsonrpc":"2.0","id":1,"method":"tools/call","params":params}
headers = {"Authorization":"Bearer " + token_data["access_token"],
    "Content-Type":"application/json","Accept":"application/json, text/event-stream",
    "MCP-Protocol-Version":"2026-07-28","Mcp-Method":"tools/call",
    "Mcp-Name":"recovery_workbench__submit_change",
    "X-Request-ID":saved["correlation"]}
req = urllib.request.Request(BASE + "/mcp?execution=classic", data=json.dumps(body).encode(),
    method="POST", headers=headers)
before = json.load(urllib.request.urlopen(FIXTURE, timeout=5))
try:
    response = urllib.request.urlopen(req, timeout=10)
    result = json.load(response)
    pod = response.headers.get("X-Gateway-Instance")
except urllib.error.HTTPError as error:
    result = json.loads(error.read())
    pod = error.headers.get("X-Gateway-Instance")
after = json.load(urllib.request.urlopen(FIXTURE, timeout=5))
if pod == saved["write_pod"]:
    raise AssertionError("retry did not reach a replacement/different pod")
if not result.get("error"): raise AssertionError("post-termination retry was not rejected")
if before != after or after != saved["metrics"]:
    raise AssertionError("upstream side-effect counters changed after terminated-pod retry")
print("termination_retry_pod=" + str(pod) + " duplicate_dispatch=false")
"""
    output = kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "exec",
        "-i",
        CLIENT,
        "--",
        "python",
        "-",
        input_text=source,
    )
    print(output)


def postgres_audit(context: str, namespace: str, run_tag: str) -> str:
    pod = kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        "app.kubernetes.io/name=postgres",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )
    return kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "exec",
        pod,
        "--",
        "psql",
        "-U",
        "gateway",
        "-d",
        "mcpcodeaway",
        "-At",
        "-F",
        "|",
        "-c",
        "SELECT correlation_id,status,count(*) FROM sensitive_write_audits "
        f"WHERE correlation_id IN ('k1-sensitive-{run_tag}', "
        f"'k1-independent-{run_tag}') GROUP BY correlation_id,status "
        "ORDER BY correlation_id,status",
    )


def executing_claim_check(context: str, namespace: str, run_tag: str) -> str:
    correlation = f"k1-executing-{run_tag}"
    arguments = {
        "idempotency_key": correlation,
        "resource_id": "local/k1-acceptance",
        "change": "executing claim acceptance",
    }
    arguments_json = json.dumps(arguments, separators=(",", ":"))
    prompt_source = f"""import json, urllib.request
BASE = "http://gateway.mcpcodeaway-k1.svc.cluster.local:8000"
arguments = json.loads({json.dumps(arguments_json)})
correlation = {json.dumps(correlation)}
token_request = urllib.request.Request(BASE + "/dev/auth/token",
    data=json.dumps({{"persona":"gateway_admin","roles":["gateway_admin","platform_admin","admin"],
        "scopes":["mcp.tools","mcp.admin","mcp.audit","mcp.write"]}}).encode(),
    headers={{"Content-Type":"application/json"}}, method="POST")
token = json.load(urllib.request.urlopen(token_request, timeout=10))["access_token"]
params = {{"_meta":{{"io.modelcontextprotocol/protocolVersion":"2026-07-28",
    "io.modelcontextprotocol/clientCapabilities":{{"elicitation":{{"form":{{}}}}}},
    "io.modelcontextprotocol/clientInfo":{{"name":"k1-executing-acceptance","version":"1"}}}},
    "name":"recovery_workbench__submit_change","arguments":arguments}}
request = urllib.request.Request(BASE + "/mcp?execution=classic",
    data=json.dumps({{"jsonrpc":"2.0","id":1,"method":"tools/call","params":params}}).encode(),
    headers={{"Authorization":"Bearer "+token,"Content-Type":"application/json",
        "Accept":"application/json, text/event-stream","MCP-Protocol-Version":"2026-07-28",
        "Mcp-Method":"tools/call","Mcp-Name":"recovery_workbench__submit_change",
        "X-Request-ID":correlation}}, method="POST")
with urllib.request.urlopen(request, timeout=15) as response:
    result=json.load(response)
    pod=response.headers.get("X-Gateway-Instance")
if result.get("result",{{}}).get("resultType") != "input_required":
    raise AssertionError("executing-claim write did not require approval: "+str(result))
print(json.dumps({{"pod":pod,"request_state":result["result"]["requestState"]}}))
"""
    prompt_output = kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "exec",
        "-i",
        CLIENT,
        "--",
        "python",
        "-",
        input_text=prompt_source,
    )
    prompt = json.loads(prompt_output)
    if not prompt.get("pod"):
        raise RuntimeError("executing-claim approval prompt did not identify its pod")

    postgres_pod = kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        "app.kubernetes.io/name=postgres",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )
    fields = kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "exec",
        postgres_pod,
        "--",
        "psql",
        "-U",
        "gateway",
        "-d",
        "mcpcodeaway",
        "-At",
        "-F",
        "|",
        "-c",
        "SELECT tenant_id,caller_sub,upstream_name,tool_name FROM sensitive_write_audits "
        f"WHERE correlation_id='k1-sensitive-{run_tag}' AND status='succeeded'",
    ).split("|")
    if len(fields) != 4:
        raise RuntimeError(f"could not find completed row to seed the executing claim: {fields}")
    tenant_id, caller_sub, upstream_name, tool_name = fields
    digest = hashlib.sha256(
        json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    dedupe_key = hashlib.sha256(
        "\0".join((tenant_id, caller_sub, correlation, upstream_name, tool_name, digest)).encode()
    ).hexdigest()

    def sql_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    values = [
        uuid.uuid4().hex,
        tenant_id,
        caller_sub,
        upstream_name,
        tool_name,
        digest,
        correlation,
        dedupe_key,
    ]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    insert = (
        "INSERT INTO sensitive_write_audits (operation_id,tenant_id,caller_sub,upstream_name,"
        "tool_name,request_digest,correlation_id,dedupe_key,status,created_at,updated_at) VALUES ("
        + ",".join(sql_literal(value) for value in values)
        + ", 'executing', "
        + sql_literal(now)
        + ", "
        + sql_literal(now)
        + ")"
    )
    kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "exec",
        postgres_pod,
        "--",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "gateway",
        "-d",
        "mcpcodeaway",
        "-c",
        insert,
    )

    retry_source = f"""import json, time, urllib.error, urllib.request
BASE = "http://gateway.mcpcodeaway-k1.svc.cluster.local:8000"
FIXTURE = "http://mock-backends.mcpcodeaway-k1.svc.cluster.local:8001"
arguments = json.loads({json.dumps(arguments_json)})
correlation = {json.dumps(correlation)}
request_state = json.loads({json.dumps(json.dumps(prompt["request_state"]))})
token_request = urllib.request.Request(BASE + "/dev/auth/token",
    data=json.dumps({{"persona":"gateway_admin","roles":["gateway_admin","platform_admin","admin"],
        "scopes":["mcp.tools","mcp.admin","mcp.audit","mcp.write"]}}).encode(),
    headers={{"Content-Type":"application/json"}}, method="POST")
token = json.load(urllib.request.urlopen(token_request, timeout=10))["access_token"]
def metrics():
    return json.load(urllib.request.urlopen(FIXTURE + "/recovery/test/metrics", timeout=5))
before = metrics()
last = None
for _ in range(80):
    params = {{"_meta":{{"io.modelcontextprotocol/protocolVersion":"2026-07-28",
        "io.modelcontextprotocol/clientCapabilities":{{"elicitation":{{"form":{{}}}}}},
        "io.modelcontextprotocol/clientInfo":{{"name":"k1-executing-acceptance","version":"1"}}}},
        "name":"recovery_workbench__submit_change","arguments":arguments,
        "requestState":request_state,
        "inputResponses":{{"confirm":{{"action":"accept","content":{{"confirm":True}}}}}}}}
    req = urllib.request.Request(BASE + "/mcp?execution=classic",
        data=json.dumps({{"jsonrpc":"2.0","id":1,"method":"tools/call","params":params}}).encode(),
        headers={{"Authorization":"Bearer "+token,"Content-Type":"application/json",
            "Accept":"application/json, text/event-stream","MCP-Protocol-Version":"2026-07-28",
            "Mcp-Method":"tools/call","Mcp-Name":"recovery_workbench__submit_change",
            "X-Request-ID":correlation}}, method="POST")
    try:
        response=urllib.request.urlopen(req, timeout=15)
        body=json.load(response); pod=response.headers.get("X-Gateway-Instance")
    except urllib.error.HTTPError as error:
        body=json.loads(error.read()); pod=error.headers.get("X-Gateway-Instance")
    last=(body,pod)
    if pod != {json.dumps(prompt["pod"])}: break
if last is None or last[1] == {json.dumps(prompt["pod"])}:
    raise AssertionError("executing-claim retry did not reach a different pod")
result = last[0].get("result", {{}})
structured = result.get("structuredContent", {{}})
if not (last[0].get("error") or result.get("error") or structured.get("error")):
    raise AssertionError("executing sensitive write was not blocked: "+str(last[0]))
if metrics() != before:
    raise AssertionError("executing-claim retry dispatched an upstream side effect")
prompt_pod = {json.dumps(prompt["pod"])}
print("executing_retry_pods="+prompt_pod+"->"+str(last[1])+" blocked=true no_dispatch=true")
"""
    retry_output = kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "exec",
        "-i",
        CLIENT,
        "--",
        "python",
        "-",
        input_text=retry_source,
    )
    print(retry_output.strip())
    row = kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "exec",
        postgres_pod,
        "--",
        "psql",
        "-U",
        "gateway",
        "-d",
        "mcpcodeaway",
        "-At",
        "-F",
        "|",
        "-c",
        "SELECT correlation_id,status,count(*) FROM sensitive_write_audits "
        f"WHERE correlation_id='{correlation}' GROUP BY correlation_id,status",
    )
    if row != f"{correlation}|executing|1":
        raise RuntimeError(f"executing journal row changed after cross-pod retry: {row}")
    return correlation


def postgres_correlation_audit(context: str, namespace: str, correlation: str) -> str:
    pod = kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        "app.kubernetes.io/name=postgres",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )
    return kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "exec",
        pod,
        "--",
        "psql",
        "-U",
        "gateway",
        "-d",
        "mcpcodeaway",
        "-At",
        "-F",
        "|",
        "-c",
        "SELECT correlation_id,status,count(*) FROM sensitive_write_audits "
        f"WHERE correlation_id='{correlation}' GROUP BY correlation_id,status",
    )


def wait_gateway(context: str, namespace: str) -> list[str]:
    kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "rollout",
        "status",
        "deployment/gateway",
        "--timeout=240s",
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        listing = kubectl(
            "--context",
            context,
            "-n",
            namespace,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=gateway",
            "-o",
            "json",
        )
        pods = json.loads(listing)["items"]
        ready = [
            pod["metadata"]["name"]
            for pod in pods
            if not pod["metadata"].get("deletionTimestamp")
            and pod.get("status", {}).get("conditions")
            and any(
                item.get("type") == "Ready" and item.get("status") == "True"
                for item in pod["status"]["conditions"]
            )
        ]
        if len(ready) == 2:
            return ready
        time.sleep(1)
    raise RuntimeError(f"expected two non-terminating ready gateway pods, found {ready}")


def lifecycle_checks(context: str, namespace: str, run_tag: str, write_pod: str) -> None:
    before = wait_gateway(context, namespace)
    if write_pod not in before:
        raise RuntimeError(f"the completed-write pod is no longer active: {write_pod}")
    kubectl("--context", context, "-n", namespace, "delete", "pod", write_pod, "--wait=false")
    after = wait_gateway(context, namespace)
    if len(after) != 2:
        raise RuntimeError("replica replacement did not restore two ready pods")
    print(f"pod_termination_replacement=passed prior={write_pod} active={','.join(after)}")
    run_health_probe(context, namespace)
    run_termination_retry(context, namespace, write_pod)

    before_restart = wait_gateway(context, namespace)
    kubectl(
        "--context", context, "-n", namespace, "delete", "pod", before_restart[0], "--wait=false"
    )
    wait_gateway(context, namespace)
    audit = postgres_audit(context, namespace, run_tag)
    if f"k1-sensitive-{run_tag}|succeeded|1" not in audit:
        raise RuntimeError(f"completed sensitive-write row did not survive restart: {audit}")
    print("pod_restart_shared_journal=passed")

    kubectl("--context", context, "-n", namespace, "rollout", "restart", "deployment/gateway")
    service_calls = 0
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        run_health_probe(context, namespace)
        service_calls += 1
        status = json.loads(
            kubectl(
                "--context", context, "-n", namespace, "get", "deployment", "gateway", "-o", "json"
            )
        )["status"]
        if status.get("updatedReplicas") == 2 and status.get("availableReplicas") == 2:
            break
        time.sleep(1)
    else:
        raise RuntimeError("rolling restart did not restore two available replicas")
    wait_gateway(context, namespace)
    audit_after = postgres_audit(context, namespace, run_tag)
    if f"k1-sensitive-{run_tag}|succeeded|1" not in audit_after:
        raise RuntimeError(f"sensitive-write journal changed during rollout: {audit_after}")
    print(f"rolling_restart=passed service_check_iterations={max(1, service_calls)}")


def database_readiness_outage(context: str, namespace: str) -> None:
    starting_gateway_pods = wait_gateway(context, namespace)
    gateway_pod = starting_gateway_pods[0]
    postgres_pod = kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        "app.kubernetes.io/name=postgres",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )
    kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "patch",
        "service/postgres",
        "--type=merge",
        "-p",
        '{"spec":{"selector":{"app.kubernetes.io/name":"postgres-outage"}}}',
    )
    probe = r"""import time, urllib.error, urllib.request
deadline = time.time() + 25
while time.time() < deadline:
    scaled_gateway = False
    try:
        response = urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=4)
        status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    if status == 503:
        print("database_outage_readiness=503")
        break
    time.sleep(0.25)
else:
    raise SystemExit("gateway readiness never returned 503 during PostgreSQL outage")
"""
    try:
        endpoint_deadline = time.monotonic() + 30
        while time.monotonic() < endpoint_deadline:
            endpoints = kubectl(
                "--context",
                context,
                "-n",
                namespace,
                "get",
                "endpoints",
                "postgres",
                "-o",
                "jsonpath={.subsets}",
            )
            if not endpoints:
                break
            time.sleep(0.5)
        else:
            raise TimeoutError("PostgreSQL Service retained endpoints after selector change")
        kubectl(
            "--context",
            context,
            "-n",
            namespace,
            "exec",
            postgres_pod,
            "--",
            "sh",
            "-c",
            'pg_ctl -D "$PGDATA" stop -m fast',
            check=False,
        )
        kubectl(
            "--context",
            context,
            "-n",
            namespace,
            "scale",
            "deployment/gateway",
            "--replicas=3",
        )
        scaled_gateway = True
        startup_deadline = time.monotonic() + 60
        failed_startup_pod = None
        while time.monotonic() < startup_deadline:
            listing = json.loads(
                kubectl(
                    "--context",
                    context,
                    "-n",
                    namespace,
                    "get",
                    "pods",
                    "-l",
                    "app.kubernetes.io/name=gateway",
                    "-o",
                    "json",
                )
            )["items"]
            for candidate in listing:
                name = candidate["metadata"]["name"]
                statuses = candidate.get("status", {}).get("containerStatuses", [])
                if name not in starting_gateway_pods and statuses:
                    state = statuses[0].get("state", {})
                    if (
                        statuses[0].get("restartCount", 0) > 0
                        or state.get("waiting", {}).get("reason") == "CrashLoopBackOff"
                    ):
                        failed_startup_pod = name
                        break
            if failed_startup_pod:
                break
            time.sleep(1)
        if not failed_startup_pod:
            raise TimeoutError("a gateway started successfully while PostgreSQL was unavailable")
        kubectl(
            "--context",
            context,
            "-n",
            namespace,
            "exec",
            gateway_pod,
            "--",
            "python",
            "-c",
            probe,
        )
    finally:
        kubectl(
            "--context",
            context,
            "-n",
            namespace,
            "patch",
            "service/postgres",
            "--type=merge",
            "-p",
            '{"spec":{"selector":{"app.kubernetes.io/name":"postgres"}}}',
        )
        kubectl(
            "--context",
            context,
            "-n",
            namespace,
            "wait",
            "--for=condition=Ready",
            f"pod/{postgres_pod}",
            "--timeout=180s",
        )
        if scaled_gateway:
            kubectl(
                "--context",
                context,
                "-n",
                namespace,
                "scale",
                "deployment/gateway",
                "--replicas=3",
            )
            kubectl(
                "--context",
                context,
                "-n",
                namespace,
                "rollout",
                "status",
                "deployment/gateway",
                "--timeout=180s",
            )
            kubectl(
                "--context",
                context,
                "-n",
                namespace,
                "scale",
                "deployment/gateway",
                "--replicas=2",
            )
            kubectl(
                "--context",
                context,
                "-n",
                namespace,
                "rollout",
                "status",
                "deployment/gateway",
                "--timeout=180s",
            )
    recovery_probe = r"""import sys, time, urllib.error, urllib.request
deadline = time.time() + 90
while time.time() < deadline:
    try:
        response = urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=3)
        if response.status == 200:
            print("database_recovery_readiness=200")
            break
    except (urllib.error.HTTPError, OSError):
        time.sleep(1)
else:
    raise SystemExit("gateway did not recover readiness after PostgreSQL restarted")
"""
    kubectl(
        "--context",
        context,
        "-n",
        namespace,
        "exec",
        gateway_pod,
        "--",
        "python",
        "-c",
        recovery_probe,
    )
    print("startup_without_postgres_failed_and_recovered=true")
    print("postgres_unavailable_readiness_and_recovery=passed")


def resource_limit_check(context: str, namespace: str) -> None:
    pod_name = "mcpcodeaway-k1-memory-limit-probe"
    pod_manifest = f"""apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
  namespace: {namespace}
spec:
  restartPolicy: Never
  containers:
    - name: allocator
      image: python:3.13-alpine
      command:
        - python
        - -c
        - |
          import time
          data = bytearray(256 * 1024 * 1024)
          for offset in range(0, len(data), 4096):
              data[offset] = 1
          time.sleep(5)
      resources:
        requests:
          cpu: 10m
          memory: 16Mi
        limits:
          memory: 64Mi
"""
    kubectl("--context", context, "apply", "-f", "-", input_text=pod_manifest)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = json.loads(
                kubectl(
                    "--context",
                    context,
                    "-n",
                    namespace,
                    "get",
                    "pod",
                    pod_name,
                    "-o",
                    "json",
                )
            )["status"]
            terminated = (
                status.get("containerStatuses", [{}])[0].get("state", {}).get("terminated", {})
            )
            if terminated.get("reason") == "OOMKilled":
                print("resource_limit_probe=OOMKilled_at_64Mi")
                return
            if status.get("phase") == "Failed":
                raise RuntimeError(f"memory-limit probe failed unexpectedly: {status}")
            time.sleep(1)
        raise TimeoutError("Kubernetes did not enforce the 64Mi memory limit")
    finally:
        kubectl(
            "--context",
            context,
            "-n",
            namespace,
            "delete",
            "pod",
            pod_name,
            "--ignore-not-found=true",
            "--grace-period=0",
            "--force",
            "--wait=false",
            check=False,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--client-only", action="store_true")
    args = parser.parse_args()
    created_client = False
    try:
        ready = wait_gateway(args.context, args.namespace)
        print("ready_gateway_pods=" + ",".join(ready))
        wait_client_ready(args.context, args.namespace)
        created_client = True
        output = run_client(args.context, args.namespace)
        run_tag_line = next(line for line in output.splitlines() if line.startswith("run_tag="))
        run_tag = run_tag_line.split("=", 1)[1]
        if args.client_only:
            print("external_postgres_client_checks=passed")
            return 0
        write_pod_line = next(
            line for line in output.splitlines() if line.startswith("approval_prompt_pod=")
        )
        write_pod = write_pod_line.split("dispatched_write_pod=", 1)[1]
        audit = postgres_audit(args.context, args.namespace, run_tag)
        print("postgres_sensitive_write_rows=" + (audit or "none"))
        if f"k1-sensitive-{run_tag}|succeeded|1" not in audit:
            raise RuntimeError(f"PostgreSQL did not retain exactly one completed write: {audit}")
        if f"k1-independent-{run_tag}|outcome_unknown|1" not in audit:
            raise RuntimeError(f"unresolved operation was not journaled once: {audit}")
        executing_correlation = executing_claim_check(args.context, args.namespace, run_tag)
        lifecycle_checks(args.context, args.namespace, run_tag, write_pod)
        executing_audit = postgres_correlation_audit(
            args.context, args.namespace, executing_correlation
        )
        if executing_audit != f"{executing_correlation}|executing|1":
            raise RuntimeError(
                f"executing operation changed across pod lifecycle: {executing_audit}"
            )
        print("executing_journal_survived_pod_lifecycle=true")
        database_readiness_outage(args.context, args.namespace)
        resource_limit_check(args.context, args.namespace)
        print("acceptance_result=passed")
        return 0
    finally:
        if created_client:
            kubectl(
                "--context",
                args.context,
                "-n",
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
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Kubernetes acceptance failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
