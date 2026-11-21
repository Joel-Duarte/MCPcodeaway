# ADR 0004: Evidence imports exclude prompts and answers

- Status: Accepted
- Date: 2026-11-21

## Decision

Imported evidence records may contain operational metadata but must not contain
prompt, answer, response, or message content.

## Context

Evidence ingestion is not intended to become a second store for user
conversations or model output. Rejecting those fields at import keeps the
evidence contract narrow.

## Consequences

The import endpoint rejects keys that indicate conversational content before
persisting an evidence record.
