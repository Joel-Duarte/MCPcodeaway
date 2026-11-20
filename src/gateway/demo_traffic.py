"""Tiered live demo traffic generator across enterprise groups, modes, and custom upstreams."""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any, Literal

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
            "last_target": self.last_target,
            "last_error": self.last_error,
        }

    def _next_workloads(self) -> list[tuple[str, str, str, dict[str, Any], dict[str, str] | None]]:
        workloads: list[tuple[str, str, str, dict[str, Any], dict[str, str] | None]] = [
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

        # Probe all enabled upstreams and their non-mutating tools across demo models.
        profiles = self.model_profiles or [{"model": "claude-sonnet-4-5"}]
        idx = 0
        dynamic_workloads: list[tuple[str, str, str, dict[str, Any], dict[str, str] | None]] = []
        for upstream in self.runtime.registry.list_upstreams():
            if not upstream.enabled:
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
                arm = "codemode" if idx % 2 == 0 else "traditional"
                idx += 1
                arg_str = ", ".join(f"{k}={v!r}" for k, v in default_args.items())
                params: dict[str, Any] = (
                    {
                        "name": "execute_code",
                        "arguments": {"code": f"await {safe_tool.qualified_name}({arg_str})"},
                    }
                    if arm == "codemode"
                    else {"name": safe_tool.qualified_name, "arguments": default_args}
                )
                dynamic_workloads.append(
                    (
                        "gateway_admin",
                        f"mode={arm}",
                        f"{upstream.name}:{safe_tool.name}:{arm}",
                        params,
                        {"X-Model-Name": model_name, "X-Admin-Force-Mode": arm},
                    )
                )
        return dynamic_workloads + workloads

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
        token = dev_keys.issue_user_token(persona=persona)
        headers = {
            **request_headers("tools/call", tool_name=str(params.get("name") or "") or None),
            "Authorization": f"Bearer {token}",
            **(extra_headers or {}),
        }
        payload_params = {"_meta": request_meta(), **params}
        try:
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
                self.last_error = f"HTTP {resp.status_code} on {label}"
                return False
            self.last_error = ""
            return True
        except Exception as exc:
            self.requests_sent += 1
            self.errors_count += 1
            self.last_target = label
            self.last_error = str(exc)
            return False

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
                item = workloads[self._step % len(workloads)]
                self._step += 1
                sent += 1
                if await self._dispatch_one(client, *item):
                    ok += 1
        return {"sent": sent, "ok": ok, "errors": sent - ok, "status": self.status()}

    async def _loop(self) -> None:
        if self._app is None:
            return
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app), base_url="http://demo.local"
        ) as client:
            while True:
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
