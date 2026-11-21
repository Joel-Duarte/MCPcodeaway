# ADR 0010: Test production feature gates without production credentials

- Status: Accepted
- Date: 2026-11-21

## Decision

Tests for production-profile feature gates may use the production feature
profile while running in a sandbox environment with the local verifier. They
must not provision production secrets to test profile behavior.

## Context

Feature-gate behavior and production credential provisioning are separate
contracts. Coupling a feature-gate test to real production secret requirements
would obscure what the test exercises and require sensitive setup.

## Consequences

Tests can verify production feature gates with local credentials, while startup
validation for actual production deployments remains covered separately.
