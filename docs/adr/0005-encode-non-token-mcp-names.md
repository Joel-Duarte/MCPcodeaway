# ADR 0005: Encode MCP names that cannot appear in HTTP token values

- Status: Accepted
- Date: 2026-11-21

## Decision

The HTTP binding encodes MCP tool names that contain non-ASCII characters or
characters outside the permitted HTTP token set using RFC 2047 Base64 form.

## Context

The `Mcp-Name` value is carried in an HTTP header. Names outside the HTTP token
character set cannot be sent there as plain values.

## Consequences

Valid MCP names remain representable in the gateway's HTTP request headers.
