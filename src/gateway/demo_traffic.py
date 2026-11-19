"""Tiered live demo traffic generator across enterprise groups, modes, and custom upstreams."""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any, Literal

import httpx

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
    {"billing_legacy_eu", "billing_legacy_us", "ops_ticketing", "snowflake_ledger"}
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
                None,
            ),
            (
                "sales_ops_analyst",
                "mode=traditional",
                "sales_ops:eu_invoices_classic",
                {
                    "name": "billing_legacy_eu__list_eu_invoices",
                    "arguments": {"status": "overdue"},
                },
                None,
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
                {"X-Intent": "reconcile overdue invoices and trial balance variances"},
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
        ]

        # Probe any custom upstreams registered by the admin so they appear live in analytics.
        for upstream in self.runtime.registry.list_upstreams():
            if not upstream.enabled or upstream.name in _BUILTIN_SERVERS:
                continue
            tools = self.runtime.catalog.get_server_tools(upstream.name)
            safe_tool = next(
                (t for t in tools if not t.mutating and not (t.input_schema or {}).get("required")),
                None,
            )
            if safe_tool is None:
                continue
            workloads.append(
                (
                    "gateway_admin",
                    "mode=codemode",
                    f"custom:{safe_tool.qualified_name}",
                    {
                        "name": "execute_code",
                        "arguments": {"code": f"await {safe_tool.qualified_name}()"},
                    },
                    None,
                )
            )
        return workloads

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
        headers = {"Authorization": f"Bearer {token}", **(extra_headers or {})}
        try:
            resp = await client.post(
                f"/mcp?{query}",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": self._step + 1,
                    "method": "tools/call",
                    "params": params,
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
            for _ in range(max(1, min(count, 24))):
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
