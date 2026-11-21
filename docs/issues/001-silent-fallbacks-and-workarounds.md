# Issue 001: Replace silent fallbacks and UI workarounds with explicit handling

- Status: Open
- Priority: P2

## Problem

Several paths hide failed or unavailable operations behind empty catches,
optimistic state, static role data, or compatibility styling. These behaviors
make degraded state difficult to distinguish from success and leave fallback
assumptions embedded in the UI.

## Work

- Gate the `devLogin` fallback on an explicit development-auth configuration;
  an unavailable external OIDC provider must not silently change the login
  mechanism.
- Replace the onboarding wizard's hard-coded role fallback with a clear
  unavailable state so the operator does not submit stale role choices.
- Surface load failures for vault secrets and upstream performance data, or
  make their optional status explicit in the UI.
- Report failed demo-traffic tier changes instead of retaining an optimistic
  selection that may not match server state.
- Replace empty catches for unavailable demo-only endpoints with explicit
  disabled/unsupported state where those features are shown.
- Revisit the global meter/progress width override and fix the component or
  layout contract at its source if possible.
- Replace the benchmark's unused `UpstreamRegistry` construction with a
  deliberate benchmark invariant or remove it.
- Replace the event-loop yield in the telemetry subscriber test with explicit
  subscriber registration synchronization.

## Acceptance criteria

- Failure and unsupported states are distinguishable from successful or
  persisted state.
- No authorization fallback can activate unless development authentication is
  explicitly enabled.
- Tests synchronize on subscriber registration without relying on a scheduler
  yield.
- The benchmark no longer constructs an unused registry solely for pattern
  similarity.
