# ADR 0001: Upstream requests do not share cookies

- Status: Accepted
- Date: 2026-11-21

## Decision

Gateway requests to upstreams must not inherit cookies accumulated by the shared
HTTPX client. Construct each request without the client's cookie jar and remove
any caller-supplied Cookie header before sending it.

## Context

The client pool is shared across concurrent callers. HTTPX's `build_request`
merges its cookie jar, so a `Set-Cookie` response from one upstream call could
otherwise cause a later call from another caller to carry that cookie.

## Consequences

The gateway retains connection pooling while keeping upstream cookie state
isolated between requests and callers.
