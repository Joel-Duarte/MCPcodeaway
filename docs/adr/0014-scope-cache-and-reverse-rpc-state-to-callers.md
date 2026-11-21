# ADR 0014: Scope cache and reverse-RPC state to the owning caller

- Status: Accepted
- Date: 2026-11-21

## Decision

Cache and session keys that represent a caller must include tenant identity,
even when the same subject value appears in more than one tenant. Reverse-RPC
callbacks must remain bound to the execution that created them across
concurrent WebSocket executions and gateway replicas. Execution admission must
wait only for its configured bounded interval and reject excess work rather
than queueing it without a limit.

## Context

Subject identifiers are not globally unique across tenants. WebSocket work can
also be interleaved by load balancing or concurrent execution, so process-wide
or connection-wide callback state can associate a response with the wrong
caller. An unbounded admission queue would let overload consume memory and
increase latency without giving callers a predictable rejection point.

## Consequences

Tests exercise the same subject in separate tenants, alternate executions
across independent gateway runtimes, and verify bounded saturation behavior.
Cache-key, callback-routing, and admission changes must preserve those
isolation and resource boundaries.
