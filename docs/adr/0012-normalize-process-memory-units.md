# ADR 0012: Normalize process memory units by platform

- Status: Accepted
- Date: 2026-11-21

## Decision

Normalize the `ru_maxrss` process statistic to bytes before exposing runtime
telemetry. Linux reports this value in KiB; macOS reports bytes.

## Context

The same field has different units across supported platforms, so returning it
without normalization would make reported process memory incomparable.

## Consequences

Callers can treat the gateway's peak-memory telemetry as bytes on both
platforms.
