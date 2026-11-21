"""Auth-spectrum OpenAPI test services enforcing all 7 downstream auth_type modes."""

from __future__ import annotations

import hmac
from typing import Any

import jwt
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from examples.auth_spectrum.mock_idp import ENTRA_ISSUER, KEYCLOAK_ISSUER, MockIdpState

SHARED_API_KEY = "test-vault-api-key-001"
SHARED_BEARER_SECRET = "test-file-bearer-secret-002"
USER_PATS: dict[str, str] = {
    "pat-alice-111": "user-fin-01",
    "pat-elena-222": "user-fin-eu-01",
}


class TaxRequest(BaseModel):
    region: str
    subtotal: float


class AggregateRequest(BaseModel):
    values: list[float]


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    return authorization.split(" ", 1)[1].strip()


def create_auth_spectrum_services(idp: MockIdpState) -> dict[str, FastAPI]:
    """Create 8 standalone OpenAPI FastAPI apps covering every auth_type & dialect."""
    services: dict[str, FastAPI] = {}

    inv_obo = FastAPI(title="Inventory Entra OBO API", version="1.0.0")

    @inv_obo.get("/items", operation_id="list_inventory_items")
    def list_inventory_items(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        tok = _extract_bearer(authorization)
        try:
            claims = idp.verify_jwt(tok, audience="api://inventory_obo", issuer=ENTRA_ISSUER)
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail=f"Invalid Entra OBO token: {exc}") from exc
        return {
            "auth_verified": "entra_v2_obo",
            "caller_sub": claims["sub"],
            "roles": claims.get("roles", []),
            "items": [
                {"id": "sku-100", "name": "Widget A", "qty": 12, "region": "EU"},
                {"id": "sku-200", "name": "Widget B", "qty": 5, "region": "US"},
            ],
        }

    services["inventory_obo"] = inv_obo

    tax_kc = FastAPI(title="Tax Keycloak RFC 8693 API", version="1.0.0")
    rates = {"us": 0.08, "eu": 0.20, "uk": 0.20}

    @tax_kc.get("/rates/{region}", operation_id="get_tax_rate")
    def get_tax_rate(
        region: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        tok = _extract_bearer(authorization)
        try:
            claims = idp.verify_jwt(tok, audience="api://tax_keycloak", issuer=KEYCLOAK_ISSUER)
        except jwt.PyJWTError as exc:
            raise HTTPException(
                status_code=401, detail=f"Invalid Keycloak 8693 token: {exc}"
            ) from exc
        return {
            "auth_verified": "rfc8693",
            "caller_sub": claims["sub"],
            "groups": claims.get("groups", []),
            "region": region.lower(),
            "rate": rates.get(region.lower(), 0.0),
        }

    @tax_kc.post("/calculate", operation_id="calculate_tax")
    def calculate_tax(
        body: TaxRequest, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        tok = _extract_bearer(authorization)
        try:
            claims = idp.verify_jwt(tok, audience="api://tax_keycloak", issuer=KEYCLOAK_ISSUER)
        except jwt.PyJWTError as exc:
            raise HTTPException(
                status_code=401, detail=f"Invalid Keycloak 8693 token: {exc}"
            ) from exc
        rate = rates.get(body.region.lower(), 0.0)
        tax = round(body.subtotal * rate, 2)
        return {
            "auth_verified": "rfc8693",
            "caller_sub": claims["sub"],
            "region": body.region.lower(),
            "tax": tax,
            "total": round(body.subtotal + tax, 2),
        }

    services["tax_keycloak"] = tax_kc

    metrics_m2m = FastAPI(title="Metrics M2M API", version="1.0.0")

    @metrics_m2m.get("/summary", operation_id="get_metrics_summary")
    def get_metrics_summary(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        tok = _extract_bearer(authorization)
        try:
            claims = idp.verify_jwt(tok, audience="api://metrics_m2m")
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail=f"Invalid M2M token: {exc}") from exc
        return {
            "auth_verified": "client_credentials",
            "service_sub": claims["sub"],
            "status": "ok",
        }

    @metrics_m2m.post("/aggregate", operation_id="aggregate_values")
    def aggregate_values(
        body: AggregateRequest, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        tok = _extract_bearer(authorization)
        try:
            claims = idp.verify_jwt(tok, audience="api://metrics_m2m")
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail=f"Invalid M2M token: {exc}") from exc
        vals = body.values
        total = round(sum(vals), 4) if vals else 0.0
        return {
            "auth_verified": "client_credentials",
            "service_sub": claims["sub"],
            "count": len(vals),
            "sum": total,
        }

    services["metrics_m2m"] = metrics_m2m

    inv_apikey = FastAPI(title="Inventory API Key Service", version="1.0.0")

    @inv_apikey.get("/items/{item_id}/value", operation_id="get_item_value")
    def get_item_value(
        item_id: str, x_api_key: str | None = Header(default=None)
    ) -> dict[str, Any]:
        if not x_api_key or not hmac.compare_digest(x_api_key, SHARED_API_KEY):
            raise HTTPException(status_code=401, detail="Invalid X-API-Key")
        return {"auth_verified": "api_key", "item_id": item_id, "total_value": 234.0}

    services["inventory_apikey"] = inv_apikey

    tax_bearer = FastAPI(title="Tax Bearer Secret Service", version="1.0.0")

    @tax_bearer.get("/summary", operation_id="get_tax_summary")
    def get_tax_summary(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        tok = _extract_bearer(authorization)
        if not hmac.compare_digest(tok, SHARED_BEARER_SECRET):
            raise HTTPException(status_code=401, detail="Invalid bearer secret")
        return {"auth_verified": "bearer_secret", "regions": ["us", "eu", "uk"]}

    services["tax_bearer"] = tax_bearer

    ext_oauth = FastAPI(title="External OAuth User Service", version="1.0.0")

    @ext_oauth.get("/profile", operation_id="get_external_profile")
    def get_external_profile(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        tok = _extract_bearer(authorization)
        rec = idp.external_tokens.get(tok)
        if rec is None:
            raise HTTPException(status_code=401, detail="Invalid external OAuth token")
        return {"auth_verified": "user_oauth", "external_user": rec["user"]}

    services["external_oauth"] = ext_oauth

    ext_pat = FastAPI(title="External Personal PAT Service", version="1.0.0")

    @ext_pat.get("/notes", operation_id="list_personal_notes")
    def list_personal_notes(
        x_personal_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        owner = USER_PATS.get(x_personal_token or "")
        if owner is None:
            raise HTTPException(status_code=401, detail="Invalid X-Personal-Token")
        return {
            "auth_verified": "user_api_key",
            "owner": owner,
            "notes": [f"private-note-for-{owner}"],
        }

    services["external_pat"] = ext_pat

    sandbox_open = FastAPI(title="Open Sandbox Service", version="1.0.0")

    @sandbox_open.get("/ping", operation_id="ping_sandbox")
    def ping_sandbox() -> dict[str, Any]:
        return {"auth_verified": "none", "ok": True}

    services["sandbox_open"] = sandbox_open

    return services
