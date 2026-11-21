# ADR 0002: Keep Prometheus labels bounded and tenant-free

- Status: Accepted
- Date: 2026-11-21

## Decision

Gateway Prometheus metrics use bounded labels and never include tenant
identifiers.

## Context

Metrics are deployment-wide and commonly readable by operations identities.
Tenant labels would expose identity data and create cardinality proportional to
the number of tenants.

## Consequences

Metrics stay useful for deployment-level operations without exporting tenant
identifiers or unbounded per-tenant series.
