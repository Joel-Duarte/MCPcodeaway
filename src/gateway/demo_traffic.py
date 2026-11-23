"""Tiered live demo traffic generator across enterprise groups, modes, and custom upstreams."""

from __future__ import annotations

import asyncio
import copy
import random
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field, model_validator

from gateway.mcp_app_renderer import MCP_APP_EXTENSION, MCP_APP_MIME_TYPE
from gateway.mcp_protocol import (
    MCP_CLIENT_CAPABILITIES_META,
    request_headers,
    request_meta,
)
from gateway.upstream_clients import compute_schema_digest
from gateway.workflow_detector import pick_schema_predicate

if TYPE_CHECKING:
    from fastapi import FastAPI

    from gateway.server import GatewayRuntime

DemoTier = Literal["light", "enterprise", "stress"]


class DemoModelProfile(BaseModel):
    model: str = Field(min_length=1, max_length=160)
    codemode_success_rate: float = Field(default=0.9, ge=0, le=1)
    traditional_success_rate: float = Field(default=0.9, ge=0, le=1)
    traditional_turns: int = Field(default=3, ge=1, le=12)
    input_token_range: tuple[int, int] = Field(default=(400, 1200))
    output_token_range: tuple[int, int] = Field(default=(100, 300))

    @model_validator(mode="after")
    def validate_ranges(self) -> DemoModelProfile:
        if any(
            value < 1 or value > 100_000
            for value in (*self.input_token_range, *self.output_token_range)
        ):
            raise ValueError("Token range values must be between 1 and 100000")
        if self.input_token_range[0] > self.input_token_range[1]:
            raise ValueError("input_token_range minimum must not exceed its maximum")
        if self.output_token_range[0] > self.output_token_range[1]:
            raise ValueError("output_token_range minimum must not exceed its maximum")
        return self


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
        "global_ticketing",
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
        self._business_value_workflow_lock = asyncio.Lock()
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
                "model": "claude-sonnet-5.5",
                "codemode_success_rate": 0.96,
                "traditional_success_rate": 0.88,
                "input_token_range": (700, 2200),
                "output_token_range": (140, 480),
                "traditional_turns": 4,
            },
            {
                "model": "gpt-6.1-sol",
                "codemode_success_rate": 0.84,
                "traditional_success_rate": 0.92,
                "input_token_range": (500, 1900),
                "output_token_range": (100, 390),
                "traditional_turns": 3,
            },
            {
                "model": "gemini-4-hydrogen",
                "codemode_success_rate": 0.76,
                "traditional_success_rate": 0.88,
                "input_token_range": (350, 1500),
                "output_token_range": (90, 340),
                "traditional_turns": 4,
            },
            {
                "model": "qwen3.5-9b",
                "codemode_success_rate": 0.92,
                "traditional_success_rate": 0.72,
                "input_token_range": (220, 1050),
                "output_token_range": (60, 260),
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
            "large_fanout",
            "data_reconciliation",
            "large_data_reconciliation",
            "batch_compute",
            "sequential_workflow",
            "mutating_workflow",
            "large_payload_processing",
        )
        workload_scale = {
            "single_read": 0.6,
            "small_fanout": 1.0,
            "large_fanout": 1.8,
            "data_reconciliation": 1.4,
            "large_data_reconciliation": 2.4,
            "batch_compute": 1.5,
            "sequential_workflow": 1.7,
            "mutating_workflow": 1.2,
            "large_payload_processing": 2.8,
        }
        created_at = datetime.now(UTC).isoformat()
        rows: list[dict[str, Any]] = []
        for profile in self.model_profiles:
            model = str(profile.get("model") or "demo-model").strip().lower()
            for workload in workloads:
                for repeat in range(5):
                    for mode in ("traditional", "codemode"):
                        key = f"{model}:{workload}:{repeat}:{mode}"
                        rng = random.Random(f"demo-evidence-v2:{key}")
                        rate = float(
                            profile.get(
                                "traditional_success_rate"
                                if mode == "traditional"
                                else "codemode_success_rate",
                                0.9,
                            )
                        )
                        correct = rng.random() < rate
                        grounded = rng.random() < min(0.995, rate + 0.06)
                        input_range = profile.get("input_token_range", (400, 1200))
                        output_range = profile.get("output_token_range", (100, 300))
                        scale = workload_scale[workload]
                        mode_scale = 1.0 if mode == "traditional" else rng.uniform(0.42, 0.95)
                        input_tokens = round(
                            rng.randint(int(input_range[0]), int(input_range[1]))
                            * scale
                            * mode_scale
                        )
                        output_tokens = round(
                            rng.randint(int(output_range[0]), int(output_range[1]))
                            * (1.0 if mode == "traditional" else rng.uniform(0.65, 1.2))
                        )
                        classic_turns = int(profile.get("traditional_turns", 3))
                        turns = (
                            rng.randint(max(1, classic_turns - 1), min(12, classic_turns + 1))
                            if mode == "traditional"
                            else rng.randint(1, max(2, (classic_turns + 1) // 2))
                        )
                        rows.append(
                            {
                                "version": 1,
                                "evidence_id": f"demo:{key}",
                                "created_at": created_at,
                                "model": model,
                                "workload": workload,
                                "scenario_id": workload,
                                "repeat_id": f"demo-repeat-{repeat}",
                                "fixture_id": "demo-simulator-v2",
                                "discovery_config": {
                                    "runner_version": "demo-traffic-v2",
                                    "model_source": "demo-simulator",
                                },
                                "mode": mode,
                                "correct": correct,
                                "grounded": grounded,
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                                "wall_ms": rng.randint(180, 1500)
                                * (1.0 if mode == "traditional" else rng.uniform(0.55, 1.1)),
                                "upstream_calls": rng.randint(2, 5)
                                if mode == "traditional"
                                else rng.randint(1, 3),
                                "turns": turns,
                                "recovery_count": rng.choices([0, 1, 2], weights=[0.75, 0.2, 0.05])[
                                    0
                                ]
                                if workload == "mutating_workflow"
                                else 0,
                                "runtime_failure": not (correct and grounded),
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
        registered_names = (
            {upstream.name for upstream in registry.list_upstreams() if upstream.enabled}
            if registry is not None
            else set()
        )
        if registry is not None and "recovery_workbench" not in registered_names:
            workloads = [item for item in workloads if not item[2].startswith("recovery:")]
        extra_codemode: list[tuple[str, str, str, dict[str, Any], dict[str, str] | None]] = []
        if "crm_accounts" in registered_names:
            extra_codemode.append(
                (
                    "gateway_admin",
                    "mode=codemode",
                    "crm_accounts:account_metrics_codemode",
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "accts = await crm_accounts__list_crm_accounts(limit=10)\n"
                                "metrics = await crm_accounts__list_crm_accounts_metrics()\n"
                                "{'accounts': accts.get('count', 0), "
                                "'active': metrics.get('active_items', 0)}"
                            )
                        },
                    },
                    {"X-Model-Name": "claude-sonnet-4-5"},
                )
            )
        if "devops_ci" in registered_names:
            extra_codemode.append(
                (
                    "gateway_admin",
                    "mode=codemode",
                    "devops_ci:deployment_health_codemode",
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "deps = await devops_ci__list_ci_deployments(limit=10)\n"
                                "health = await devops_ci__list_ci_deployments_health()\n"
                                "{'deployments': deps.get('count', 0), "
                                "'healthy': health.get('healthy', False)}"
                            )
                        },
                    },
                    {"X-Model-Name": "gpt-4o"},
                )
            )
        if "hr_directory" in registered_names:
            extra_codemode.append(
                (
                    "gateway_admin",
                    "mode=codemode",
                    "hr_directory:directory_redaction_codemode",
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "staff = await hr_directory__list_hr_employees(limit=5)\n"
                                "{'count': staff.get('count', 0), "
                                "'sample_emails': [e.get('email') for e in staff.get('items', [])]}"
                            )
                        },
                    },
                    {"X-Model-Name": "claude-sonnet-4-5"},
                )
            )
        if "global_ticketing" in registered_names:
            extra_codemode.append(
                (
                    "ops_engineer",
                    "mode=codemode",
                    "global_ticketing:queue_sla_codemode",
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "tickets = await global_ticketing__list_tickets(region='EU')\n"
                                "metrics = await global_ticketing__get_ticketing_metrics()\n"
                                "{'eu_tickets': tickets.get('count', 0), "
                                "'open_total': metrics.get('open_tickets', 0)}"
                            )
                        },
                    },
                    {"X-Model-Name": "gemini-2.5-pro"},
                )
            )
        return workloads + extra_codemode + dynamic_workloads

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
                "groups": ["platform-admins", "platform"],
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
        business_value_workflow = await self._run_hr_business_value_workflow()
        ticketing_workflow = await self._run_ticketing_business_value_workflow()
        passive_workflows = await self._run_passive_workflow_evidences()
        self.golden_runs_count += 1
        return {
            "discovery": discovery,
            "traffic": burst,
            "workload_cases": min(len(workloads), 100),
            "business_value_workflow": business_value_workflow,
            "ticketing_workflow": ticketing_workflow,
            "passive_workflows": passive_workflows,
            "status": self.status(),
        }

    async def ensure_demo_solutions(self) -> None:
        """Seed pre-built agent solution definitions in local demo mode."""
        if self._app is None or self.runtime.dev_keys is None:
            return
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app), base_url="http://demo.local"
        ) as client:
            seeders = [
                self._ensure_ticketing_demo_workflows,
                self._ensure_crm_demo_solutions,
                self._ensure_devops_demo_solutions,
            ]
            if self.runtime.settings.demo_auto_golden:
                seeders.append(self._ensure_hr_demo_workflow)
            for seeder in seeders:
                try:
                    await seeder(client)
                except Exception:
                    continue

    async def _ensure_ticketing_demo_workflows(self, client: httpx.AsyncClient) -> str | None:
        upstream = self.runtime.registry.get("global_ticketing")
        if upstream is None or not upstream.enabled:
            return None
        endpoint = upstream.url or upstream.openapi_spec_url or ""
        if urlsplit(endpoint).hostname not in {"127.0.0.1", "localhost", "mock-backends"}:
            return None
        tenant_id = next(iter(self.runtime.settings.allowed_tenants), None)
        if not tenant_id or self.runtime.dev_keys is None:
            return None
        workflow_id = "global_ticketing/triage_and_resolve_ticket"
        async with self._business_value_workflow_lock:
            current = await self.runtime.store.get_current_published_integration_definition(
                tenant_id, "global_ticketing"
            )
            if current is not None:
                return workflow_id
            discovery_identity = self.runtime._dev_discovery_identity()
            await self.runtime.ensure_catalog_for_identity(discovery_identity)
            tools = {
                t.qualified_name: t
                for t in self.runtime.catalog.get_server_tools("global_ticketing")
            }
            if not tools:
                return None
            today = datetime.now(UTC).date().isoformat()
            needed = {
                "global_ticketing__list_tickets",
                "global_ticketing__get_ticket",
                "global_ticketing__assign_ticket",
                "global_ticketing__add_ticket_comment",
                "global_ticketing__resolve_ticket",
                "global_ticketing__get_ticket_sla",
                "global_ticketing__escalate_ticket",
                "global_ticketing__list_ticketing_queues",
                "global_ticketing__create_ticket",
                "global_ticketing__get_ticketing_metrics",
            }
            if not needed.issubset(tools):
                return None
            definition = {
                "manifest_version": "1.0",
                "integration": {
                    "id": "global_ticketing",
                    "display_name": "Enterprise Global Ticketing",
                    "upstream_name": "global_ticketing",
                    "status": "draft",
                    "revision": 1,
                },
                "tool_refs": [
                    {"name": name, "schema_sha256": compute_schema_digest([tools[name]])}
                    for name in sorted(needed)
                ],
                "experiences": [
                    {
                        "id": "ticket_inspector_ui",
                        "resource_uri": "ui://mcpcodeaway/global_ticketing/ticket_inspector/r1",
                        "renderer_schema": "mcpcodeaway-ui/1",
                        "tool": "global_ticketing__get_ticket",
                        "fallback": (
                            "Inspect a ticket by invoking global_ticketing__get_ticket "
                            "with ticket_id."
                        ),
                        "input_mapping": {"ticket_id": "/form/ticket_id"},
                        "components": [
                            {"type": "heading", "text": "Global Ticket Inspector"},
                            {
                                "type": "text",
                                "text": (
                                    "Enter a ticket ID to inspect live metadata, status, "
                                    "priority, and assignment."
                                ),
                            },
                            {
                                "type": "text_input",
                                "id": "ticket_id",
                                "label": "Ticket ID",
                                "required": True,
                            },
                            {"type": "button", "label": "Inspect Ticket", "action": "invoke_tool"},
                            {"type": "record", "data": "tool_result"},
                        ],
                        "csp": {"connect_domains": [], "resource_domains": [], "frame_domains": []},
                    },
                    {
                        "id": "ticketing_metrics_ui",
                        "resource_uri": "ui://mcpcodeaway/global_ticketing/metrics_dashboard/r1",
                        "renderer_schema": "mcpcodeaway-ui/1",
                        "tool": "global_ticketing__get_ticketing_metrics",
                        "fallback": (
                            "View ticketing operations metrics via "
                            "global_ticketing__get_ticketing_metrics."
                        ),
                        "input_mapping": {},
                        "components": [
                            {"type": "heading", "text": "Global Ticketing Operations Summary"},
                            {
                                "type": "text",
                                "text": (
                                    "Fetch live global ticketing volume across EU, US, "
                                    "APAC, and LATAM."
                                ),
                            },
                            {"type": "button", "label": "Refresh Metrics", "action": "invoke_tool"},
                            {"type": "record", "data": "tool_result"},
                        ],
                        "csp": {"connect_domains": [], "resource_domains": [], "frame_domains": []},
                    },
                ],
                "workflows": [
                    {
                        "id": "triage_and_resolve_ticket",
                        "name": "Triage and Resolve Ticket",
                        "description": (
                            "List or inspect an open ticket, assign an SRE, leave findings "
                            "in comments, resolve, and verify status."
                        ),
                        "enabled": True,
                        "origin": "agent_prompt",
                        "intent_examples": [
                            "Triage open tickets, assign SRE, comment, and resolve",
                            "Investigate EU ticket and mark resolved with root cause",
                        ],
                        "participating_tools": [
                            "global_ticketing__list_tickets",
                            "global_ticketing__get_ticket",
                            "global_ticketing__assign_ticket",
                            "global_ticketing__add_ticket_comment",
                            "global_ticketing__resolve_ticket",
                        ],
                        "required_evidence": [
                            {
                                "tool": "global_ticketing__resolve_ticket",
                                "min_successful_calls": 1,
                                "result_when": {
                                    "path": "/status",
                                    "type": "string",
                                    "equals": "resolved",
                                },
                            }
                        ],
                        "completion": {
                            "kind": "verified_followup",
                            "tool": "global_ticketing__get_ticket",
                            "success_when": {
                                "path": "/status",
                                "type": "string",
                                "equals": "resolved",
                            },
                        },
                        "business_value": {
                            "manual_baseline_minutes": 14,
                            "baseline_source": "sampled_manual_measurement",
                            "confidence": "sampled",
                            "loaded_labor_cost_per_hour": 85.0,
                            "currency": "USD",
                            "regional_labor_rates": {
                                "US": 115.0,
                                "EU": 85.0,
                                "APAC": 70.0,
                                "LATAM": 55.0,
                            },
                            "review_minutes": {"kind": "expected_average", "minutes": 2.0},
                            "correction_minutes": {"kind": "expected_average", "minutes": 0.5},
                            "effective_from": today,
                            "evidence_note": (
                                "Sampled SRE queue benchmark across regional support pods."
                            ),
                        },
                    },
                    {
                        "id": "ticket_escalation_and_sla_review",
                        "name": "Ticket Escalation and SLA Review",
                        "description": (
                            "Check ticket SLA status, escalate to tier3_incident, and confirm "
                            "critical priority."
                        ),
                        "enabled": True,
                        "origin": "agent_prompt",
                        "intent_examples": [
                            "Check SLA timer and escalate urgent ticket to critical",
                        ],
                        "participating_tools": [
                            "global_ticketing__get_ticket_sla",
                            "global_ticketing__escalate_ticket",
                            "global_ticketing__get_ticket",
                        ],
                        "required_evidence": [
                            {
                                "tool": "global_ticketing__escalate_ticket",
                                "min_successful_calls": 1,
                                "result_when": {
                                    "path": "/status",
                                    "type": "string",
                                    "equals": "escalated",
                                },
                            }
                        ],
                        "completion": {
                            "kind": "verified_followup",
                            "tool": "global_ticketing__get_ticket",
                            "success_when": {
                                "path": "/priority",
                                "type": "string",
                                "equals": "critical",
                            },
                        },
                        "business_value": {
                            "manual_baseline_minutes": 10,
                            "baseline_source": "validated_external_measurement",
                            "confidence": "validated",
                            "loaded_labor_cost_per_hour": 95.0,
                            "currency": "USD",
                            "review_minutes": {"kind": "expected_average", "minutes": 1.0},
                            "correction_minutes": {"kind": "expected_average", "minutes": 0.5},
                            "effective_from": today,
                            "evidence_note": (
                                "Validated on-call SLA escalation runbook timing study."
                            ),
                        },
                    },
                    {
                        "id": "intake_and_route_new_ticket",
                        "name": "Intake and Route New Ticket",
                        "description": (
                            "Inspect regional queues, open a new ticket, and assign an engineer."
                        ),
                        "enabled": True,
                        "origin": "agent_prompt",
                        "intent_examples": [
                            "List regional queues, create ticket, and assign owner",
                        ],
                        "participating_tools": [
                            "global_ticketing__list_ticketing_queues",
                            "global_ticketing__create_ticket",
                            "global_ticketing__assign_ticket",
                        ],
                        "required_evidence": [
                            {
                                "tool": "global_ticketing__create_ticket",
                                "min_successful_calls": 1,
                                "result_when": {
                                    "path": "/ticket_id",
                                    "type": "non_empty_string",
                                },
                            }
                        ],
                        "completion": {
                            "kind": "terminal_tool_result",
                            "tool": "global_ticketing__assign_ticket",
                            "success_when": {
                                "path": "/status",
                                "type": "non_empty_string",
                            },
                        },
                        "business_value": {
                            "manual_baseline_minutes": 6,
                            "baseline_source": "administrator_estimate",
                            "confidence": "unvalidated",
                            "loaded_labor_cost_per_hour": 75.0,
                            "currency": "USD",
                            "review_minutes": {"kind": "expected_average", "minutes": 0.5},
                            "correction_minutes": {"kind": "expected_average", "minutes": 0.5},
                            "effective_from": today,
                            "evidence_note": (
                                "Administrator triage estimate pending external timing audit."
                            ),
                        },
                    },
                ],
                "standalone_tool_values": [],
                "publication": None,
            }
            admin_token = self.runtime.dev_keys.issue_user_token(
                persona="gateway_admin",
                overrides={"tenant_id": tenant_id},
            )
            headers = {"Authorization": f"Bearer {admin_token}"}
            draft = await client.post(
                "/admin/integration-definitions/drafts",
                headers=headers,
                json={"definition": definition},
            )
            if draft.status_code != 200:
                return None
            revision = str(draft.json()["revision"])
            published = await client.post(
                "/admin/integration-definitions/global_ticketing/publish",
                headers=headers,
                json={"revision": revision},
            )
            if published.status_code != 200:
                return None
            return workflow_id

    async def _run_ticketing_business_value_workflow(self) -> dict[str, Any]:
        if self._app is None or self.runtime.dev_keys is None:
            return {"status": "skipped", "reason": "gateway demo runtime is unavailable"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app), base_url="http://demo.local"
        ) as client:
            workflow_id = await self._ensure_ticketing_demo_workflows(client)
            if workflow_id is None:
                return {"status": "skipped", "reason": "local global_ticketing is unavailable"}
            tenant_id = next(iter(self.runtime.settings.allowed_tenants), None)
            if not tenant_id:
                return {"status": "skipped", "reason": "demo tenant is unavailable"}
            caller_token = self.runtime.dev_keys.issue_user_token(
                persona="gateway_admin",
                overrides={
                    "tenant_id": tenant_id,
                    "sub": "demo-sre-operator",
                    "roles": ["ops_engineer", "ticketing_reader", "ticketing_writer", "admin"],
                    "groups": ["operations", "platform-admins"],
                    "client_groups": ["EU"],
                    "scopes": ["mcp.tools", "ops.write"],
                },
            )
            meta = request_meta(client_name="mcpcodeaway-demo-traffic")
            meta[MCP_CLIENT_CAPABILITIES_META]["elicitation"] = {"form": {}}
            intent = "Triage open EU ticket, assign SRE, comment, and resolve"

            async def call_confirmed(tool: str, args: dict[str, Any]) -> dict[str, Any]:
                first = await self._business_value_rpc(
                    client, caller_token, meta, tool, args, intent=intent
                )
                if first.get("resultType") == "input_required":
                    self.write_calls_count += 1
                    return await self._business_value_rpc(
                        client,
                        caller_token,
                        meta,
                        tool,
                        args,
                        request_state=str(first["requestState"]),
                        input_responses={
                            "confirm": {"action": "accept", "content": {"confirm": True}}
                        },
                        intent=intent,
                    )
                return first

            listed = await call_confirmed("global_ticketing__list_tickets", {"region": "EU"})
            await call_confirmed(
                "global_ticketing__assign_ticket",
                {"ticket_id": "TCK-1001", "assignee": "elena.rostova@example.test"},
            )
            await call_confirmed(
                "global_ticketing__add_ticket_comment",
                {
                    "ticket_id": "TCK-1001",
                    "body": "Restarted webhook relay in demo traffic.",
                    "internal": False,
                },
            )
            await call_confirmed(
                "global_ticketing__resolve_ticket",
                {
                    "ticket_id": "TCK-1001",
                    "resolution_summary": "Balanced webhook gateway pool",
                    "root_cause": "connection_pool_exhaustion",
                },
            )
            verified = await call_confirmed(
                "global_ticketing__get_ticket", {"ticket_id": "TCK-1001"}
            )
            ok = (verified.get("result") or verified).get("status") == "resolved"
            return {
                "status": "completed" if ok else "failed",
                "workflow_id": workflow_id,
                "tickets_listed": ((listed.get("result") or listed).get("count") or 0),
            }

    async def _ensure_hr_demo_workflow(self, client: httpx.AsyncClient) -> str | None:
        upstream = self.runtime.registry.get("hr_directory")
        if upstream is None or not upstream.enabled:
            return None
        endpoint = upstream.url or upstream.openapi_spec_url or ""
        if urlsplit(endpoint).hostname not in {"127.0.0.1", "localhost", "mock-backends"}:
            return None

        tenant_id = next(iter(self.runtime.settings.allowed_tenants), None)
        if not tenant_id or self.runtime.dev_keys is None:
            return None
        workflow_slug = "demo_fictional_employee_profile_update"
        audit_slug = "employee_role_audit_and_update"
        workflow_id = f"hr_directory/{workflow_slug}"
        async with self._business_value_workflow_lock:
            current = await self.runtime.store.get_current_published_integration_definition(
                tenant_id, "hr_directory"
            )
            current_def = (current.get("definition") or {}) if current else {}
            existing_wfs = {item.get("id") for item in current_def.get("workflows", [])}
            existing_exps = {exp.get("id") for exp in current_def.get("experiences", [])}
            if (
                current
                and {workflow_slug, audit_slug} <= existing_wfs
                and {"hr_employee_lookup_ui", "hr_employee_inspector_ui"} <= existing_exps
            ):
                return workflow_id

            discovery_identity = self.runtime._dev_discovery_identity()
            await self.runtime.ensure_catalog_for_identity(discovery_identity)
            selected = {
                name: self.runtime.catalog.get_tool(f"hr_directory__{name}")
                for name in ("list_hr_employees", "get_hr_employee", "update_hr_employee")
            }
            if any(tool is None for tool in selected.values()):
                return None
            tools = {name: tool for name, tool in selected.items() if tool is not None}

            if current:
                definition = copy.deepcopy(current["definition"])
                integration = definition["integration"]
                integration["status"] = "draft"
                integration["revision"] = 1
                definition["publication"] = None
            else:
                definition = {
                    "manifest_version": "1.0",
                    "integration": {
                        "id": "hr_directory_demo",
                        "display_name": "HR Directory demo workflows",
                        "upstream_name": "hr_directory",
                        "status": "draft",
                        "revision": 1,
                    },
                    "tool_refs": [],
                    "experiences": [],
                    "workflows": [],
                    "standalone_tool_values": [],
                    "publication": None,
                }
            refs = definition.setdefault("tool_refs", [])
            existing_refs = {reference.get("name") for reference in refs}
            for tool in tools.values():
                if tool.qualified_name not in existing_refs:
                    refs.append(
                        {
                            "name": tool.qualified_name,
                            "schema_sha256": compute_schema_digest([tool]),
                        }
                    )

            definition.setdefault("experiences", [])
            if not any(
                exp.get("id") == "hr_employee_lookup_ui" for exp in definition["experiences"]
            ):
                definition["experiences"].append(
                    {
                        "id": "hr_employee_lookup_ui",
                        "resource_uri": "ui://mcpcodeaway/hr/employee-profile/r1",
                        "renderer_schema": "mcpcodeaway-ui/1",
                        "tool": tools["get_hr_employee"].qualified_name,
                        "fallback": "Use hr_directory__get_hr_employee to inspect an employee.",
                        "input_mapping": {"employee_id": "/form/employee_id"},
                        "components": [
                            {"type": "heading", "text": "Employee Profile"},
                            {
                                "type": "text_input",
                                "id": "employee_id",
                                "label": "Employee ID",
                                "required": True,
                                "max_length": 64,
                            },
                            {"type": "button", "label": "Inspect Profile", "action": "invoke_tool"},
                            {"type": "record", "data": "tool_result"},
                        ],
                        "csp": {"connect_domains": [], "resource_domains": [], "frame_domains": []},
                    }
                )
            if not any(
                exp.get("id") == "hr_employee_inspector_ui" for exp in definition["experiences"]
            ):
                definition["experiences"].append(
                    {
                        "id": "hr_employee_inspector_ui",
                        "resource_uri": "ui://mcpcodeaway/hr/employee-inspector/r1",
                        "renderer_schema": "mcpcodeaway-ui/1",
                        "tool": tools["list_hr_employees"].qualified_name,
                        "fallback": (
                            "Use hr_directory__list_hr_employees to inspect directory staff."
                        ),
                        "input_mapping": {},
                        "components": [
                            {"type": "heading", "text": "HR Directory Inspector"},
                            {
                                "type": "text",
                                "text": (
                                    "Browse seeded employees across finance, engineering, "
                                    "and operations."
                                ),
                            },
                            {"type": "button", "label": "List Employees", "action": "invoke_tool"},
                            {"type": "record", "data": "tool_result"},
                        ],
                        "csp": {"connect_domains": [], "resource_domains": [], "frame_domains": []},
                    }
                )
            definition.setdefault("workflows", [])
            if not any(wf.get("id") == workflow_slug for wf in definition["workflows"]):
                definition["workflows"].append(
                    {
                        "id": workflow_slug,
                        "name": "Update and verify a fictional employee profile",
                        "description": (
                            "List the fictional HR directory, read an employee profile, update the "
                            "employee role, and verify the saved profile with a terminal readback."
                        ),
                        "participating_tools": [
                            tools["list_hr_employees"].qualified_name,
                            tools["get_hr_employee"].qualified_name,
                            tools["update_hr_employee"].qualified_name,
                        ],
                        "required_evidence": [
                            {
                                "tool": tools["list_hr_employees"].qualified_name,
                                "result_when": {"path": "/items", "type": "non_empty_array"},
                            },
                            {
                                "tool": tools["get_hr_employee"].qualified_name,
                                "min_successful_calls": 2,
                                "result_when": {
                                    "path": "/employee_id",
                                    "type": "non_empty_string",
                                },
                            },
                            {
                                "tool": tools["update_hr_employee"].qualified_name,
                                "result_when": {
                                    "path": "/role",
                                    "type": "string",
                                    "equals": "senior_controller",
                                },
                            },
                        ],
                        "completion": {
                            "kind": "terminal_tool_result",
                            "tool": tools["get_hr_employee"].qualified_name,
                            "success_when": {
                                "path": "/role",
                                "type": "string",
                                "equals": "senior_controller",
                            },
                        },
                        "business_value": {
                            "manual_baseline_minutes": 8,
                            "baseline_source": "sampled_manual_measurement",
                            "confidence": "sampled",
                            "loaded_labor_cost_per_hour": 90.0,
                            "currency": "USD",
                            "review_minutes": {"kind": "expected_average", "minutes": 1.0},
                            "correction_minutes": {"kind": "expected_average", "minutes": 0.5},
                            "effective_from": datetime.now(UTC).date().isoformat(),
                            "evidence_note": (
                                "Sampled People-Ops timing study for fictional HR profile updates."
                            ),
                        },
                    }
                )
            if not any(wf.get("id") == audit_slug for wf in definition["workflows"]):
                definition["workflows"].append(
                    {
                        "id": audit_slug,
                        "name": "Employee Role Audit and Update",
                        "description": (
                            "Review directory staffing records, audit role assignments, "
                            "and record a promotion."
                        ),
                        "enabled": True,
                        "origin": "agent_prompt",
                        "intent_examples": [
                            "Audit directory staffing roles and promote a member",
                        ],
                        "participating_tools": [
                            tools["list_hr_employees"].qualified_name,
                            tools["get_hr_employee"].qualified_name,
                            tools["update_hr_employee"].qualified_name,
                        ],
                        "required_evidence": [
                            {
                                "tool": tools["list_hr_employees"].qualified_name,
                                "min_successful_calls": 1,
                                "result_when": {"path": "/items", "type": "non_empty_array"},
                            },
                            {
                                "tool": tools["update_hr_employee"].qualified_name,
                                "min_successful_calls": 1,
                                "result_when": {
                                    "path": "/employee_id",
                                    "type": "non_empty_string",
                                },
                            },
                        ],
                        "completion": {
                            "kind": "verified_followup",
                            "tool": tools["get_hr_employee"].qualified_name,
                            "success_when": {
                                "path": "/role",
                                "type": "non_empty_string",
                            },
                        },
                        "business_value": {
                            "manual_baseline_minutes": 10,
                            "baseline_source": "administrator_estimate",
                            "confidence": "unvalidated",
                            "loaded_labor_cost_per_hour": 90.0,
                            "currency": "USD",
                            "review_minutes": {"kind": "expected_average", "minutes": 1.0},
                            "correction_minutes": {"kind": "expected_average", "minutes": 0.5},
                            "effective_from": datetime.now(UTC).date().isoformat(),
                            "evidence_note": (
                                "People-Ops estimate pending quarterly role audit timing sample."
                            ),
                        },
                    }
                )
            admin_token = self.runtime.dev_keys.issue_user_token(
                persona="gateway_admin",
                overrides={"tenant_id": tenant_id},
            )
            headers = {"Authorization": f"Bearer {admin_token}"}
            draft = await client.post(
                "/admin/integration-definitions/drafts",
                headers=headers,
                json={"definition": definition},
            )
            if draft.status_code != 200:
                raise RuntimeError(f"Could not prepare the demo HR workflow: {draft.text}")
            revision = str(draft.json()["revision"])
            published = await client.post(
                "/admin/integration-definitions/hr_directory/publish",
                headers=headers,
                json={"revision": revision},
            )
            if published.status_code != 200:
                raise RuntimeError(f"Could not publish the demo HR workflow: {published.text}")
            return workflow_id

    async def _ensure_simple_demo_solution(
        self,
        client: httpx.AsyncClient,
        *,
        upstream_name: str,
        display_name: str,
        tool_names: tuple[str, ...],
        experience_spec: dict[str, Any],
        workflow_spec: dict[str, Any],
    ) -> str | None:
        upstream = self.runtime.registry.get(upstream_name)
        if upstream is None or not upstream.enabled:
            return None
        endpoint = upstream.url or upstream.openapi_spec_url or ""
        if urlsplit(endpoint).hostname not in {"127.0.0.1", "localhost", "mock-backends"}:
            return None
        tenant_id = next(iter(self.runtime.settings.allowed_tenants), None)
        if not tenant_id or self.runtime.dev_keys is None:
            return None
        async with self._business_value_workflow_lock:
            current = await self.runtime.store.get_current_published_integration_definition(
                tenant_id, upstream_name
            )
            discovery_identity = self.runtime._dev_discovery_identity()
            await self.runtime.ensure_catalog_for_identity(discovery_identity)
            tools = {
                name: self.runtime.catalog.get_tool(f"{upstream_name}__{name}")
                for name in tool_names
            }
            if any(tool is None for tool in tools.values()):
                return None
            resolved_tools = {name: tool for name, tool in tools.items() if tool is not None}
            completion_tool = resolved_tools[workflow_spec["completion_tool_short"]]
            has_output_props = isinstance(completion_tool.output_schema, dict) and bool(
                completion_tool.output_schema.get("properties")
            )
            current_def = (current.get("definition") or {}) if current else {}
            existing_exps = {exp.get("id") for exp in current_def.get("experiences", [])}
            existing_wfs = {wf.get("id") for wf in current_def.get("workflows", [])}
            needs_wf = has_output_props and workflow_spec["id"] not in existing_wfs
            if current and experience_spec["id"] in existing_exps and not needs_wf:
                return f"{upstream_name}/{workflow_spec['id']}"

            if current:
                definition = copy.deepcopy(current["definition"])
                definition["integration"]["status"] = "draft"
                definition["integration"]["revision"] = 1
                definition["publication"] = None
            else:
                definition = {
                    "manifest_version": "1.0",
                    "integration": {
                        "id": upstream_name,
                        "display_name": display_name,
                        "upstream_name": upstream_name,
                        "status": "draft",
                        "revision": 1,
                    },
                    "tool_refs": [],
                    "experiences": [],
                    "workflows": [],
                    "standalone_tool_values": [],
                    "publication": None,
                }
            refs = definition.setdefault("tool_refs", [])
            existing_refs = {ref.get("name") for ref in refs}
            for tool in resolved_tools.values():
                if tool.qualified_name not in existing_refs:
                    refs.append(
                        {
                            "name": tool.qualified_name,
                            "schema_sha256": compute_schema_digest([tool]),
                        }
                    )
            experiences = definition.setdefault("experiences", [])
            if experience_spec["id"] not in {exp.get("id") for exp in experiences}:
                experiences.append(experience_spec)
            workflows = definition.setdefault("workflows", [])
            if has_output_props and workflow_spec["id"] not in {wf.get("id") for wf in workflows}:
                today = datetime.now(UTC).date().isoformat()
                completion_pred = pick_schema_predicate(completion_tool)
                workflows.append(
                    {
                        "id": workflow_spec["id"],
                        "name": workflow_spec["name"],
                        "description": workflow_spec["description"],
                        "enabled": True,
                        "origin": "agent_prompt",
                        "intent_examples": workflow_spec["intent_examples"],
                        "participating_tools": [
                            resolved_tools[name].qualified_name for name in tool_names
                        ],
                        "required_evidence": [
                            {
                                "tool": resolved_tools[name].qualified_name,
                                "min_successful_calls": 1,
                                **(
                                    {"result_when": pick_schema_predicate(resolved_tools[name])}
                                    if isinstance(resolved_tools[name].output_schema, dict)
                                    and resolved_tools[name].output_schema.get("properties")
                                    else {}
                                ),
                            }
                            for name in tool_names[:2]
                        ],
                        "completion": {
                            "kind": "verified_followup",
                            "tool": completion_tool.qualified_name,
                            "success_when": completion_pred,
                        },
                        "business_value": {
                            "manual_baseline_minutes": workflow_spec["manual_baseline_minutes"],
                            "baseline_source": workflow_spec.get(
                                "baseline_source", "validated_external_measurement"
                            ),
                            "confidence": workflow_spec.get("confidence", "validated"),
                            "loaded_labor_cost_per_hour": workflow_spec.get(
                                "loaded_labor_cost_per_hour", 110.0
                            ),
                            "currency": workflow_spec.get("currency", "USD"),
                            "review_minutes": {
                                "kind": "expected_average",
                                "minutes": workflow_spec.get("review_minutes", 1.0),
                            },
                            "correction_minutes": {
                                "kind": "expected_average",
                                "minutes": workflow_spec.get("correction_minutes", 0.5),
                            },
                            "effective_from": today,
                            "evidence_note": workflow_spec["evidence_note"],
                        },
                    }
                )
            admin_token = self.runtime.dev_keys.issue_user_token(
                persona="gateway_admin",
                overrides={"tenant_id": tenant_id},
            )
            headers = {"Authorization": f"Bearer {admin_token}"}
            draft = await client.post(
                "/admin/integration-definitions/drafts",
                headers=headers,
                json={"definition": definition},
            )
            if draft.status_code != 200:
                return None
            revision = str(draft.json()["revision"])
            published = await client.post(
                f"/admin/integration-definitions/{upstream_name}/publish",
                headers=headers,
                json={"revision": revision},
            )
            if published.status_code != 200:
                return None
            return f"{upstream_name}/{workflow_spec['id']}"

    async def _ensure_crm_demo_solutions(self, client: httpx.AsyncClient) -> str | None:
        return await self._ensure_simple_demo_solution(
            client,
            upstream_name="crm_accounts",
            display_name="Enterprise CRM Accounts",
            tool_names=(
                "list_crm_accounts",
                "list_crm_accounts_summary",
                "list_crm_accounts_metrics",
            ),
            experience_spec={
                "id": "crm_account_summary_ui",
                "resource_uri": "ui://mcpcodeaway/crm_accounts/account_summary/r1",
                "renderer_schema": "mcpcodeaway-ui/1",
                "tool": "crm_accounts__list_crm_accounts",
                "fallback": (
                    "Inspect enterprise CRM accounts using crm_accounts__list_crm_accounts."
                ),
                "input_mapping": {},
                "components": [
                    {"type": "heading", "text": "Enterprise CRM Account Summary"},
                    {
                        "type": "text",
                        "text": "Review enterprise ARR accounts, tiers, and live health metrics.",
                    },
                    {"type": "button", "label": "Load CRM Accounts", "action": "invoke_tool"},
                    {"type": "record", "data": "tool_result"},
                ],
                "csp": {"connect_domains": [], "resource_domains": [], "frame_domains": []},
            },
            workflow_spec={
                "id": "account_risk_review",
                "name": "Account Risk Review",
                "description": (
                    "Inspect enterprise CRM accounts, review summary totals, and verify "
                    "account metrics."
                ),
                "intent_examples": [
                    "Review enterprise CRM accounts for risk and ARR concentration",
                ],
                "completion_tool_short": "list_crm_accounts_metrics",
                "manual_baseline_minutes": 12,
                "baseline_source": "validated_external_measurement",
                "confidence": "validated",
                "loaded_labor_cost_per_hour": 110.0,
                "currency": "USD",
                "review_minutes": 1.5,
                "correction_minutes": 0.5,
                "evidence_note": (
                    "Validated RevOps benchmark across 40 enterprise QBR account risk reviews."
                ),
            },
        )

    async def _ensure_devops_demo_solutions(self, client: httpx.AsyncClient) -> str | None:
        return await self._ensure_simple_demo_solution(
            client,
            upstream_name="devops_ci",
            display_name="DevOps CI/CD Pipelines",
            tool_names=(
                "list_ci_deployments",
                "list_ci_deployments_health",
                "list_ci_deployments_metrics",
            ),
            experience_spec={
                "id": "devops_build_status_ui",
                "resource_uri": "ui://mcpcodeaway/devops_ci/build_status/r1",
                "renderer_schema": "mcpcodeaway-ui/1",
                "tool": "devops_ci__list_ci_deployments",
                "fallback": (
                    "Inspect recent CI/CD deployments using devops_ci__list_ci_deployments."
                ),
                "input_mapping": {},
                "components": [
                    {"type": "heading", "text": "DevOps CI/CD Build Status"},
                    {
                        "type": "text",
                        "text": "Inspect recent production deployments, SHAs, and health status.",
                    },
                    {"type": "button", "label": "Refresh Deployments", "action": "invoke_tool"},
                    {"type": "record", "data": "tool_result"},
                ],
                "csp": {"connect_domains": [], "resource_domains": [], "frame_domains": []},
            },
            workflow_spec={
                "id": "pipeline_incident_triage",
                "name": "Pipeline Incident Triage",
                "description": (
                    "Inspect active CI/CD deployments, check service health, and review "
                    "deployment latency metrics."
                ),
                "intent_examples": [
                    "Triage CI/CD deployment status and check pipeline health",
                ],
                "completion_tool_short": "list_ci_deployments_health",
                "manual_baseline_minutes": 10,
                "baseline_source": "validated_external_measurement",
                "confidence": "validated",
                "loaded_labor_cost_per_hour": 120.0,
                "currency": "USD",
                "review_minutes": 1.0,
                "correction_minutes": 0.5,
                "evidence_note": (
                    "Validated DevOps on-call postmortem timing benchmark across 28 deployments."
                ),
            },
        )

    async def _run_hr_business_value_workflow(self) -> dict[str, Any]:
        if self._app is None or self.runtime.dev_keys is None:
            return {"status": "skipped", "reason": "gateway demo runtime is unavailable"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app), base_url="http://demo.local"
        ) as client:
            workflow_id = await self._ensure_hr_demo_workflow(client)
            if workflow_id is None:
                return {"status": "skipped", "reason": "local hr_directory is unavailable"}

            tenant_id = next(iter(self.runtime.settings.allowed_tenants), None)
            if not tenant_id:
                return {"status": "skipped", "reason": "demo tenant is unavailable"}
            caller_token = self.runtime.dev_keys.issue_user_token(
                persona="gateway_admin",
                overrides={
                    "tenant_id": tenant_id,
                    "sub": "demo-hr-operator",
                    "roles": ["hr_ops", "hr_writer", "gateway_admin", "admin"],
                    "groups": ["hr", "platform-admins"],
                    "scopes": ["mcp.tools", "mcp.write"],
                },
            )
            meta = request_meta(client_name="mcpcodeaway-demo-traffic")
            capabilities = meta[MCP_CLIENT_CAPABILITIES_META]
            capabilities["elicitation"] = {"form": {}}
            calls_before = self.requests_sent
            write_count_before = self.write_calls_count

            demo_intent = (
                "Update fictional employee EMP-01 role to senior_controller and verify profile"
            )

            async def call_tool(
                name: str,
                arguments: dict[str, Any],
                *,
                request_state: str | None = None,
                input_responses: dict[str, Any] | None = None,
            ) -> dict[str, Any]:
                return await self._business_value_rpc(
                    client,
                    caller_token,
                    meta,
                    name,
                    arguments,
                    request_state=request_state,
                    input_responses=input_responses,
                    intent=demo_intent,
                )

            listing_resp = await call_tool("hr_directory__list_hr_employees", {"limit": 10})
            listing = listing_resp.get("result", listing_resp)
            before_resp = await call_tool(
                "hr_directory__get_hr_employee", {"employee_id": "EMP-01"}
            )
            before = before_resp.get("result", before_resp)
            update_arguments = {"employee_id": "EMP-01", "role": "senior_controller"}
            self.write_calls_count += 1
            update_name = "hr_directory__update_hr_employee"
            requested = await call_tool(update_name, update_arguments)
            state = requested.get("requestState")
            if requested.get("resultType") != "input_required" or not isinstance(state, str):
                return {
                    "status": "failed",
                    "workflow_id": workflow_id,
                    "reason": "demo client did not receive the expected write confirmation",
                    "requests": self.requests_sent - calls_before,
                }
            update_resp = await call_tool(
                update_name,
                update_arguments,
                request_state=state,
                input_responses={"confirm": {"action": "accept", "content": {"confirm": True}}},
            )
            update = update_resp.get("result", update_resp)
            verified_resp = await call_tool(
                "hr_directory__get_hr_employee", {"employee_id": "EMP-01"}
            )
            verified = verified_resp.get("result", verified_resp)
            detection = verified_resp.get("workflow_detection") or {}
            episode_id = str(detection.get("episode_id") or "")
            successful = (
                listing.get("count", 0) >= 1
                and before.get("employee_id") == "EMP-01"
                and update.get("role") == "senior_controller"
                and verified.get("role") == "senior_controller"
                and detection.get("status") == "completed_verified"
            )
            events = await self.runtime.store.list_business_value_events(tenant_id)
            credited = [
                event
                for event in events
                if event.get("business_execution_id") == episode_id and event.get("credited")
            ]
            app_meta = request_meta(client_name="mcpcodeaway-demo-traffic")
            app_meta[MCP_CLIENT_CAPABILITIES_META]["extensions"] = {
                MCP_APP_EXTENSION: {"mimeTypes": [MCP_APP_MIME_TYPE]}
            }
            res_list = await client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {caller_token}",
                    **request_headers("resources/list"),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "demo-hr-resources-list",
                    "method": "resources/list",
                    "params": {"_meta": app_meta},
                },
            )
            listed_uris = (
                [r.get("uri") for r in (res_list.json().get("result") or {}).get("resources", [])]
                if res_list.status_code == 200
                else []
            )
            return {
                "status": "completed" if successful and credited else "failed",
                "workflow_id": workflow_id,
                "episode_id": episode_id,
                "attribution_mode": "passive_matched",
                "completion_count": len(credited),
                "manual_baseline_minutes": 8,
                "requests": self.requests_sent - calls_before,
                "write_calls": self.write_calls_count - write_count_before,
                "mcp_app_ui_verified": "ui://mcpcodeaway/hr/employee-profile/r1" in listed_uris,
                "confirmation": "accepted by the demo caller through MCP form elicitation",
            }

    async def _run_passive_workflow_evidences(self) -> dict[str, Any]:
        """Run bounded demo traffic across CRM, DevOps, Ticketing, and HR workflows."""
        if self._app is None or self.runtime.dev_keys is None:
            return {"status": "skipped", "completed": []}
        tenant_id = next(iter(self.runtime.settings.allowed_tenants), None)
        if not tenant_id:
            return {"status": "skipped", "completed": []}
        completed: list[str] = []
        errors: dict[str, str] = {}
        caller_token = self.runtime.dev_keys.issue_user_token(
            persona="gateway_admin",
            overrides={
                "tenant_id": tenant_id,
                "sub": "demo-workflow-operator",
                "roles": [
                    "admin",
                    "gateway_admin",
                    "ops_engineer",
                    "ticketing_reader",
                    "ticketing_writer",
                    "hr_ops",
                    "hr_writer",
                    "sales_ops",
                    "finance_analyst",
                    "platform_eng",
                    "sre_oncall",
                ],
                "groups": [
                    "operations",
                    "platform-admins",
                    "hr",
                    "engineering",
                    "platform",
                    "sales",
                    "finance",
                ],
                "client_groups": ["US"],
                "scopes": ["mcp.tools", "ops.write", "mcp.write"],
            },
        )
        meta = request_meta(client_name="mcpcodeaway-demo-traffic")
        meta[MCP_CLIENT_CAPABILITIES_META]["elicitation"] = {"form": {}}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app), base_url="http://demo.local"
        ) as client:

            async def run_codemode_flow(label: str, intent: str, code: str) -> None:
                try:
                    resp = await client.post(
                        "/mcp?mode=codemode",
                        headers={
                            "Authorization": f"Bearer {caller_token}",
                            "X-Intent": intent,
                            **request_headers("tools/call", tool_name="execute_code"),
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": f"demo-wf-{uuid4().hex[:8]}",
                            "method": "tools/call",
                            "params": {
                                "_meta": meta,
                                "name": "execute_code",
                                "arguments": {"code": code},
                            },
                        },
                    )
                    body = resp.json() if resp.status_code == 200 else {}
                    res = body.get("result") if isinstance(body, dict) else None
                    if (
                        resp.status_code == 200
                        and not body.get("error")
                        and isinstance(res, dict)
                        and not res.get("isError")
                    ):
                        completed.append(label)
                    else:
                        errors[label] = str(body.get("error") or res or f"HTTP {resp.status_code}")
                except Exception as exc:
                    errors[label] = str(exc)

            async def call_step(
                tool_name: str,
                arguments: dict[str, Any],
                *,
                intent: str,
            ) -> dict[str, Any]:
                first = await self._business_value_rpc(
                    client,
                    caller_token,
                    meta,
                    tool_name,
                    arguments,
                    intent=intent,
                    track_counters=False,
                )
                if first.get("resultType") == "input_required":
                    return await self._business_value_rpc(
                        client,
                        caller_token,
                        meta,
                        tool_name,
                        arguments,
                        request_state=str(first["requestState"]),
                        input_responses={
                            "confirm": {"action": "accept", "content": {"confirm": True}}
                        },
                        intent=intent,
                        track_counters=False,
                    )
                return first

            if self.runtime.registry.get("crm_accounts"):
                await self._ensure_crm_demo_solutions(client)
                await run_codemode_flow(
                    "crm_accounts/account_risk_review",
                    "Review enterprise CRM accounts for risk and ARR concentration",
                    (
                        "accounts = await crm_accounts__list_crm_accounts(limit=10)\n"
                        "summary = await crm_accounts__list_crm_accounts_summary()\n"
                        "metrics = await crm_accounts__list_crm_accounts_metrics()\n"
                        "{'accounts': accounts.get('count', 0), "
                        "'status': summary.get('status'), "
                        "'active': metrics.get('active_items', 0)}"
                    ),
                )

            if self.runtime.registry.get("devops_ci"):
                await self._ensure_devops_demo_solutions(client)
                await run_codemode_flow(
                    "devops_ci/pipeline_incident_triage",
                    "Triage CI/CD deployment status and check pipeline health",
                    (
                        "deployments = await devops_ci__list_ci_deployments(limit=10)\n"
                        "health = await devops_ci__list_ci_deployments_health()\n"
                        "metrics = await devops_ci__list_ci_deployments_metrics()\n"
                        "{'deployments': deployments.get('count', 0), "
                        "'healthy': health.get('healthy', False), "
                        "'latency_p50_ms': metrics.get('latency_p50_ms', 0)}"
                    ),
                )

            if self.runtime.registry.get("global_ticketing"):
                await self._ensure_ticketing_demo_workflows(client)
                try:
                    sla_intent = "Check SLA timer and escalate urgent ticket to critical"
                    await call_step(
                        "global_ticketing__get_ticket_sla",
                        {"ticket_id": "TCK-1002"},
                        intent=sla_intent,
                    )
                    await call_step(
                        "global_ticketing__escalate_ticket",
                        {
                            "ticket_id": "TCK-1002",
                            "escalation_tier": "tier3_incident",
                            "reason": "APAC vault latency nearing SLA limit",
                        },
                        intent=sla_intent,
                    )
                    await call_step(
                        "global_ticketing__get_ticket",
                        {"ticket_id": "TCK-1002"},
                        intent=sla_intent,
                    )
                    completed.append("global_ticketing/ticket_escalation_and_sla_review")
                except Exception as exc:
                    errors["global_ticketing/ticket_escalation_and_sla_review"] = str(exc)

                try:
                    intake_intent = "List regional queues, create ticket, and assign owner"
                    await call_step(
                        "global_ticketing__list_ticketing_queues",
                        {"region": "EU"},
                        intent=intake_intent,
                    )
                    created = await call_step(
                        "global_ticketing__create_ticket",
                        {
                            "subject": "Settlement webhook timeout burst",
                            "description": "EU merchant webhook workers saw 504 spikes.",
                            "region": "EU",
                            "queue_id": "Q-EU-OPS",
                            "priority": "high",
                        },
                        intent=intake_intent,
                    )
                    created_id = str(
                        (created.get("result") or created).get("ticket_id") or "TCK-1005"
                    )
                    await call_step(
                        "global_ticketing__assign_ticket",
                        {
                            "ticket_id": created_id,
                            "assignee": "elena.rostova@example.test",
                            "team": "tier2_sre",
                        },
                        intent=intake_intent,
                    )
                    completed.append("global_ticketing/intake_and_route_new_ticket")
                except Exception as exc:
                    errors["global_ticketing/intake_and_route_new_ticket"] = str(exc)

            if self.runtime.registry.get("hr_directory"):
                try:
                    audit_intent = "Audit directory staffing roles and promote a member"
                    await call_step(
                        "hr_directory__list_hr_employees",
                        {"limit": 10},
                        intent=audit_intent,
                    )
                    await call_step(
                        "hr_directory__update_hr_employee",
                        {"employee_id": "EMP-01", "role": "principal_engineer"},
                        intent=audit_intent,
                    )
                    await call_step(
                        "hr_directory__get_hr_employee",
                        {"employee_id": "EMP-01"},
                        intent=audit_intent,
                    )
                    completed.append("hr_directory/employee_role_audit_and_update")
                except Exception as exc:
                    errors["hr_directory/employee_role_audit_and_update"] = str(exc)

        return {"status": "completed", "completed": completed, "errors": errors}

    async def _business_value_rpc(
        self,
        client: httpx.AsyncClient,
        token: str,
        meta: dict[str, Any],
        name: str,
        arguments: dict[str, Any],
        *,
        request_state: str | None = None,
        input_responses: dict[str, Any] | None = None,
        intent: str | None = None,
        track_counters: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"_meta": meta, "name": name, "arguments": arguments}
        if request_state is not None:
            params["requestState"] = request_state
        if input_responses is not None:
            params["inputResponses"] = input_responses
        headers = {
            "Authorization": f"Bearer {token}",
            **request_headers("tools/call", tool_name=name),
        }
        if intent:
            headers["X-Intent"] = intent
        response = await client.post(
            "/mcp?mode=traditional",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": f"demo-business-{uuid4().hex}",
                "method": "tools/call",
                "params": params,
            },
        )
        if track_counters:
            self.requests_sent += 1
            self.last_target = f"business_value:{name}"
        if response.status_code >= 400:
            if track_counters:
                self.errors_count += 1
                self.http_errors_count += 1
                self.last_error = f"HTTP {response.status_code} on business-value demo workflow"
            raise RuntimeError(f"HTTP {response.status_code} on business-value demo workflow")
        body = response.json()
        rpc_error = body.get("error") if isinstance(body, dict) else None
        if rpc_error:
            msg = str(rpc_error.get("message") or "MCP workflow call failed")
            if track_counters:
                self.errors_count += 1
                self.rpc_errors_count += 1
                self.last_error = msg
            raise RuntimeError(msg)
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict) or result.get("isError"):
            if track_counters:
                self.errors_count += 1
                self.rpc_errors_count += 1
                self.last_error = "MCP business-value workflow call returned an error"
            raise RuntimeError("MCP business-value workflow call returned an error")
        if track_counters:
            self.last_error = ""
        structured = result.get("structuredContent")
        return structured if isinstance(structured, dict) else result

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
