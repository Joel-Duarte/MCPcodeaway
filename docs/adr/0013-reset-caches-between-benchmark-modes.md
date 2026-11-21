# ADR 0013: Reset benchmark caches between execution modes

- Status: Accepted
- Date: 2026-11-21

## Decision

The load benchmark clears caches when it switches execution modes and uses a
pool of distinct user identities for repeated requests.

## Context

The benchmark compares cache hit rates and behavior across modes. Carrying
cache entries from one mode into another would bias those measurements, while
reusing only one identity would fail to model distinct callers.

## Consequences

Each mode starts from a comparable cache state, and the workload includes
multiple caller identities.
