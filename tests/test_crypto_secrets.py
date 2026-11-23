"""Tests for AES-GCM envelope encryption, connect tickets, and pluggable SecretResolver."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx
import pytest

from gateway.config import GatewaySettings
from gateway.crypto import (
    CryptoError,
    EnvelopeCipher,
    sign_connect_ticket,
    verify_connect_ticket,
)
from gateway.secrets import CredentialVault, SecretResolutionError, SecretResolver
from gateway.state import PostgresAuditStore, SQLiteAuditStore


def _b64_key(seed: bytes) -> str:
    return base64.urlsafe_b64encode(seed * 2).decode("ascii")


def test_envelope_cipher_roundtrip_aad_and_key_rotation() -> None:
    k1 = _b64_key(b"0123456789abcdef")
    k2 = _b64_key(b"fedcba9876543210")
    cipher_v1 = EnvelopeCipher(f"k1:{k1}")
    ct = cipher_v1.encrypt("super-secret-pat", aad=b"tenant-a:user-1:jira")
    assert ct.startswith("v1:k1:")
    assert cipher_v1.decrypt(ct, aad=b"tenant-a:user-1:jira") == "super-secret-pat"

    with pytest.raises(CryptoError):
        cipher_v1.decrypt(ct, aad=b"tenant-a:user-2:jira")

    cipher_v2 = EnvelopeCipher(f"k2:{k2},k1:{k1}")
    assert cipher_v2.needs_reencrypt(ct) is True
    assert cipher_v2.decrypt(ct, aad=b"tenant-a:user-1:jira") == "super-secret-pat"
    ct2 = cipher_v2.encrypt("super-secret-pat", aad=b"tenant-a:user-1:jira")
    assert ct2.startswith("v1:k2:")
    assert cipher_v2.needs_reencrypt(ct2) is False


def test_envelope_cipher_fail_closed_when_ephemeral_disallowed() -> None:
    cipher = EnvelopeCipher("", allow_ephemeral=False)
    assert cipher.is_configured is False
    with pytest.raises(CryptoError):
        cipher.encrypt("secret")


def test_connect_ticket_signing_and_verification() -> None:
    key = os.urandom(32)
    ticket = sign_connect_ticket(
        signing_key=key,
        sub="user-1",
        tenant_id="tenant-acme",
        upstream_name="jira_cloud",
        ttl_seconds=120,
    )
    payload = verify_connect_ticket(
        signing_key=key,
        ticket=ticket,
        expected_upstream="jira_cloud",
    )
    assert payload["sub"] == "user-1"
    assert payload["tid"] == "tenant-acme"

    with pytest.raises(CryptoError):
        verify_connect_ticket(signing_key=key, ticket=ticket, expected_upstream="github")

    tampered = ticket[:-2] + ("AA" if not ticket.endswith("AA") else "BB")
    with pytest.raises(CryptoError):
        verify_connect_ticket(signing_key=key, ticket=tampered)


async def test_secret_resolver_env_file_vault_akv_hcv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    allowed_file = secret_dir / "api_token"
    allowed_file.write_text("file-secret-123\n", encoding="utf-8")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("nope", encoding="utf-8")

    monkeypatch.setenv("TEST_RESOLVER_ENV", "env-secret-456")
    monkeypatch.setenv("AZURE_KEY_VAULT_TOKEN", "az-tok")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.internal:8200")
    monkeypatch.setenv("VAULT_TOKEN", "hcv-tok")

    settings = GatewaySettings(
        app_env="sandbox",
        allowed_secret_dirs=[str(secret_dir)],
        ssrf_protection_enabled=False,
    )
    store = SQLiteAuditStore(str(tmp_path / "gw.db"))
    await store.init()
    cipher = EnvelopeCipher(f"k1:{_b64_key(b'0123456789abcdef')}")
    vault = CredentialVault(store=store, cipher=cipher)
    await vault.save_gateway_secret("stripe_key", "sk_live_vault_789", updated_by="admin")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host is not None and request.url.host.endswith(".vault.azure.net"):
            return httpx.Response(200, json={"value": "akv-secret-111"})
        if request.url.host == "vault.internal" and request.url.port == 8200:
            return httpx.Response(200, json={"data": {"data": {"token": "hcv-secret-222"}}})
        return httpx.Response(404, json={"error": "not found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        resolver = SecretResolver(
            settings=settings, store=store, cipher=cipher, http_client=http_client
        )
        assert await resolver.resolve("env://TEST_RESOLVER_ENV") == "env-secret-456"
        assert await resolver.resolve("TEST_RESOLVER_ENV", allow_bare_env=True) == "env-secret-456"
        assert await resolver.resolve(f"file://{allowed_file}") == "file-secret-123"
        assert await resolver.resolve("vault://stripe_key") == "sk_live_vault_789"
        with pytest.raises(SecretResolutionError, match="OAuth client secrets"):
            await resolver.resolve("vault://stripe_key", allow_gateway_vault=False)
        assert await resolver.resolve("akv://myvault/mysecret") == "akv-secret-111"
        assert await resolver.resolve("hcv://kv/prod/mcp#token") == "hcv-secret-222"

        with pytest.raises(SecretResolutionError):
            await resolver.resolve(f"file://{outside_file}")
    await store.close()


@pytest.mark.asyncio
async def test_postgres_credential_vault_keeps_oauth_tokens_out_of_store() -> None:
    store = PostgresAuditStore("postgresql://unused", pool=object())

    async def no_user_credential(*_args: object, **_kwargs: object) -> None:
        return None

    async def no_user_credentials(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return []

    async def no_delete(*_args: object, **_kwargs: object) -> bool:
        return False

    store.get_user_credential = no_user_credential
    store.list_user_credentials = no_user_credentials
    store.delete_user_credential = no_delete
    vault = CredentialVault(
        store=store,
        cipher=EnvelopeCipher(f"k1:{_b64_key(b'0123456789abcdef')}"),
    )
    await vault.save_user_credential(
        tenant_id="tenant-a",
        user_sub="user-a",
        upstream_name="upstream-a",
        auth_kind="user_oauth",
        payload={"access_token": "access-secret", "refresh_token": "refresh-secret"},
    )
    credential = await vault.load_user_credential(
        tenant_id="tenant-a", user_sub="user-a", upstream_name="upstream-a"
    )
    assert credential is not None
    assert credential["payload"]["access_token"] == "access-secret"
    assert credential["payload"]["refresh_token"] == "refresh-secret"
    assert (
        await vault.load_user_credential(
            tenant_id="tenant-b", user_sub="user-a", upstream_name="upstream-a"
        )
        is None
    )
    assert (
        await vault.load_user_credential(
            tenant_id="tenant-a", user_sub="user-b", upstream_name="upstream-a"
        )
        is None
    )

    other_runtime_vault = CredentialVault(
        store=store,
        cipher=EnvelopeCipher(f"k1:{_b64_key(b'0123456789abcdef')}"),
    )
    assert (
        await other_runtime_vault.load_user_credential(
            tenant_id="tenant-a", user_sub="user-a", upstream_name="upstream-a"
        )
        is None
    )
    assert await vault.delete_user_credential(
        tenant_id="tenant-a", user_sub="user-a", upstream_name="upstream-a"
    )
    assert (
        await vault.load_user_credential(
            tenant_id="tenant-a", user_sub="user-a", upstream_name="upstream-a"
        )
        is None
    )
