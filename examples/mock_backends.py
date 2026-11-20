"""Mock enterprise MCP and OpenAPI backends enforcing downstream delegated JWTs and RBAC."""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Callable
from typing import Any

import httpx
import jwt
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from jwt.algorithms import RSAAlgorithm
from pydantic import BaseModel

from gateway.mcp_protocol import (
    MCP_CLIENT_CAPABILITIES_META,
    MCP_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSION_META,
    MCP_SERVER_INFO_META,
)


class CreateCollectionCaseRequest(BaseModel):
    customer_id: str
    reason: str
    priority: str = "high"


class InvoiceItem(BaseModel):
    invoice_id: str
    customer_id: str
    amount: float
    currency: str
    status: str
    days_past_due: int
    merchant_name: str | None = None
    cost_center: str | None = None
    payment_terms: str | None = None


class InvoiceListResponse(BaseModel):
    region: str
    count: int
    invoices: list[InvoiceItem]


class CustomerRecord(BaseModel):
    customer_id: str
    name: str
    region: str
    contact_email: str
    tier: str


def _seed_enterprise_data() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, dict[str, Any]]],
    list[dict[str, Any]],
]:
    """Build deterministic finance, billing, and ops datasets with intentional discrepancies."""
    debt_ledger: list[dict[str, Any]] = []
    trial_balance: list[dict[str, Any]] = []
    invoices_by_region: dict[str, list[dict[str, Any]]] = {"EU": [], "US": []}
    customers_by_region: dict[str, dict[str, dict[str, Any]]] = {"EU": {}, "US": {}}

    discrepancy_map = {3: 450.0, 11: 1200.0, 24: -320.0}

    for idx in range(1, 31):
        cid = f"CUST-{1000 + idx}"
        region = "EU" if idx <= 15 else "US"
        overdue = idx % 3 == 0
        billing_amount = float(1000 + idx * 125)
        ledger_delta = discrepancy_map.get(idx, 0.0)
        ledger_amount = billing_amount + ledger_delta
        days_past_due = 45 + (idx % 5) * 15 if overdue else 0

        debt_ledger.append(
            {
                "customer_id": cid,
                "region": region,
                "ledger_balance": ledger_amount,
                "currency": "EUR" if region == "EU" else "USD",
                "status": "overdue" if overdue else "current",
                "days_past_due": days_past_due,
            }
        )
        trial_balance.append(
            {
                "account_code": f"AR-{region}-{100 + idx}",
                "customer_id": cid,
                "debit": ledger_amount,
                "credit": billing_amount,
                "variance": round(ledger_amount - billing_amount, 2),
            }
        )
        customers_by_region[region][cid] = {
            "customer_id": cid,
            "name": f"Merchant {idx} ({region})",
            "region": region,
            "contact_email": f"ap@merchant{idx}.example",
            "tier": "enterprise" if idx % 4 == 0 else "growth",
        }
        invoices_by_region[region].append(
            {
                "invoice_id": f"INV-{region}-{2000 + idx}",
                "customer_id": cid,
                "amount": billing_amount,
                "currency": "EUR" if region == "EU" else "USD",
                "status": "overdue" if overdue else "paid",
                "days_past_due": days_past_due,
            }
        )

    terminal_incidents = [
        {
            "incident_id": "INC-901",
            "terminal_id": "TERM-EU-44",
            "customer_id": "CUST-1003",
            "severity": "high",
            "issue": "Settlement batch timeout",
            "status": "open",
        },
        {
            "incident_id": "INC-902",
            "terminal_id": "TERM-US-18",
            "customer_id": "CUST-1024",
            "severity": "medium",
            "issue": "Offline pinpad firmware mismatch",
            "status": "open",
        },
        {
            "incident_id": "INC-903",
            "terminal_id": "TERM-EU-09",
            "customer_id": "CUST-1006",
            "severity": "low",
            "issue": "Intermittent heartbeat drop",
            "status": "resolved",
        },
    ]
    return debt_ledger, trial_balance, invoices_by_region, customers_by_region, terminal_incidents


def _seed_bulk_data() -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Build 200 overdue customers (100 EU + 100 US) with 3 intentional variance rows."""
    bulk_ledger: list[dict[str, Any]] = []
    bulk_invoices: dict[str, list[dict[str, Any]]] = {"EU": [], "US": []}
    discrepancy_map = {41: 615.0, 118: -480.0, 199: 1250.0}

    for idx in range(1, 201):
        cid = f"CUST-{2000 + idx}"
        region = "EU" if idx <= 100 else "US"
        billing_amount = float(1500 + (idx % 25) * 85)
        ledger_delta = discrepancy_map.get(idx, 0.0)
        ledger_amount = billing_amount + ledger_delta
        days_past_due = 30 + (idx % 6) * 15
        bulk_ledger.append(
            {
                "customer_id": cid,
                "merchant_name": f"Global Merchant {idx:03d} ({region})",
                "region": region,
                "ledger_balance": ledger_amount,
                "currency": "EUR" if region == "EU" else "USD",
                "status": "overdue",
                "days_past_due": days_past_due,
                "cost_center": f"CC-{region}-{(idx % 7) + 1:02d}",
                "payment_terms": "NET30",
            }
        )
        bulk_invoices[region].append(
            {
                "invoice_id": f"INV-{region}-{5000 + idx}",
                "customer_id": cid,
                "merchant_name": f"Global Merchant {idx:03d} ({region})",
                "amount": billing_amount,
                "currency": "EUR" if region == "EU" else "USD",
                "status": "overdue",
                "days_past_due": days_past_due,
                "cost_center": f"CC-{region}-{(idx % 7) + 1:02d}",
                "payment_terms": "NET30",
            }
        )
    return bulk_ledger, bulk_invoices


def build_synthetic_tools(count: int) -> list[dict[str, Any]]:
    """Deterministically generate synthetic MCP tools for scaling benchmarks."""
    domains = ("ledger", "billing", "erp", "crm", "treasury")
    tools: list[dict[str, Any]] = []
    for idx in range(count):
        domain = domains[idx % len(domains)]
        tools.append(
            {
                "name": f"{domain}_metric_query_{idx:03d}",
                "description": (
                    f"Query synthetic {domain} operational records and aggregations "
                    f"for segment {idx:03d}."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "customer_id": {"type": "string", "description": "Customer ID"},
                        "limit": {"type": "integer", "default": 25},
                        "include_metadata": {"type": "boolean", "default": False},
                    },
                    "required": ["customer_id"],
                },
                "annotations": {"readOnlyHint": True, "destructiveHint": False},
            }
        )
    return tools


class _DownstreamTokenVerifier:
    """Validates delegated Bearer JWTs against static PEM or live JWKS endpoint."""

    def __init__(
        self,
        *,
        public_key_pem: str | Callable[[], str] | None,
        jwks_url: str,
        expected_issuer: str,
    ) -> None:
        self._public_key_pem = public_key_pem
        self._jwks_url = jwks_url
        self._expected_issuer = expected_issuer
        self._cached_jwks: dict[str, Any] | None = None

    async def _resolve_key(self, token: str) -> Any:
        if self._public_key_pem is not None:
            return (
                self._public_key_pem() if callable(self._public_key_pem) else self._public_key_pem
            )
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if self._cached_jwks is None:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(self._jwks_url)
                resp.raise_for_status()
                self._cached_jwks = resp.json()
        keys = (self._cached_jwks or {}).get("keys", [])
        match = next((k for k in keys if k.get("kid") == kid), keys[0] if len(keys) == 1 else None)
        if match is None:
            raise HTTPException(status_code=401, detail="Signing key not found")
        return RSAAlgorithm.from_jwk(json.dumps(match))

    async def verify(
        self,
        authorization: str | None,
        *,
        expected_audience: str,
        required_roles: set[str],
    ) -> dict[str, Any]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing downstream Bearer token")
        token = authorization.split(" ", 1)[1].strip()
        try:
            key = await self._resolve_key(token)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=expected_audience,
                issuer=self._expected_issuer,
                options={"require": ["exp", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail=f"Invalid downstream token: {exc}") from exc

        roles = set(claims.get("roles", []))
        if required_roles and not (roles & required_roles):
            raise HTTPException(
                status_code=403,
                detail=f"Caller lacks any required role {sorted(required_roles)}",
            )
        return claims


def _build_billing_subapp(
    *,
    region: str,
    audience: str,
    invoices: list[dict[str, Any]],
    bulk_invoices: list[dict[str, Any]],
    customers: dict[str, dict[str, Any]],
    verifier: _DownstreamTokenVerifier,
) -> FastAPI:
    subapp = FastAPI(
        title=f"Legacy Billing API ({region})",
        version="1.0.0",
        servers=[{"url": f"/billing-{region.lower()}"}],
    )
    required = {"billing_reader", "finance_analyst", "admin"}

    @subapp.get(
        "/invoices",
        operation_id=f"list_{region.lower()}_invoices",
        response_model=InvoiceListResponse,
    )
    async def list_invoices(
        status: str | None = Query(default=None, description="Filter by invoice status"),
        dataset: str = Query(
            default="standard", description="Dataset size: 'standard' or 'bulk' (100 rows)"
        ),
        authorization: str | None = Header(default=None),
    ) -> InvoiceListResponse:
        claims = await verifier.verify(
            authorization, expected_audience=audience, required_roles=required
        )
        allowed_groups = {str(g) for g in (claims.get("client_groups") or []) if g}
        if allowed_groups and region not in allowed_groups:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Caller client_groups={sorted(allowed_groups)} cannot access region {region}"
                ),
            )
        source = bulk_invoices if dataset == "bulk" else invoices
        filtered = [inv for inv in source if status is None or inv["status"] == status]
        return InvoiceListResponse(
            region=region,
            count=len(filtered),
            invoices=[InvoiceItem(**inv) for inv in filtered],
        )

    @subapp.get(
        "/customers/{customer_id}",
        operation_id=f"get_{region.lower()}_customer",
        response_model=CustomerRecord,
    )
    async def get_customer(
        customer_id: str,
        authorization: str | None = Header(default=None),
    ) -> CustomerRecord:
        claims = await verifier.verify(
            authorization, expected_audience=audience, required_roles=required
        )
        allowed_groups = {str(g) for g in (claims.get("client_groups") or []) if g}
        if allowed_groups and region not in allowed_groups:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Caller client_groups={sorted(allowed_groups)} cannot access region {region}"
                ),
            )
        customer = customers.get(customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail=f"Customer '{customer_id}' not found")
        return CustomerRecord(**customer)

    return subapp


def _jsonrpc_result(req_id: Any, result: Any) -> JSONResponse:
    if isinstance(result, dict):
        result.setdefault("resultType", "complete")
        result.setdefault("_meta", {MCP_SERVER_INFO_META: {"name": "mock-mcp", "version": "1.0.0"}})
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def _jsonrpc_error(req_id: Any, code: int, message: str, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        status_code=status_code,
    )


async def _handle_mcp_rpc(
    request: Request,
    *,
    server_name: str,
    tools: list[dict[str, Any]],
    call_fn: Callable[[str, dict[str, Any]], Any],
) -> JSONResponse:
    """Strict mock of the current sessionless Streamable HTTP contract."""
    payload = await request.json()
    req_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    meta = params.get("_meta") or {}
    if (
        payload.get("jsonrpc") != "2.0"
        or not isinstance(req_id, (str, int))
        or isinstance(req_id, bool)
        or meta.get(MCP_PROTOCOL_VERSION_META) != MCP_PROTOCOL_VERSION
        or not isinstance(meta.get(MCP_CLIENT_CAPABILITIES_META), dict)
        or request.headers.get("mcp-protocol-version") != MCP_PROTOCOL_VERSION
        or request.headers.get("mcp-method") != method
    ):
        return _jsonrpc_error(req_id, -32020, "Invalid current MCP request framing", 400)
    if request.headers.get("mcp-session-id"):
        return _jsonrpc_error(req_id, -32020, "MCP protocol sessions are not supported", 400)
    if method == "server/discover":
        return _jsonrpc_result(
            req_id,
            {
                "supportedVersions": [MCP_PROTOCOL_VERSION],
                "capabilities": {"tools": {}},
                "ttlMs": 30000,
                "cacheScope": "private",
            },
        )
    if method == "tools/list":
        return _jsonrpc_result(req_id, {"tools": tools, "ttlMs": 0, "cacheScope": "private"})
    if method == "tools/call":
        name = str(params.get("name") or "")
        header_name = request.headers.get("mcp-name") or ""
        if header_name.startswith("=?base64?") and header_name.endswith("?="):
            try:
                header_name = base64.b64decode(header_name[9:-2], validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return _jsonrpc_error(req_id, -32020, "Invalid Mcp-Name", 400)
        if header_name != name:
            return _jsonrpc_error(req_id, -32020, "Mcp-Name does not match tool name", 400)
        args = params.get("arguments") or {}
        try:
            data = call_fn(name, args)
        except KeyError as exc:
            return _jsonrpc_error(req_id, -32601, str(exc))
        except ValueError as exc:
            return _jsonrpc_error(req_id, -32602, str(exc))
        return _jsonrpc_result(
            req_id,
            {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data},
        )
    return _jsonrpc_error(req_id, -32601, f"Unsupported method '{method}'")


def create_mock_backends_app(
    public_key_pem: str | Callable[[], str] | None = None,
    expected_issuer: str = "http://127.0.0.1:8000/dev/oidc",
    jwks_url: str | None = None,
) -> FastAPI:
    """Create isolated mock enterprise MCP and OpenAPI services for demos and tests."""
    resolved_jwks_url = jwks_url or os.getenv(
        "GATEWAY_JWKS_URL", "http://127.0.0.1:8000/dev/oidc/jwks"
    )
    verifier = _DownstreamTokenVerifier(
        public_key_pem=public_key_pem,
        jwks_url=resolved_jwks_url,
        expected_issuer=expected_issuer,
    )
    debt_ledger, trial_balance, invoices_by_region, customers_by_region, incidents = (
        _seed_enterprise_data()
    )
    bulk_ledger, bulk_invoices_by_region = _seed_bulk_data()
    collection_cases: list[dict[str, Any]] = []

    app = FastAPI(title="Enterprise Mock Backends", version="0.1.0")

    snowflake_tools = [
        {
            "name": "query_debt_ledger",
            "description": (
                "Query Snowflake debt ledger rows with optional status, region, or dataset filter."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "region": {"type": "string"},
                    "dataset": {
                        "type": "string",
                        "description": (
                            "Optional dataset: 'standard' (default) or 'bulk' (200 rows)."
                        ),
                    },
                },
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "queried_by": {"type": "string"},
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "customer_id": {"type": "string"},
                                "region": {"type": "string"},
                                "ledger_balance": {"type": "number"},
                                "currency": {"type": "string"},
                                "status": {"type": "string"},
                                "days_past_due": {"type": "integer"},
                            },
                        },
                    },
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "get_customer_balance",
            "description": "Fetch a customer's current debt ledger balance from Snowflake.",
            "inputSchema": {
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "region": {"type": "string"},
                    "ledger_balance": {"type": "number"},
                    "currency": {"type": "string"},
                    "status": {"type": "string"},
                    "days_past_due": {"type": "integer"},
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "get_trial_balance_lines",
            "description": "Fetch trial balance reconciliation lines with variance details.",
            "inputSchema": {
                "type": "object",
                "properties": {"only_variances": {"type": "boolean", "default": False}},
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "account_code": {"type": "string"},
                                "customer_id": {"type": "string"},
                                "debit": {"type": "number"},
                                "credit": {"type": "number"},
                                "variance": {"type": "number"},
                            },
                        },
                    }
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "compute_interest_penalty",
            "description": (
                "Compute compound overdue late-fee penalty and total due for a customer balance."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "principal": {"type": "number"},
                    "days_past_due": {"type": "integer"},
                    "annual_rate_pct": {"type": "number", "default": 18.0},
                },
                "required": ["customer_id", "principal", "days_past_due"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "principal": {"type": "number"},
                    "days_past_due": {"type": "integer"},
                    "penalty_amount": {"type": "number"},
                    "total_due": {"type": "number"},
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
    ]

    ops_tools = [
        {
            "name": "list_terminal_incidents",
            "description": "List payment terminal incidents across EU and US fleets.",
            "inputSchema": {
                "type": "object",
                "properties": {"status": {"type": "string"}},
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "incidents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "incident_id": {"type": "string"},
                                "terminal_id": {"type": "string"},
                                "customer_id": {"type": "string"},
                                "severity": {"type": "string"},
                                "issue": {"type": "string"},
                                "status": {"type": "string"},
                            },
                        },
                    }
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "get_terminal_health",
            "description": "Inspect health telemetry for a specific terminal ID.",
            "inputSchema": {
                "type": "object",
                "properties": {"terminal_id": {"type": "string"}},
                "required": ["terminal_id"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "terminal_id": {"type": "string"},
                    "online": {"type": "boolean"},
                    "active_incident": {"type": "object"},
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "create_collection_case",
            "description": "Create an outbound collections escalation case for a customer.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "priority": {"type": "string", "default": "high"},
                },
                "required": ["customer_id", "reason"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "customer_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "priority": {"type": "string"},
                    "created_by": {"type": "string"},
                },
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "search_sentry_errors",
            "description": (
                "Search Sentry production error stack traces and release SHAs by terminal_id."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"terminal_id": {"type": "string"}},
                "required": ["terminal_id"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "terminal_id": {"type": "string"},
                    "release_sha": {"type": "string"},
                    "error_type": {"type": "string"},
                    "culprit": {"type": "string"},
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "get_github_release_commit",
            "description": "Look up GitHub PR author and commit metadata by release_sha.",
            "inputSchema": {
                "type": "object",
                "properties": {"release_sha": {"type": "string"}},
                "required": ["release_sha"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "release_sha": {"type": "string"},
                    "pr_number": {"type": "integer"},
                    "author": {"type": "string"},
                    "repo": {"type": "string"},
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "post_slack_incident_alert",
            "description": "Post an incident triage alert summary to an operations Slack channel.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "case_id": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["channel", "case_id", "summary"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "message_ts": {"type": "string"},
                    "channel": {"type": "string"},
                    "case_id": {"type": "string"},
                    "delivered": {"type": "boolean"},
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
    ]

    @app.post("/snowflake/mcp")
    async def snowflake_mcp(
        request: Request, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        claims = await verifier.verify(
            authorization,
            expected_audience="api://snowflake-ledger",
            required_roles={"snowflake_reader", "finance_analyst", "readonly_auditor", "admin"},
        )

        def call_snowflake(name: str, args: dict[str, Any]) -> Any:
            allowed_groups = {str(g) for g in (claims.get("client_groups") or []) if g}
            customer_regions = {r["customer_id"]: r["region"] for r in debt_ledger + bulk_ledger}
            if name == "query_debt_ledger":
                source = bulk_ledger if args.get("dataset") == "bulk" else debt_ledger
                rows = [
                    r
                    for r in source
                    if (not args.get("status") or r["status"] == args["status"])
                    and (not args.get("region") or r["region"] == args["region"])
                    and (not allowed_groups or r["region"] in allowed_groups)
                ]
                return {"queried_by": claims["sub"], "rows": rows}
            if name == "get_customer_balance":
                cid = args.get("customer_id")
                row = next((r for r in debt_ledger if r["customer_id"] == cid), None)
                if row is None:
                    raise ValueError(f"Unknown customer_id '{cid}'")
                if allowed_groups and row["region"] not in allowed_groups:
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            f"RLS denied customer '{cid}' (region {row['region']}) "
                            f"for client_groups={sorted(allowed_groups)}"
                        ),
                    )
                return row
            if name == "get_trial_balance_lines":
                only_var = bool(args.get("only_variances", False))
                return {
                    "lines": [
                        line
                        for line in trial_balance
                        if (not only_var or line["variance"] != 0.0)
                        and (
                            not allowed_groups
                            or customer_regions.get(line["customer_id"]) in allowed_groups
                        )
                    ]
                }
            if name == "compute_interest_penalty":
                cid = str(args["customer_id"])
                principal = float(args["principal"])
                days = int(args["days_past_due"])
                rate = float(args.get("annual_rate_pct", 18.0))
                penalty = round(principal * (rate / 100.0) * (days / 365.0), 2)
                return {
                    "customer_id": cid,
                    "principal": principal,
                    "days_past_due": days,
                    "penalty_amount": penalty,
                    "total_due": round(principal + penalty, 2),
                }
            raise KeyError(f"Unknown tool '{name}'")

        return await _handle_mcp_rpc(
            request,
            server_name="snowflake_ledger",
            tools=snowflake_tools,
            call_fn=call_snowflake,
        )

    @app.post("/ops/mcp")
    async def ops_mcp(
        request: Request, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        claims = await verifier.verify(
            authorization,
            expected_audience="api://ops-ticketing",
            required_roles={"ops_engineer", "ticketing_writer", "support_agent", "admin"},
        )

        def call_ops(name: str, args: dict[str, Any]) -> Any:
            if name == "list_terminal_incidents":
                rows = [
                    inc
                    for inc in incidents
                    if not args.get("status") or inc["status"] == args["status"]
                ]
                return {"incidents": rows}
            if name == "get_terminal_health":
                tid = args.get("terminal_id")
                inc = next((i for i in incidents if i["terminal_id"] == tid), None)
                return {
                    "terminal_id": tid,
                    "online": inc is None or inc["status"] == "resolved",
                    "active_incident": inc,
                }
            if name == "create_collection_case":
                roles = set(claims.get("roles", []))
                if not (roles & {"ticketing_writer", "admin"}):
                    raise HTTPException(
                        status_code=403, detail="Role 'ticketing_writer' required to create cases"
                    )
                case = {
                    "case_id": f"CASE-{len(collection_cases) + 1:04d}",
                    "customer_id": args["customer_id"],
                    "reason": args["reason"],
                    "priority": args.get("priority", "high"),
                    "created_by": claims["sub"],
                }
                collection_cases.append(case)
                return case
            if name == "search_sentry_errors":
                tid = str(args["terminal_id"])
                sha_map = {
                    "TERM-EU-44": ("SEN-8801", "sha-9f4c21a", "SettlementBatchTimeoutError"),
                    "TERM-US-18": ("SEN-8802", "sha-3b11e04", "PinpadFirmwareMismatchError"),
                }
                ev_id, sha, err_type = sha_map.get(
                    tid, ("SEN-8899", "sha-0000000", "TerminalOfflineError")
                )
                return {
                    "event_id": ev_id,
                    "terminal_id": tid,
                    "release_sha": sha,
                    "error_type": err_type,
                    "culprit": f"payments.settlement.worker ({tid})",
                }
            if name == "get_github_release_commit":
                sha = str(args["release_sha"])
                pr_map = {
                    "sha-9f4c21a": (412, "alice-sre", "acme/payment-terminal-firmware"),
                    "sha-3b11e04": (389, "bob-embedded", "acme/payment-terminal-firmware"),
                }
                pr_num, author, repo = pr_map.get(sha, (100, "release-bot", "acme/core"))
                return {
                    "release_sha": sha,
                    "pr_number": pr_num,
                    "author": author,
                    "repo": repo,
                }
            if name == "post_slack_incident_alert":
                roles = set(claims.get("roles", []))
                if not (roles & {"ticketing_writer", "ops_engineer", "admin"}):
                    raise HTTPException(
                        status_code=403, detail="Write role required to post Slack alerts"
                    )
                return {
                    "message_ts": "1732051200.004200",
                    "channel": str(args["channel"]),
                    "case_id": str(args["case_id"]),
                    "delivered": True,
                }
            raise KeyError(f"Unknown tool '{name}'")

        return await _handle_mcp_rpc(
            request,
            server_name="ops_ticketing",
            tools=ops_tools,
            call_fn=call_ops,
        )

    app.mount(
        "/billing-eu",
        _build_billing_subapp(
            region="EU",
            audience="api://billing-eu",
            invoices=invoices_by_region["EU"],
            bulk_invoices=bulk_invoices_by_region["EU"],
            customers=customers_by_region["EU"],
            verifier=verifier,
        ),
    )
    app.mount(
        "/billing-us",
        _build_billing_subapp(
            region="US",
            audience="api://billing-us",
            invoices=invoices_by_region["US"],
            bulk_invoices=bulk_invoices_by_region["US"],
            customers=customers_by_region["US"],
            verifier=verifier,
        ),
    )

    def _build_simple_enterprise_subapp(
        *,
        title: str,
        mount_prefix: str,
        op_id: str,
        path: str,
        rows: list[dict[str, Any]],
    ) -> FastAPI:
        sub = FastAPI(title=title, version="1.0.0", servers=[{"url": mount_prefix}])

        @sub.get(path, operation_id=op_id)
        async def list_items(limit: int = Query(default=25, ge=1, le=100)) -> dict[str, Any]:
            return {"items": rows[:limit], "count": len(rows[:limit])}

        return sub

    app.mount(
        "/crm",
        _build_simple_enterprise_subapp(
            title="Enterprise CRM Accounts API",
            mount_prefix="/crm",
            op_id="list_crm_accounts",
            path="/accounts",
            rows=[
                {
                    "account_id": "ACC-101",
                    "name": "Acme Retail EU",
                    "arr_usd": 148000,
                    "tier": "enterprise",
                },
                {
                    "account_id": "ACC-102",
                    "name": "Northwind Logistics",
                    "arr_usd": 92000,
                    "tier": "growth",
                },
                {
                    "account_id": "ACC-103",
                    "name": "Contoso FinTech",
                    "arr_usd": 215000,
                    "tier": "enterprise",
                },
            ],
        ),
    )
    app.mount(
        "/hr",
        _build_simple_enterprise_subapp(
            title="HR Directory API",
            mount_prefix="/hr",
            op_id="list_hr_employees",
            path="/employees",
            rows=[
                {"emp_id": "EMP-01", "department": "finance", "role": "controller", "region": "EU"},
                {"emp_id": "EMP-02", "department": "engineering", "role": "sre", "region": "US"},
                {"emp_id": "EMP-03", "department": "sales_ops", "role": "analyst", "region": "EU"},
            ],
        ),
    )
    app.mount(
        "/devops",
        _build_simple_enterprise_subapp(
            title="DevOps CI/CD Pipelines API",
            mount_prefix="/devops",
            op_id="list_ci_deployments",
            path="/deployments",
            rows=[
                {
                    "deploy_id": "DEP-901",
                    "service": "gateway-prod",
                    "status": "healthy",
                    "sha": "ca43384",
                },
                {
                    "deploy_id": "DEP-902",
                    "service": "billing-eu",
                    "status": "healthy",
                    "sha": "9b4c734",
                },
            ],
        ),
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_mock_backends_app()
