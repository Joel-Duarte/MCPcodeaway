"""Tiered live demo traffic generator across enterprise groups, modes, and custom upstreams."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from gateway.mcp_protocol import request_headers, request_meta

if TYPE_CHECKING:
    from fastapi import FastAPI

    from gateway.server import GatewayRuntime

DemoTier = Literal["light", "enterprise", "stress"]

_TIER_TIMING: dict[str, tuple[float, float]] = {
    "light": (2.0, 0.45),
    "enterprise": (0.5, 0.2),
    "stress": (0.2, 0.06),
}

_BUILTIN_SERVERS = frozenset(
    {
        "billing_legacy_eu",
        "billing_legacy_us",
        "ops_ticketing",
        "snowflake_ledger",
        "crm_accounts",
        "hr_directory",
        "devops_ci",
        "recovery_workbench",
    }
)


class DemoTrafficRunner:
    """Dispatches variable real /mcp JSON-RPC traffic across tiers and company groups."""

    def __init__(self, runtime: GatewayRuntime, app: FastAPI | None = None) -> None:
        self.runtime = runtime
        self._app = app
        self._task: asyncio.Task[None] | None = None
        self._step = 0
        self.tier: DemoTier = "enterprise"
        self.requests_sent = 0
        self.errors_count = 0
        self.rpc_errors_count = 0
        self.http_errors_count = 0
        self.write_calls_count = 0
        self.discovery_requests_count = 0
        self.task_polls_count = 0
        self.golden_runs_count = 0
        self.last_target = ""
        self.last_error = ""
        self.model_profiles: list[dict[str, Any]] = [
            {
                "model": "claude-sonnet-4-5",
                "codemode_success_rate": 0.98,
                "traditional_success_rate": 0.95,
                "traditional_turns": 4,
            },
            {
                "model": "gpt-4o",
                "codemode_success_rate": 0.92,
                "traditional_success_rate": 0.94,
                "traditional_turns": 3,
            },
            {
                "model": "gemini-2.5-pro",
                "codemode_success_rate": 0.96,
                "traditional_success_rate": 0.90,
                "traditional_turns": 4,
            },
            {
                "model": "llama-3.3-70b",
                "codemode_success_rate": 0.45,
                "traditional_success_rate": 0.88,
                "traditional_turns": 2,
            },
        ]

    def bind_app(self, app: FastAPI) -> None:
        self._app = app

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.runtime.settings.app_env != "prod"
            and self.runtime.settings.effective_profile_name != "production",
            "running": self.running,
            "tier": self.tier,
            "requests_sent": self.requests_sent,
            "errors_count": self.errors_count,
            "rpc_errors_count": self.rpc_errors_count,
            "http_errors_count": self.http_errors_count,
            "write_calls_count": self.write_calls_count,
            "discovery_requests_count": self.discovery_requests_count,
            "task_polls_count": self.task_polls_count,
            "golden_runs_count": self.golden_runs_count,
            "last_target": self.last_target,
            "last_error": self.last_error,
        }

    def simulated_workflow_evidence(self) -> list[dict[str, Any]]:
        """Create labeled, deterministic paired fixtures for the demo report only."""
        workloads = (
            "single_read",
            "small_fanout",
            "data_reconciliation",
            "large_data_reconciliation",
            "sequential_workflow",
        )
        thresholds = (0.1, 0.3, 0.5, 0.7, 0.9)
        created_at = datetime.now(UTC).isoformat()
        rows: list[dict[str, Any]] = []
        for profile in self.model_profiles:
            model = str(profile.get("model") or "demo-model").strip().lower()
            for workload in workloads:
                for repeat, threshold in enumerate(thresholds):
                    for mode in ("traditional", "codemode"):
                        key = f"{model}:{workload}:{repeat}:{mode}"
                        rate_key = (
                            "traditional_success_rate"
                            if mode == "traditional"
                            else "codemode_success_rate"
                        )
                        correct = threshold <= float(profile.get(rate_key, 0.9))
                        rows.append(
                            {
                                "version": 1,
                                "evidence_id": f"demo:{key}",
                                "created_at": created_at,
                                "model": model,
                                "workload": workload,
                                "scenario_id": workload,
                                "repeat_id": f"demo-repeat-{repeat}",
                                "fixture_id": "demo-simulator-v1",
                                "discovery_config": {
                                    "runner_version": "demo-traffic-v1",
                                    "model_source": "demo-simulator",
                                },
                                "mode": mode,
                                "correct": correct,
                                "grounded": correct,
                                "input_tokens": 480 if mode == "traditional" else 290,
                                "output_tokens": 90 if mode == "traditional" else 70,
                                "wall_ms": 280 if mode == "traditional" else 190,
                                "upstream_calls": 3 if mode == "traditional" else 2,
                                "turns": int(profile.get("traditional_turns", 3))
                                if mode == "traditional"
                                else 1,
                                "recovery_count": 0,
                                "runtime_failure": not correct,
                            }
                        )
        return rows

    def _next_workloads(self) -> list[tuple[str, str, str, dict[str, Any], dict[str, str] | None]]:
        batch_key = f"demo-{uuid4().hex}"
        workloads: list[tuple[str, str, str, dict[str, Any], dict[str, str] | None]] = [
            (
                "gateway_admin",
                "mode=traditional",
                "recovery:delayed_write",
                {
                    "name": "recovery_workbench__submit_change",
                    "arguments": {
                        "idempotency_key": batch_key,
                        "resource_id": "DEMO-RESOURCE-01",
                        "change": "set processing state",
                        "delay_ms": 650,
                    },
                },
                {"X-Model-Name": "claude-sonnet-4-5"},
            ),
            (
                "gateway_admin",
                "mode=traditional",
                "recovery:idempotent_replay",
                {
                    "name": "recovery_workbench__submit_change",
                    "arguments": {
                        "idempotency_key": batch_key,
                        "resource_id": "DEMO-RESOURCE-01",
                        "change": "set processing state",
                        "delay_ms": 650,
                    },
                },
                {"X-Model-Name": "claude-sonnet-4-5"},
            ),
            (
                "gateway_admin",
                "mode=traditional",
                "recovery:idempotency_conflict",
                {
                    "name": "recovery_workbench__submit_change",
                    "arguments": {
                        "idempotency_key": batch_key,
                        "resource_id": "DEMO-RESOURCE-02",
                        "change": "different payload",
                    },
                },
                {"X-Model-Name": "claude-sonnet-4-5"},
            ),
            (
                "gateway_admin",
                "mode=traditional",
                "recovery:upstream_rejection",
                {
                    "name": "recovery_workbench__submit_change",
                    "arguments": {
                        "idempotency_key": f"{batch_key}-reject",
                        "resource_id": "DEMO-RESOURCE-03",
                        "change": "rejected update",
                        "simulate_failure": True,
                    },
                },
                {"X-Model-Name": "claude-sonnet-4-5"},
            ),
            (
                "gateway_admin",
                "mode=traditional",
                "recovery:lost_response_then_status_read",
                {
                    "name": "recovery_workbench__submit_change",
                    "arguments": {
                        "idempotency_key": f"{batch_key}-lost",
                        "resource_id": "DEMO-RESOURCE-04",
                        "change": "commit before simulated response loss",
                        "simulate_lost_response": True,
                    },
                },
                {"X-Model-Name": "claude-sonnet-4-5"},
            ),
            (
                "gateway_admin",
                "mode=traditional",
                "recovery:status_after_lost_response",
                {
                    "name": "recovery_workbench__get_change_status",
                    "arguments": {"idempotency_key": f"{batch_key}-lost"},
                },
                {"X-Model-Name": "claude-sonnet-4-5"},
            ),
            (
                "gateway_admin",
                "mode=traditional",
                "recovery:upstream_stays_pending",
                {
                    "name": "recovery_workbench__submit_change",
                    "arguments": {
                        "idempotency_key": f"{batch_key}-pending",
                        "resource_id": "DEMO-RESOURCE-06",
                        "change": "simulate an upstream operation that remains processing",
                        "simulate_pending": True,
                    },
                },
                {"X-Model-Name": "claude-sonnet-4-5"},
            ),
            (
                "readonly_auditor",
                "mode=traditional",
                "recovery:rbac_write_denied",
                {
                    "name": "recovery_workbench__submit_change",
                    "arguments": {
                        "idempotency_key": f"{batch_key}-denied",
                        "resource_id": "DEMO-RESOURCE-05",
                        "change": "must be denied",
                    },
                },
                {"X-Model-Name": "gpt-4o"},
            ),
            (
                "finance_analyst",
                "mode=codemode",
                "finance:reconcile_bulk_200_codemode",
                {
                    "name": "execute_code",
                    "arguments": {
                        "code": (
                            "led = await snowflake_ledger__query_debt_ledger("
                            "status='overdue', dataset='bulk')\n"
                            "eu = await billing_legacy_eu__list_eu_invoices("
                            "status='overdue', dataset='bulk')\n"
                            "us = await billing_legacy_us__list_us_invoices("
                            "status='overdue', dataset='bulk')\n"
                            "inv = {r['customer_id']: r['amount'] "
                            "for r in eu['invoices'] + us['invoices']}\n"
                            "[{'customer_id': r['customer_id'], "
                            "'delta': r['ledger_balance'] - inv[r['customer_id']]} "
                            "for r in led['rows'] "
                            "if r['customer_id'] in inv "
                            "and r['ledger_balance'] != inv[r['customer_id']]]"
                        )
                    },
                },
                {"X-Model-Name": "claude-sonnet-4-5"},
            ),
            (
                "sales_ops_analyst",
                "mode=traditional",
                "sales_ops:eu_invoices_classic",
                {
                    "name": "billing_legacy_eu__list_eu_invoices",
                    "arguments": {"status": "overdue"},
                },
                {"X-Model-Name": "gpt-4o"},
            ),
            (
                "platform_engineer",
                "execution=auto&discovery_mode=x_intent",
                "engineering:auto_trial_balance",
                {
                    "name": "execute_code",
                    "arguments": {
                        "code": (
                            "tb = await snowflake_ledger__get_trial_balance_lines("
                            "only_variances=True)\nlen(tb['lines'])"
                        )
                    },
                },
                {
                    "X-Intent": "reconcile overdue invoices and trial balance variances",
                    "X-Model-Name": "gemini-2.5-pro",
                },
            ),
            (
                "finance_analyst",
                "mode=codemode",
                "finance:llama_codemode_err",
                {
                    "name": "execute_code",
                    "arguments": {
                        "code": "await billing_legacy_eu__list_eu_invoices(invalid_arg=True)"
                    },
                },
                {"X-Model-Name": "llama-3.3-70b"},
            ),
            (
                "finance_analyst",
                "mode=traditional",
                "finance:llama_classic_ok",
                {
                    "name": "billing_legacy_eu__list_eu_invoices",
                    "arguments": {"status": "overdue"},
                },
                {"X-Model-Name": "llama-3.3-70b"},
            ),
            (
                "legal_counsel",
                "mode=codemode",
                "legal:audit_ledger_codemode",
                {
                    "name": "execute_code",
                    "arguments": {
                        "code": (
                            "tb = await snowflake_ledger__get_trial_balance_lines("
                            "only_variances=True)\n"
                            "{'variances': len(tb['lines'])}"
                        )
                    },
                },
                None,
            ),
            (
                "eu_accountant",
                "mode=codemode",
                "finance_eu:rls_codemode",
                {
                    "name": "execute_code",
                    "arguments": {
                        "code": (
                            "res = await snowflake_ledger__query_debt_ledger(status='overdue')\n"
                            "{'rows': len(res['rows'])}"
                        )
                    },
                },
                None,
            ),
            (
                "us_accountant",
                "mode=codemode",
                "finance_us:rls_codemode",
                {
                    "name": "execute_code",
                    "arguments": {
                        "code": (
                            "res = await snowflake_ledger__query_debt_ledger(status='overdue')\n"
                            "{'rows': len(res['rows'])}"
                        )
                    },
                },
                None,
            ),
            (
                "ops_engineer",
                "mode=codemode",
                "operations:incidents_codemode",
                {
                    "name": "execute_code",
                    "arguments": {
                        "code": (
                            "res = await ops_ticketing__list_terminal_incidents()\n"
                            "{'incidents': len(res['incidents'])}"
                        )
                    },
                },
                None,
            ),
            (
                "support_agent",
                "mode=traditional",
                "support:incidents_classic",
                {"name": "ops_ticketing__list_terminal_incidents", "arguments": {}},
                None,
            ),
            (
                "readonly_auditor",
                "mode=traditional",
                "audit:bulk_ledger_classic",
                {
                    "name": "snowflake_ledger__query_debt_ledger",
                    "arguments": {"status": "overdue", "dataset": "bulk"},
                },
                None,
            ),
            (
                "finance_analyst",
                "mode=codemode&require_grounding=true",
                "security:grounding_block_probe",
                {
                    "name": "execute_code",
                    "arguments": {"code": "{'customer_id': 'CUST-9999', 'delta': 999}"},
                },
                None,
            ),
            (
                "readonly_auditor",
                "mode=traditional",
                "ops:rbac_denied_probe",
                {
                    "name": "ops_ticketing__escalate_incident",
                    "arguments": {"incident_id": "INC-404", "priority": "sev1"},
                },
                None,
            ),
            (
                "sales_ops_analyst",
                "mode=codemode&require_grounding=true",
                "crm:grounding_block_probe",
                {
                    "name": "execute_code",
                    "arguments": {"code": "{'account_id': 'ACC-9999', 'arr_usd': 999999}"},
                },
                None,
            ),
        ]

        profiles = self.model_profiles or [{"model": "claude-sonnet-4-5"}]
        personas = [
            "finance_analyst",
            "ops_engineer",
            "platform_engineer",
            "readonly_auditor",
            "sales_ops_analyst",
            "support_agent",
            "gateway_admin",
            "eu_accountant",
            "us_accountant",
            "legal_counsel",
        ]
        idx = 0
        dynamic_workloads: list[tuple[str, str, str, dict[str, Any], dict[str, str] | None]] = []
        for upstream in self.runtime.registry.list_upstreams():
            if not upstream.enabled:
                continue
            if upstream.name not in _BUILTIN_SERVERS:
                continue
            endpoint = upstream.url or upstream.openapi_spec_url or ""
            if urlsplit(endpoint).hostname not in {"127.0.0.1", "localhost", "mock-backends"}:
                continue
            for safe_tool in self.runtime.catalog.get_server_tools(upstream.name):
                if safe_tool.mutating:
                    continue
                schema = safe_tool.input_schema or {}
                props = schema.get("properties") or {}
                req_fields = schema.get("required") or []
                default_args: dict[str, Any] = {}
                for field in req_fields:
                    ftype = (props.get(field) or {}).get("type", "string")
                    if ftype == "integer":
                        default_args[field] = 1
                    elif ftype == "number":
                        default_args[field] = 10.0
                    elif ftype == "boolean":
                        default_args[field] = False
                    else:
                        default_args[field] = "demo"
                prof = profiles[idx % len(profiles)]
                model_name = str(prof.get("model") or "claude-sonnet-4-5")
                persona = personas[idx % len(personas)]
                arm = "codemode" if idx % 2 == 0 else "traditional"
                idx += 1
                arg_str = ", ".join(f"{k}={v!r}" for k, v in default_args.items())
                params: dict[str, Any] = (
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                f"res = await {safe_tool.qualified_name}({arg_str})\n"
                                f"len(res) if isinstance(res, (list, dict)) else 1"
                            )
                        },
                    }
                    if arm == "codemode"
                    else {"name": safe_tool.qualified_name, "arguments": default_args}
                )
                dynamic_workloads.append(
                    (
                        persona,
                        f"mode={arm}",
                        f"{upstream.name}:{safe_tool.name}:{arm}",
                        params,
                        {"X-Model-Name": model_name, "X-Admin-Force-Mode": arm},
                    )
                )
        registry = getattr(self.runtime, "registry", None)
        if registry is not None and not any(
            upstream.name == "recovery_workbench" for upstream in registry.list_upstreams()
        ):
            workloads = [item for item in workloads if not item[2].startswith("recovery:")]
        return workloads + dynamic_workloads

    async def _dispatch_one(
        self,
        client: httpx.AsyncClient,
        persona: str,
        query: str,
        label: str,
        params: dict[str, Any],
        extra_headers: dict[str, str] | None,
    ) -> bool:
        dev_keys = self.runtime.dev_keys
        if dev_keys is None:
            return False
        token_overrides = (
            {
                "roles": ["gateway_admin", "admin"],
                "groups": ["platform-admins"],
                "scopes": ["mcp.tools", "mcp.write"],
            }
            if persona == "gateway_admin"
            else None
        )
        token = dev_keys.issue_user_token(persona=persona, overrides=token_overrides)
        headers = {
            **request_headers("tools/call", tool_name=str(params.get("name") or "") or None),
            "Authorization": f"Bearer {token}",
            **(extra_headers or {}),
        }
        payload_params = {"_meta": request_meta(supports_tasks=True), **params}
        try:
            if str(params.get("name") or "").endswith("__submit_change"):
                self.write_calls_count += 1
            resp = await client.post(
                f"/mcp?{query}",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": self._step + 1,
                    "method": "tools/call",
                    "params": payload_params,
                },
            )
            self.requests_sent += 1
            self.last_target = label
            if resp.status_code >= 400:
                self.errors_count += 1
                self.http_errors_count += 1
                self.last_error = f"HTTP {resp.status_code} on {label}"
                return False
            body = resp.json()
            rpc_error = body.get("error") if isinstance(body, dict) else None
            result = body.get("result") if isinstance(body, dict) else None
            if isinstance(result, dict) and result.get("resultType") == "task":
                task_id = str(result.get("taskId") or "")
                if task_id:
                    poll = await client.post(
                        "/mcp",
                        headers={
                            **request_headers("tasks/get", task_id=task_id),
                            "Authorization": f"Bearer {token}",
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": f"demo-task-{self._step + 1}",
                            "method": "tasks/get",
                            "params": {
                                "taskId": task_id,
                                "_meta": request_meta(supports_tasks=True),
                            },
                        },
                    )
                    self.task_polls_count += 1
                    if poll.status_code >= 400:
                        self.errors_count += 1
                        self.http_errors_count += 1
                        self.last_error = f"HTTP {poll.status_code} polling task {task_id}"
                        return False
                    task_body = poll.json()
                    if task_body.get("error"):
                        self.errors_count += 1
                        self.rpc_errors_count += 1
                        self.last_error = f"MCP task poll failed for {task_id}"
                        return False
            if rpc_error or (isinstance(result, dict) and result.get("isError")):
                self.errors_count += 1
                self.rpc_errors_count += 1
                error = rpc_error or result
                detail = (
                    error.get("message", "MCP tool returned an error")
                    if isinstance(error, dict)
                    else str(error)
                )
                self.last_error = f"MCP error on {label}: {detail}"
                return False
            self.last_error = ""
            return True
        except Exception as exc:
            self.requests_sent += 1
            self.errors_count += 1
            self.http_errors_count += 1
            self.last_target = label
            self.last_error = str(exc)
            return False

    async def _dispatch_discovery(
        self, client: httpx.AsyncClient, *, method: str, query: str
    ) -> bool:
        """Record a real listing or search against the gateway for demo discovery telemetry."""
        if self.runtime.dev_keys is None:
            return False
        token = self.runtime.dev_keys.issue_user_token(
            persona="gateway_admin",
            overrides={
                "roles": ["gateway_admin", "admin"],
                "groups": ["platform-admins"],
                "scopes": ["mcp.tools", "mcp.write"],
            },
        )
        headers = {
            **request_headers(method, tool_name="search_tools" if method == "tools/call" else None),
            "Authorization": f"Bearer {token}",
            "X-Model-Name": "claude-sonnet-4-5",
        }
        params: dict[str, Any] = {"_meta": request_meta()}
        if method == "tools/list":
            params["limit"] = 50
            endpoint = "/mcp?execution=classic&discovery_mode=upfront"
        else:
            params.update(
                {
                    "name": "search_tools",
                    "arguments": {"query": query, "limit": 10},
                }
            )
            endpoint = "/mcp?execution=classic&discovery_mode=deferred"
        try:
            response = await client.post(
                endpoint,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": f"demo-discovery-{self.discovery_requests_count + 1}",
                    "method": method,
                    "params": params,
                },
            )
            self.discovery_requests_count += 1
            if response.status_code >= 400:
                self.errors_count += 1
                self.http_errors_count += 1
                self.last_error = f"HTTP {response.status_code} during demo {method}"
                return False
            body = response.json()
            if body.get("error"):
                self.errors_count += 1
                self.rpc_errors_count += 1
                self.last_error = f"MCP {method} discovery failed"
                return False
            return True
        except Exception as exc:
            self.errors_count += 1
            self.http_errors_count += 1
            self.last_error = str(exc)
            return False

    async def _run_discovery(self) -> dict[str, int]:
        if self._app is None:
            raise RuntimeError("DemoTrafficRunner app is not bound")
        start = self.discovery_requests_count
        ok = 0
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app), base_url="http://demo.local"
        ) as client:
            for method, query in (
                ("tools/list", ""),
                ("tools/call", "reconcile overdue invoices"),
            ):
                if await self._dispatch_discovery(client, method=method, query=query):
                    ok += 1
        return {"sent": self.discovery_requests_count - start, "ok": ok}

    async def golden_run(self) -> dict[str, Any]:
        """Exercise discovery plus every bounded local golden traffic flow once."""
        workloads = self._next_workloads()
        discovery = await self._run_discovery()
        burst = await self.burst(count=min(max(1, len(workloads)), 100))
        self.golden_runs_count += 1
        return {
            "discovery": discovery,
            "traffic": burst,
            "workload_cases": min(len(workloads), 100),
            "status": self.status(),
        }

    async def burst(self, count: int = 8) -> dict[str, Any]:
        if self._app is None:
            raise RuntimeError("DemoTrafficRunner app is not bound")
        workloads = self._next_workloads()
        if not workloads:
            return {"sent": 0, "ok": 0, "errors": 0, "status": self.status()}
        sent = 0
        ok = 0
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app), base_url="http://demo.local"
        ) as client:
            for _ in range(max(1, min(count, 100))):
                if self._step % len(workloads) == 0:
                    workloads = self._next_workloads()
                item = workloads[self._step % len(workloads)]
                self._step += 1
                sent += 1
                if await self._dispatch_one(client, *item):
                    ok += 1
        return {"sent": sent, "ok": ok, "errors": sent - ok, "status": self.status()}

    async def _loop(self) -> None:
        if self._app is None:
            return
        workloads = self._next_workloads()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app), base_url="http://demo.local"
        ) as client:
            for method, query in (("tools/list", ""), ("tools/call", "invoice status")):
                await self._dispatch_discovery(client, method=method, query=query)
            while True:
                if self._step % len(workloads) == 0:
                    workloads = self._next_workloads()
                if workloads:
                    item = workloads[self._step % len(workloads)]
                    self._step += 1
                    await self._dispatch_one(client, *item)
                base_delay, jitter = _TIER_TIMING.get(self.tier, (0.5, 0.2))
                await asyncio.sleep(max(0.1, base_delay + random.uniform(-jitter, jitter)))

    async def start(self, tier: DemoTier = "enterprise") -> dict[str, Any]:
        self.tier = tier if tier in _TIER_TIMING else "enterprise"
        if not self.running:
            self._task = asyncio.create_task(self._loop())
        return self.status()

    async def set_tier(self, tier: DemoTier) -> dict[str, Any]:
        self.tier = tier if tier in _TIER_TIMING else "enterprise"
        return self.status()

    async def stop(self) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        return self.status()
