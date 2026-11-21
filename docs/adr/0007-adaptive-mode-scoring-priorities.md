# ADR 0007: Prioritize quality in adaptive mode scoring

- Status: Accepted
- Date: 2026-11-21

## Decision

Adaptive model scoring ranks success rate or error rate first, token efficiency
second, and latency as a lower-priority signal. Traditional MCP's additional
multi-turn reasoning cost may add a capped penalty to its score.

## Context

An adaptive choice should prefer reliable execution over efficiency gains, then
use token savings and latency to distinguish similarly reliable modes.

## Consequences

The scorer's ranking and caps form an explicit product decision. Changing that
order or the multi-turn penalty requires revisiting this record.
