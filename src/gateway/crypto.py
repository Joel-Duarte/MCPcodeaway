"""Authenticated envelope encryption (AES-256-GCM) and HMAC connect-ticket helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gateway.observability import log_event


class CryptoError(Exception):
    """Raised when envelope encryption, decryption, or ticket verification fails."""


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (binascii.Error, ValueError) as exc:
        raise CryptoError("Invalid base64url encoding") from exc


def _decode_key_bytes(raw: str) -> bytes:
    """Decode a 32-byte key from standard/urlsafe base64 or hex."""
    candidate = raw.strip()
    if len(candidate) == 64:
        try:
            key = bytes.fromhex(candidate)
            if len(key) == 32:
                return key
        except ValueError:
            pass
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            padded = candidate + "=" * (-len(candidate) % 4)
            key = decoder(padded)
            if len(key) == 32:
                return key
        except (binascii.Error, ValueError):
            continue
    raise CryptoError("Encryption key must decode to exactly 32 bytes (256 bits)")


class EnvelopeCipher:
    """AES-256-GCM envelope cipher supporting key IDs (`kid`) and key rotation."""

    def __init__(
        self,
        key_spec: str = "",
        *,
        allow_ephemeral: bool = True,
    ) -> None:
        self._keys: dict[str, bytes] = {}
        self._active_kid: str = ""
        self._ephemeral = False

        spec = (key_spec or "").strip()
        if spec:
            for part in spec.split(","):
                item = part.strip()
                if not item:
                    continue
                if ":" in item:
                    kid, raw_key = item.split(":", 1)
                    kid = kid.strip()
                    if not kid:
                        raise CryptoError("Key ID in encryption key spec cannot be empty")
                else:
                    kid, raw_key = "default", item
                self._keys[kid] = _decode_key_bytes(raw_key)
                if not self._active_kid:
                    self._active_kid = kid
        elif allow_ephemeral:
            self._active_kid = "ephemeral"
            self._keys[self._active_kid] = os.urandom(32)
            self._ephemeral = True
            log_event(
                "crypto.ephemeral_key_generated",
                level=logging.WARNING,
                reason="GATEWAY_CREDENTIAL_ENCRYPTION_KEY unset in non-production mode",
            )

    @property
    def is_configured(self) -> bool:
        return bool(self._keys)

    @property
    def is_ephemeral(self) -> bool:
        """Whether the active encryption key exists only for this process lifetime."""
        return self._ephemeral

    @property
    def active_kid(self) -> str:
        return self._active_kid

    def _require_ready(self) -> None:
        if not self._keys or not self._active_kid:
            raise CryptoError(
                "GATEWAY_CREDENTIAL_ENCRYPTION_KEY must be configured for credential storage"
            )

    def encrypt(self, plaintext: str, *, aad: bytes = b"") -> str:
        """Encrypt a UTF-8 string into `v1:<kid>:<nonce>:<ciphertext>`."""
        self._require_ready()
        key = self._keys[self._active_kid]
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad or None)
        return f"v1:{self._active_kid}:{_b64u_encode(nonce)}:{_b64u_encode(ct)}"

    def decrypt(self, token: str, *, aad: bytes = b"") -> str:
        """Decrypt and authenticate a `v1:<kid>:<nonce>:<ciphertext>` envelope."""
        self._require_ready()
        parts = token.split(":")
        if len(parts) != 4 or parts[0] != "v1":
            raise CryptoError("Malformed encrypted credential envelope")
        _, kid, nonce_b64, ct_b64 = parts
        key = self._keys.get(kid)
        if key is None:
            raise CryptoError(f"Unknown encryption key ID '{kid}'")
        nonce = _b64u_decode(nonce_b64)
        ct = _b64u_decode(ct_b64)
        if len(nonce) != 12:
            raise CryptoError("Invalid AES-GCM nonce length")
        try:
            pt = AESGCM(key).decrypt(nonce, ct, aad or None)
        except InvalidTag as exc:
            raise CryptoError("Credential authentication tag verification failed") from exc
        return pt.decode("utf-8")

    def needs_reencrypt(self, token: str) -> bool:
        """Return True if the envelope was encrypted with a non-active key ID."""
        parts = token.split(":")
        if len(parts) != 4 or parts[0] != "v1":
            return True
        return parts[1] != self._active_kid

    def derive_subkey(self, purpose: str) -> bytes:
        """Derive a domain-separated 32-byte HMAC key from the active master key."""
        self._require_ready()
        master = self._keys[self._active_kid]
        return hmac.new(master, f"mcpcodeaway:{purpose}".encode(), hashlib.sha256).digest()


def sign_connect_ticket(
    *,
    signing_key: bytes,
    sub: str,
    tenant_id: str,
    upstream_name: str,
    ttl_seconds: int = 600,
) -> str:
    """Create a short-lived HMAC-SHA256 ticket for /auth/connect/{upstream}."""
    payload = {
        "sub": sub,
        "tid": tenant_id,
        "up": upstream_name,
        "exp": int(time.time()) + max(30, ttl_seconds),
    }
    raw = _b64u_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _b64u_encode(hmac.new(signing_key, raw.encode("ascii"), hashlib.sha256).digest())
    return f"{raw}.{sig}"


def verify_connect_ticket(
    *,
    signing_key: bytes,
    ticket: str,
    expected_upstream: str | None = None,
) -> dict[str, Any]:
    """Verify a connect ticket's signature, expiry, and optional upstream binding."""
    if "." not in ticket:
        raise CryptoError("Malformed connect ticket")
    raw, sig = ticket.split(".", 1)
    expected_sig = _b64u_encode(hmac.new(signing_key, raw.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected_sig):
        raise CryptoError("Invalid connect ticket signature")
    try:
        payload = json.loads(_b64u_decode(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoError("Corrupt connect ticket payload") from exc
    if not isinstance(payload, dict):
        raise CryptoError("Invalid connect ticket payload")
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        raise CryptoError("Connect ticket has expired")
    if expected_upstream is not None and payload.get("up") != expected_upstream:
        raise CryptoError("Connect ticket does not match requested upstream")
    if not payload.get("sub") or not payload.get("tid") or not payload.get("up"):
        raise CryptoError("Connect ticket missing required identity fields")
    return payload
