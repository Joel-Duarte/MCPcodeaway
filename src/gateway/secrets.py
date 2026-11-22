"""Pluggable secret resolution (env, file, vault, AKV, HCV) and user credential vault."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from gateway.config import GatewaySettings
from gateway.crypto import CryptoError, EnvelopeCipher
from gateway.state import AuditStore, PostgresAuditStore


class SecretResolutionError(Exception):
    """Raised when a secret reference cannot be resolved or fails security validation."""


def _secret_aad(name: str) -> bytes:
    return f"gateway_secret:{name}".encode()


def _user_cred_aad(tenant_id: str, user_sub: str, upstream_name: str) -> bytes:
    return f"user_cred:{tenant_id}:{user_sub}:{upstream_name}".encode()


class SecretResolver:
    """Resolves secret references from env://, file://, vault://, akv://, or hcv://."""

    def __init__(
        self,
        *,
        settings: GatewaySettings,
        store: AuditStore | None = None,
        cipher: EnvelopeCipher | None = None,
        http_client: httpx.AsyncClient | None = None,
        cache_ttl_seconds: int = 60,
    ) -> None:
        self.settings = settings
        self.store = store
        self.cipher = cipher
        self._http_client = http_client
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[str, float]] = {}

    def invalidate(self, ref: str | None = None) -> None:
        """Invalidate cached external secrets (all or a specific ref)."""
        if ref is None:
            self._cache.clear()
        else:
            self._cache.pop(ref, None)

    async def resolve(
        self,
        ref: str,
        *,
        allow_bare_env: bool = False,
        allow_gateway_vault: bool = True,
    ) -> str:
        """Resolve a secret reference URI to its plaintext value."""
        raw = (ref or "").strip()
        if not raw:
            raise SecretResolutionError("Secret reference is empty")

        if "://" not in raw:
            if allow_bare_env:
                raw = f"env://{raw}"
            else:
                raise SecretResolutionError(
                    f"Secret reference '{raw}' must use an explicit scheme "
                    "(env://, file://, vault://, akv://, hcv://)"
                )

        scheme, rest = raw.split("://", 1)
        scheme = scheme.lower()

        if scheme == "env":
            val = os.getenv(rest, "")
            if not val:
                raise SecretResolutionError(f"Environment variable '{rest}' is not set")
            return val

        if scheme == "file":
            return self._resolve_file(rest)

        if scheme == "vault":
            if not allow_gateway_vault:
                raise SecretResolutionError(
                    "OAuth client secrets must use an external secret provider, "
                    "not gateway vault storage"
                )
            return await self._resolve_gateway_vault(rest)

        now = time.monotonic()
        cached = self._cache.get(raw)
        if cached is not None and cached[1] > now:
            return cached[0]

        if scheme == "akv":
            val = await self._resolve_akv(rest)
        elif scheme == "hcv":
            val = await self._resolve_hcv(rest)
        else:
            raise SecretResolutionError(f"Unsupported secret scheme '{scheme}://'")

        self._cache[raw] = (val, now + self._cache_ttl)
        return val

    def _resolve_file(self, path_str: str) -> str:
        if not path_str:
            raise SecretResolutionError("file:// secret path is empty")
        try:
            target = Path(path_str).resolve(strict=True)
        except OSError as exc:
            raise SecretResolutionError(f"Secret file '{path_str}' is not accessible") from exc
        if not target.is_file():
            raise SecretResolutionError(f"Secret path '{path_str}' is not a regular file")

        allowed_roots = [Path(d).resolve() for d in self.settings.allowed_secret_dirs if d]
        if not any(target.is_relative_to(root) for root in allowed_roots):
            raise SecretResolutionError(f"Secret file '{path_str}' is outside allowed_secret_dirs")
        content = target.read_text(encoding="utf-8").rstrip("\r\n")
        if not content:
            raise SecretResolutionError(f"Secret file '{path_str}' is empty")
        return content

    async def _resolve_gateway_vault(self, name: str) -> str:
        if self.store is None or self.cipher is None:
            raise SecretResolutionError("Gateway vault store/cipher is not initialized")
        if not name:
            raise SecretResolutionError("vault:// secret name is empty")
        ciphertext = await self.store.get_gateway_secret(name)
        if ciphertext is None:
            raise SecretResolutionError(f"Vault secret '{name}' not found")
        try:
            return self.cipher.decrypt(ciphertext, aad=_secret_aad(name))
        except CryptoError as exc:
            raise SecretResolutionError(f"Failed to decrypt vault secret '{name}': {exc}") from exc

    async def _http_get_json(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        if self.settings.ssrf_protection_enabled:
            from gateway.upstream_clients import validate_upstream_url

            validate_upstream_url(
                url,
                app_env=self.settings.app_env,
                block_private_ips_in_prod=self.settings.block_private_ips_in_prod,
                allowed_hosts=self.settings.allowed_upstream_hosts,
            )
        if self._http_client is not None:
            resp = await self._http_client.get(url, headers=headers, follow_redirects=False)
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers, follow_redirects=False)
        if resp.status_code >= 400:
            raise SecretResolutionError(f"External secret manager returned HTTP {resp.status_code}")
        data = resp.json()
        if not isinstance(data, dict):
            raise SecretResolutionError("External secret manager returned non-object JSON")
        return data

    async def _resolve_akv(self, rest: str) -> str:
        parts = [p for p in rest.split("/") if p]
        if len(parts) < 2:
            raise SecretResolutionError("akv:// reference must be akv://<vault-name>/<secret-name>")
        vault_name, secret_name = parts[0], parts[1]
        token = os.getenv("AZURE_KEY_VAULT_TOKEN", "")
        if not token:
            raise SecretResolutionError(
                "AZURE_KEY_VAULT_TOKEN must be set to resolve akv:// secrets"
            )
        url = (
            f"https://{quote(vault_name, safe='')}.vault.azure.net"
            f"/secrets/{quote(secret_name, safe='')}?api-version=7.4"
        )
        body = await self._http_get_json(url, {"Authorization": f"Bearer {token}"})
        value = body.get("value")
        if not isinstance(value, str) or not value:
            raise SecretResolutionError(f"AKV secret '{secret_name}' has no value")
        return value

    async def _resolve_hcv(self, rest: str) -> str:
        addr = os.getenv("VAULT_ADDR", "").rstrip("/")
        token = os.getenv("VAULT_TOKEN", "")
        if not addr or not token:
            raise SecretResolutionError("VAULT_ADDR and VAULT_TOKEN must be set for hcv:// secrets")
        parsed_addr = urlsplit(addr)
        if not parsed_addr.scheme or not parsed_addr.netloc:
            raise SecretResolutionError("VAULT_ADDR is not a valid URL")

        path_part, _, field = rest.partition("#")
        field = field or "value"
        segments = [s for s in path_part.split("/") if s]
        if len(segments) < 2:
            raise SecretResolutionError("hcv:// reference must be hcv://<mount>/<path>[#<field>]")
        mount = quote(segments[0], safe="")
        secret_path = "/".join(quote(seg, safe="") for seg in segments[1:])
        url = f"{addr}/v1/{mount}/data/{secret_path}"
        body = await self._http_get_json(url, {"X-Vault-Token": token})
        data_layer = body.get("data", {})
        kv_data = data_layer.get("data", {}) if isinstance(data_layer, dict) else {}
        value = kv_data.get(field) if isinstance(kv_data, dict) else None
        if not isinstance(value, str) or not value:
            raise SecretResolutionError(f"HashiCorp Vault secret field '{field}' not found")
        return value


class CredentialVault:
    """Stores encrypted user credentials; PostgreSQL-backed OAuth stays process-local."""

    def __init__(self, *, store: AuditStore, cipher: EnvelopeCipher) -> None:
        self.store = store
        self.cipher = cipher
        self._oauth_credentials: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._oauth_lock = asyncio.Lock()

    async def save_gateway_secret(self, name: str, plaintext: str, *, updated_by: str = "") -> None:
        ct = self.cipher.encrypt(plaintext, aad=_secret_aad(name))
        await self.store.set_gateway_secret(name, ct, updated_by=updated_by)

    async def save_user_credential(
        self,
        *,
        tenant_id: str,
        user_sub: str,
        upstream_name: str,
        auth_kind: str,
        payload: dict[str, Any],
        expires_at: int | None = None,
    ) -> None:
        """Persist encrypted credentials except OAuth tokens for PostgreSQL stores."""
        raw = json.dumps(payload, separators=(",", ":"))
        if auth_kind == "user_oauth" and isinstance(self.store, PostgresAuditStore):
            async with self._oauth_lock:
                self._oauth_credentials[(tenant_id, user_sub, upstream_name)] = {
                    "auth_kind": auth_kind,
                    "expires_at": expires_at,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "payload": json.loads(raw),
                }
            return
        ct = self.cipher.encrypt(raw, aad=_user_cred_aad(tenant_id, user_sub, upstream_name))
        await self.store.set_user_credential(
            tenant_id,
            user_sub,
            upstream_name,
            auth_kind,
            ct,
            expires_at=expires_at,
        )

    async def load_user_credential(
        self,
        *,
        tenant_id: str,
        user_sub: str,
        upstream_name: str,
    ) -> dict[str, Any] | None:
        key = (tenant_id, user_sub, upstream_name)
        if isinstance(self.store, PostgresAuditStore):
            async with self._oauth_lock:
                oauth_record = self._oauth_credentials.get(key)
                if oauth_record is not None:
                    return dict(oauth_record)
        record = await self.store.get_user_credential(tenant_id, user_sub, upstream_name)
        if record is None:
            return None
        aad = _user_cred_aad(tenant_id, user_sub, upstream_name)
        plaintext = self.cipher.decrypt(record["ciphertext"], aad=aad)
        payload = json.loads(plaintext)
        if not isinstance(payload, dict):
            raise CryptoError("Corrupt user credential payload")
        if self.cipher.needs_reencrypt(record["ciphertext"]):
            await self.save_user_credential(
                tenant_id=tenant_id,
                user_sub=user_sub,
                upstream_name=upstream_name,
                auth_kind=record["auth_kind"],
                payload=payload,
                expires_at=record.get("expires_at"),
            )
        return {
            "auth_kind": record["auth_kind"],
            "expires_at": record.get("expires_at"),
            "updated_at": record.get("updated_at"),
            "payload": payload,
        }

    async def list_user_credentials(self, *, tenant_id: str, user_sub: str) -> list[dict[str, Any]]:
        persisted = await self.store.list_user_credentials(tenant_id, user_sub)
        if not isinstance(self.store, PostgresAuditStore):
            return persisted
        async with self._oauth_lock:
            volatile = [
                {
                    "upstream_name": upstream_name,
                    "auth_kind": "user_oauth",
                    "expires_at": record.get("expires_at"),
                    "updated_at": record.get("updated_at", ""),
                }
                for (tenant, subject, upstream_name), record in self._oauth_credentials.items()
                if tenant == tenant_id and subject == user_sub
            ]
        return sorted(persisted + volatile, key=lambda item: str(item["upstream_name"]))

    async def delete_user_credential(
        self, *, tenant_id: str, user_sub: str, upstream_name: str
    ) -> bool:
        async with self._oauth_lock:
            existed = self._oauth_credentials.pop((tenant_id, user_sub, upstream_name), None)
        return (
            await self.store.delete_user_credential(tenant_id, user_sub, upstream_name)
            or existed is not None
        )
