"""Mock enterprise MCP and OpenAPI backends enforcing downstream delegated JWTs and RBAC."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx
import jwt
from fastapi import FastAPI, Header, HTTPException, Query, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import APIKeyHeader
from jwt.algorithms import RSAAlgorithm
from pydantic import BaseModel

from gateway.auth import DEV_PERSONAS, LocalDevKeyManager, claims_to_identity
from gateway.config import ClaimMappingConfig
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


class IdempotencyOperationRequest(BaseModel):
    idempotency_key: str
    item_id: str
    simulate_lost_response: bool = False


_READINESS_OIDC_IDENTITIES: dict[str, dict[str, Any]] = {
    "readiness-admin": {
        "sub": "readiness-admin",
        "tenant_id": "tenant-acme-dev",
        "email": "readiness-admin@example.test",
        "client_id": "mcp-readiness",
        "roles": ["gateway_admin"],
        "groups": ["platform-admins"],
        "scopes": ["openid", "mcp.tools", "mcp.admin", "mcp.audit"],
    },
    "readiness-finance": {
        "sub": "readiness-finance",
        "tenant_id": "tenant-acme-dev",
        "email": "readiness-finance@example.test",
        "client_id": "mcp-readiness",
        "roles": ["finance_analyst"],
        "groups": ["finance"],
        "scopes": ["openid", "mcp.tools", "finance.read"],
    },
    "readiness-ops": {
        "sub": "readiness-ops",
        "tenant_id": "tenant-acme-dev",
        "email": "readiness-ops@example.test",
        "client_id": "mcp-readiness",
        "roles": ["ops_engineer", "ticketing_writer", "tool_approver"],
        "groups": ["operations", "operations-approvers"],
        "scopes": ["openid", "mcp.tools", "ops.write"],
    },
    "readiness-approver": {
        "sub": "readiness-approver",
        "tenant_id": "tenant-acme-dev",
        "email": "readiness-approver@example.test",
        "client_id": "mcp-readiness",
        "roles": ["tool_approver"],
        "groups": ["operations-approvers"],
        "scopes": ["openid", "mcp.tools"],
    },
    "readiness-viewer": {
        "sub": "readiness-viewer",
        "tenant_id": "tenant-acme-dev",
        "email": "readiness-viewer@example.test",
        "client_id": "mcp-readiness",
        "roles": ["operator_viewer"],
        "groups": ["platform"],
        "scopes": ["openid", "mcp.tools"],
    },
    "readiness-unassigned": {
        "sub": "readiness-unassigned",
        "tenant_id": "tenant-acme-dev",
        "email": "readiness-unassigned@example.test",
        "client_id": "mcp-readiness",
        "roles": [],
        "groups": [],
        "scopes": ["openid", "mcp.tools"],
    },
    "readiness-other-tenant": {
        "sub": "readiness-other-tenant",
        "tenant_id": "mcp-other",
        "email": "readiness-other@example.test",
        "client_id": "mcp-readiness",
        "roles": ["finance_analyst"],
        "groups": ["finance"],
        "scopes": ["openid", "mcp.tools", "finance.read"],
    },
    "readiness-wrong-group-approver": {
        "sub": "readiness-wrong-group-approver",
        "tenant_id": "tenant-acme-dev",
        "email": "readiness-wrong-group@example.test",
        "client_id": "mcp-readiness",
        "roles": ["tool_approver"],
        "groups": ["finance"],
        "scopes": ["openid", "mcp.tools"],
    },
}


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
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(self._jwks_url)
                    resp.raise_for_status()
                    self._cached_jwks = resp.json()
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"JWKS endpoint unavailable: {exc}",
                ) from exc
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
    expected_issuer: str | None = None,
    jwks_url: str | None = None,
) -> FastAPI:
    """Create isolated mock enterprise MCP and OpenAPI services for demos and tests."""
    resolved_jwks_url = jwks_url or os.getenv(
        "GATEWAY_JWKS_URL", "http://127.0.0.1:8000/dev/oidc/jwks"
    )
    resolved_expected_issuer = expected_issuer or os.getenv(
        "GATEWAY_TOKEN_ISSUER", "http://127.0.0.1:8000/dev/oidc"
    )
    verifier = _DownstreamTokenVerifier(
        public_key_pem=public_key_pem,
        jwks_url=resolved_jwks_url,
        expected_issuer=resolved_expected_issuer,
    )
    debt_ledger, trial_balance, invoices_by_region, customers_by_region, incidents = (
        _seed_enterprise_data()
    )
    readiness_approval_cases: list[dict[str, Any]] = []
    readiness_crm_health = {"available": True}
    idempotency_operations: dict[str, dict[str, Any]] = {}
    idempotency_side_effects = {"count": 0}
    bulk_ledger, bulk_invoices_by_region = _seed_bulk_data()
    collection_cases: list[dict[str, Any]] = []

    app = FastAPI(title="Enterprise Mock Backends", version="0.1.0")
    idempotency_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def _require_idempotency_api_key(x_api_key: str | None) -> None:
        expected = os.getenv("MOCK_IDEMPOTENCY_API_KEY", "mock-idempotency-key")
        if x_api_key != expected:
            raise HTTPException(status_code=401, detail="Invalid mock API key")

    @app.post("/idempotency/operations", operation_id="create_idempotent_operation")
    async def create_idempotent_operation(
        payload: IdempotencyOperationRequest,
        x_api_key: str | None = Security(idempotency_key_header),
    ) -> dict[str, Any]:
        _require_idempotency_api_key(x_api_key)
        key = payload.idempotency_key.strip()
        item_id = payload.item_id.strip()
        if not key or not item_id:
            raise HTTPException(status_code=422, detail="idempotency_key and item_id are required")
        fingerprint = hashlib.sha256(
            json.dumps({"item_id": item_id}, sort_keys=True).encode("utf-8")
        ).hexdigest()
        existing = idempotency_operations.get(key)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key was already used with a different request",
                )
            return {**existing["result"], "replayed": True}

        idempotency_side_effects["count"] += 1
        result = {
            "operation_id": f"OP-{idempotency_side_effects['count']:06d}",
            "idempotency_key": key,
            "item_id": item_id,
            "status": "committed",
            "replayed": False,
        }
        idempotency_operations[key] = {"fingerprint": fingerprint, "result": result}
        if payload.simulate_lost_response:
            raise HTTPException(status_code=504, detail="Simulated response loss after commit")
        return result

    @app.get(
        "/idempotency/operations/{idempotency_key}", operation_id="get_idempotent_operation_status"
    )
    async def get_idempotent_operation_status(
        idempotency_key: str, x_api_key: str | None = Security(idempotency_key_header)
    ) -> dict[str, Any]:
        _require_idempotency_api_key(x_api_key)
        operation = idempotency_operations.get(idempotency_key)
        if operation is None:
            raise HTTPException(status_code=404, detail="Operation not found")
        return {**operation["result"], "replayed": False}

    @app.get("/idempotency/metrics", operation_id="get_idempotent_operation_metrics")
    async def get_idempotent_operation_metrics(
        x_api_key: str | None = Security(idempotency_key_header),
    ) -> dict[str, int]:
        _require_idempotency_api_key(x_api_key)
        return {
            "side_effect_count": idempotency_side_effects["count"],
            "operation_count": len(idempotency_operations),
        }

    @app.middleware("http")
    async def readiness_crm_fault(request: Request, call_next):
        if (
            os.getenv("MOCK_ENABLE_READINESS_FAILURE_CONTROLS", "false").lower() == "true"
            and request.url.path.startswith("/crm/")
            and not readiness_crm_health["available"]
        ):
            return JSONResponse(
                {"detail": "Readiness CRM fixture is intentionally unavailable"},
                status_code=503,
            )
        return await call_next(request)

    @app.post("/readiness/test/upstream-health")
    async def set_readiness_crm_health(payload: dict[str, Any]) -> dict[str, bool]:
        if os.getenv("MOCK_ENABLE_READINESS_FAILURE_CONTROLS", "false").lower() != "true":
            raise HTTPException(status_code=404, detail="Readiness failure controls are disabled")
        available = payload.get("available")
        if not isinstance(available, bool):
            raise HTTPException(status_code=422, detail="available must be a boolean")
        readiness_crm_health["available"] = available
        return {"available": available}

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

    @app.post("/readiness/approvals/mcp")
    async def readiness_approvals_mcp(request: Request) -> JSONResponse:
        """Unauthenticated mutation fixture for the local approval readiness lab only."""
        tools = [
            {
                "name": "create_approval_case",
                "description": "Create a local readiness case after manager approval.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "customer_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["customer_id", "reason"],
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "case_id": {"type": "string"},
                        "customer_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
                "annotations": {"readOnlyHint": False, "destructiveHint": False},
            }
        ]

        def create_readiness_case(name: str, args: dict[str, Any]) -> Any:
            if name != "create_approval_case":
                raise KeyError(name)
            case = {
                "case_id": f"READY-{len(readiness_approval_cases) + 1:04d}",
                "customer_id": args["customer_id"],
                "reason": args["reason"],
            }
            readiness_approval_cases.append(case)
            return case

        return await _handle_mcp_rpc(
            request,
            server_name="readiness_approvals",
            tools=tools,
            call_fn=create_readiness_case,
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

        @sub.get("/summary", operation_id=f"{op_id}_summary")
        async def get_summary() -> dict[str, Any]:
            return {"service": title, "total_records": len(rows), "status": "ok"}

        @sub.get("/health", operation_id=f"{op_id}_health")
        async def get_health() -> dict[str, Any]:
            return {"service": title, "healthy": True, "records": len(rows)}

        @sub.get("/metrics", operation_id=f"{op_id}_metrics")
        async def get_metrics() -> dict[str, Any]:
            return {"service": title, "active_items": len(rows), "latency_p50_ms": 12.4}

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

    mock_oidc_issuer = os.getenv("MOCK_OIDC_ISSUER", "http://127.0.0.1:8001/oidc").rstrip("/")
    mock_oidc_private_key_path = os.getenv("MOCK_OIDC_PRIVATE_KEY_PATH")
    mock_oidc_private_key = (
        Path(mock_oidc_private_key_path).read_text(encoding="utf-8")
        if mock_oidc_private_key_path
        else None
    )
    mock_oidc_keys = LocalDevKeyManager(
        issuer=mock_oidc_issuer,
        private_key_pem=mock_oidc_private_key,
    )
    mock_oidc_audience = os.getenv("MOCK_OIDC_AUDIENCE", "api://mcp-code-gateway")
    mock_oidc_client_id = os.getenv("MOCK_OIDC_CLIENT_ID", "mcp-readiness")
    mock_oidc_redirect_uri = os.getenv(
        "MOCK_OIDC_REDIRECT_URI", "http://localhost:8080/auth/admin/callback"
    )
    mock_oidc_codes: dict[str, dict[str, Any]] = {}
    mock_oidc_supported_scopes = {
        "openid",
        "profile",
        "email",
        "mcp.tools",
        "mcp.admin",
        "mcp.audit",
        "finance.read",
        "ops.read",
        "ops.write",
    }

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/oidc/.well-known/openid-configuration")
    async def mock_oidc_openid_config() -> dict[str, Any]:
        return {
            "issuer": mock_oidc_issuer,
            "authorization_endpoint": f"{mock_oidc_issuer}/authorize",
            "token_endpoint": f"{mock_oidc_issuer}/token",
            "userinfo_endpoint": f"{mock_oidc_issuer}/userinfo",
            "jwks_uri": f"{mock_oidc_issuer}/jwks",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": [
                "openid",
                "profile",
                "email",
                "mcp.tools",
                "mcp.admin",
                "mcp.audit",
                "finance.read",
                "ops.read",
                "ops.write",
            ],
        }

    @app.get("/oidc/jwks")
    async def mock_oidc_jwks() -> dict[str, Any]:
        return mock_oidc_keys.jwks()

    @app.get("/oidc/authorize")
    async def mock_oidc_authorize(
        response_type: str = Query(...),
        client_id: str = Query(...),
        redirect_uri: str = Query(...),
        state: str = Query(..., min_length=1),
        scope: str = Query(...),
        code_challenge: str = Query(..., min_length=43, max_length=43),
        code_challenge_method: str = Query(...),
        nonce: str | None = Query(default=None),
        persona: str = Query(default="gateway_admin"),
    ) -> RedirectResponse:
        if response_type != "code":
            raise HTTPException(status_code=400, detail="response_type must be code")
        if client_id != mock_oidc_client_id or redirect_uri != mock_oidc_redirect_uri:
            raise HTTPException(status_code=400, detail="Unregistered OIDC client or redirect URI")
        scopes = set(scope.split())
        if "openid" not in scopes:
            raise HTTPException(status_code=400, detail="scope must include openid")
        if code_challenge_method != "S256":
            raise HTTPException(status_code=400, detail="Only S256 PKCE is supported")
        if persona not in DEV_PERSONAS and persona not in _READINESS_OIDC_IDENTITIES:
            raise HTTPException(status_code=400, detail="Unknown local mock OIDC persona")
        if not scopes <= mock_oidc_supported_scopes:
            raise HTTPException(status_code=400, detail="Requested scope is not supported")

        now = time.time()
        for expired_code in [
            saved_code
            for saved_code, saved_record in mock_oidc_codes.items()
            if saved_record["expires_at"] <= now
        ]:
            mock_oidc_codes.pop(expired_code, None)
        code = secrets.token_urlsafe(32)
        mock_oidc_codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "persona": persona,
            "scope": sorted(scopes),
            "nonce": nonce,
            "expires_at": time.time() + 120,
        }
        sep = "&" if "?" in redirect_uri else "?"
        q = urlencode({"code": code, "state": state})
        return RedirectResponse(url=f"{redirect_uri}{sep}{q}", status_code=302)

    @app.post("/oidc/token")
    async def mock_oidc_token(request: Request) -> dict[str, Any]:
        content_type = (request.headers.get("content-type") or "").lower()
        if not content_type.startswith("application/x-www-form-urlencoded"):
            raise HTTPException(status_code=415, detail="Token requests must be form encoded")
        raw = (await request.body()).decode("utf-8", errors="replace")
        form = {key: values[-1] for key, values in parse_qs(raw).items()}
        if form.get("grant_type") != "authorization_code":
            raise HTTPException(status_code=400, detail="grant_type must be authorization_code")
        code = str(form.get("code") or "")
        record = mock_oidc_codes.pop(code, None)
        if record is None or record["expires_at"] < time.time():
            raise HTTPException(status_code=400, detail="Invalid or expired authorization code")
        if (
            form.get("client_id") != record["client_id"]
            or form.get("client_id") != mock_oidc_client_id
            or form.get("redirect_uri") != record["redirect_uri"]
        ):
            raise HTTPException(status_code=400, detail="Authorization code client mismatch")
        verifier = str(form.get("code_verifier") or "")
        if not (
            43 <= len(verifier) <= 128
            and all(char.isascii() and (char.isalnum() or char in "-._~") for char in verifier)
        ):
            raise HTTPException(status_code=400, detail="Invalid PKCE code_verifier")
        digest = hashlib.sha256(verifier.encode("ascii", errors="strict")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if not secrets.compare_digest(challenge, record["code_challenge"]):
            raise HTTPException(status_code=400, detail="PKCE code_verifier mismatch")

        now = int(time.time())
        readiness_identity = _READINESS_OIDC_IDENTITIES.get(str(record["persona"]))
        granted_access_scopes = [
            item for item in record["scope"] if item not in {"openid", "profile", "email"}
        ]
        access_token = mock_oidc_keys.issue_user_token(
            persona=None if readiness_identity else str(record["persona"]),
            audience=mock_oidc_audience,
            ttl_seconds=3600,
            overrides=(
                {**readiness_identity, "scopes": granted_access_scopes}
                if readiness_identity
                else {"client_id": mock_oidc_client_id, "scopes": granted_access_scopes}
            ),
        )
        access_claims = jwt.decode(
            access_token,
            key=mock_oidc_keys.public_pem,
            algorithms=["RS256"],
            audience=mock_oidc_audience,
            issuer=mock_oidc_issuer,
        )
        id_claims = {
            key: access_claims[key]
            for key in ("iss", "sub", "email", "groups", "roles", "tid", "azp")
            if key in access_claims
        }
        id_claims.update(
            {
                "aud": mock_oidc_client_id,
                "iat": now,
                "exp": now + 3600,
                "auth_time": now,
            }
        )
        if record.get("nonce"):
            id_claims["nonce"] = record["nonce"]
        id_token = jwt.encode(
            id_claims,
            mock_oidc_keys.private_pem,
            algorithm="RS256",
            headers={"kid": mock_oidc_keys.kid},
        )
        return {
            "access_token": access_token,
            "id_token": id_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "issuer": mock_oidc_issuer,
            "persona": record["persona"],
        }

    @app.get("/oidc/userinfo")
    async def mock_oidc_userinfo(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=401,
                detail="A valid bearer access token is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        raw_token = authorization.split(" ", 1)[1].strip()
        try:
            claims = jwt.decode(
                raw_token,
                key=mock_oidc_keys.public_pem,
                algorithms=["RS256"],
                audience=mock_oidc_audience,
                issuer=mock_oidc_issuer,
                options={"require": ["exp", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail=f"Invalid OIDC token: {exc}") from exc
        identity = claims_to_identity(claims, ClaimMappingConfig(), raw_token=raw_token)
        profile = identity.public_profile()
        profile["iss"] = mock_oidc_issuer
        return profile

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_mock_backends_app()
