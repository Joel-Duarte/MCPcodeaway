# ADR 0008: Keep mutating tools directly exposed in hybrid mode

- Status: Accepted
- Date: 2026-11-21

## Decision

Hybrid mode exposes read-only tools through Code Mode orchestration and exposes
mutating tools as direct MCP tools.

## Context

The hybrid mode combines Code Mode's orchestration surface with direct tool
definitions for writes, keeping mutating operations explicit at the MCP tool
boundary.

## Consequences

The hybrid tools/list response contains the Code Mode tools plus accessible
mutating tools. This composition is a mode contract, not an incidental
implementation detail.
