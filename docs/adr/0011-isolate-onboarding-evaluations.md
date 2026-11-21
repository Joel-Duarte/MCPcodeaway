# ADR 0011: Isolate onboarding evaluations from live gateway state

- Status: Accepted
- Date: 2026-11-21

## Decision

Onboarding replay and A/B evaluation runs use an ephemeral staging runtime with
an in-memory store and cache. The candidate upstream is explicitly enabled for
the evaluation caller within that isolated runtime.

## Context

Evaluations should exercise the gateway path without mutating live config or
audit state. The candidate can be temporarily added to the isolated registry
so caller authorization and tool discovery are evaluated consistently.

## Consequences

Evaluation state is discarded after the run, and changes to candidate access
must not affect the live runtime.
