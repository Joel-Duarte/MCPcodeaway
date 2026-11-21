# ADR 0006: Re-encrypt secrets after master-key rotation

- Status: Accepted
- Date: 2026-11-21

## Decision

When a stored secret is successfully decrypted with a non-active master key,
re-encrypt it with the active key and persist the new ciphertext.

## Context

Key rotation needs to migrate stored values without requiring users to
re-enter every secret. Successful reads provide a safe point to migrate each
value.

## Consequences

Secrets migrate incrementally as they are read, allowing older keys to be
retired after their remaining values have been re-encrypted.
