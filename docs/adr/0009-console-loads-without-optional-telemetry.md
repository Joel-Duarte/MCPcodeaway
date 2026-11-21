# ADR 0009: Keep the console available when optional services fail

- Status: Accepted
- Date: 2026-11-21

## Decision

The console may continue loading its audit and analytics views when runtime
telemetry is disabled or unreachable. Route middleware also allows navigation
to continue during a gateway outage so the view can render its connection
state.

## Context

Runtime telemetry is not enabled in every profile, and a temporary gateway
outage should not make the console shell inaccessible. Core pages can still
explain the failure and provide useful data when their own endpoints work.

## Consequences

Runtime metadata is optional for console navigation and audit/value views.
Failures of required data endpoints continue to use their page-level error
state.
