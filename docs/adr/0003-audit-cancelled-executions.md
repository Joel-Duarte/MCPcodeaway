# ADR 0003: Record cancelled executions before propagating cancellation

- Status: Accepted
- Date: 2026-11-21

## Decision

When a caller disconnects or cancels an execution, record its cancelled outcome
in the audit trail, then propagate cancellation to the request task.

## Context

Cancellation is a meaningful execution outcome for audit and operational
analysis. Propagating it before recording would erase that outcome.

## Consequences

Cancelled work remains visible in audit records while normal cancellation
semantics still reach the caller and task runtime.
