# Operations acceptance

The gateway uses existing JSON structured logs, process-local metrics, readiness endpoints and
durable PostgreSQL records. This acceptance adds transition and blocked-retry events without
introducing a monitoring service. Log events contain event types, safe statuses, error classes and
opaque operation IDs; they do not include tool arguments, request bodies, access tokens or secrets.

| Condition | Existing signal | Detection method | Operator action |
|---|---|---|---|
| PostgreSQL unavailable or pool failure | `database.postgres_startup_failed` if startup cannot initialize; `database.postgres_readiness_changed` on runtime state transition; `/ready` returns 503; process-local dependency health | Alert on startup/readiness failures and structured `status=unavailable`; inspect recovery for `status=available` | Check database reachability, TLS/hostname, credentials and connection limits. Do not route sensitive writes while readiness is false. |
| Sensitive write outcome is uncertain | `upstream.failed` now includes `outcome_unknown` and `journal_operation_id`; caller result also includes uncertainty; existing upstream error metric | Search structured logs by operation ID and request correlation; inspect journal status | Treat as potentially completed. Reconcile with authoritative upstream state before any manual action. Never replay automatically. |
| Journal remains `executing` | Durable `sensitive_write_audits` row; blocked retry emits `audit.sensitive_write_retry_blocked` | Read-only query below, comparing `updated_at` with the expected operation duration | Check whether a worker is still active, then reconcile against upstream records. Do not mark successful based only on gateway logs. |
| Same-correlation retry blocked | `audit.sensitive_write_retry_blocked` with operation ID and current journal status | Search by request correlation in application logs and inspect the journal row | Reuse the operation ID to investigate. A new correlation is a distinct operation and is not proof that the original did not run. |
| Terminal journal persistence failed | `audit.sensitive_write_status_failed`, with operation ID, intended terminal status and safe error type | Search structured logs and inspect journal status | Assume the side effect may have happened if dispatch began. Reconcile with the upstream system before manually resolving. |
| Approval lifecycle | Durable `tool_approvals` statuses and events for requester confirmation, manager request/decision, execution failure, policy update; list/read APIs are admin-authorized | Use the existing admin approval endpoints and filter for pending, approved, rejected, expired, invalidated, executing or recovery-pending statuses | Confirm requester/approver context and expiry. Do not treat approval as evidence that an upstream side effect completed. |
| OIDC/JWKS refresh failure | `auth.jwks_refresh_failed` with safe HTTP status and exception class; token validation fails closed | Search structured logs; distinguish from ordinary invalid-token denials | Check issuer reachability, TLS, DNS and signing-key publication. Expired cached keys are not used as fallback. |
| Entra Graph lookup failure / incomplete membership | `auth.group_overage_resolution_failed`; identity is marked incomplete and group-dependent policy denies | Search event and failed request correlation; inspect Graph availability and challenge response in identity-provider diagnostics | Restore Graph/OBO access. A 401 challenge is currently suppressed by the validator; the request stays denied. |

## Read-only journal inspection

`sensitive_write_audits` contains operation ID, tenant and caller identifiers, upstream/tool names,
request digest, correlation, dedupe key, status and timestamps. It does not store raw arguments.
There is no gateway API to list these rows, so use an approved read-only database principal and
apply the tenant's access policy when querying. Example:

```sql
SELECT operation_id, tenant_id, upstream_name, tool_name, correlation_id,
       status, created_at, updated_at
FROM sensitive_write_audits
WHERE status IN ('prepared', 'executing', 'outcome_unknown')
ORDER BY updated_at ASC;
```

The current read API retrieves a journal record by dedupe key, not by listing unresolved writes.
Providing a tenant-scoped read-only unresolved-journal endpoint with appropriate admin/viewer RBAC
is the narrowest future product change; this acceptance does not add an administration surface.

## Safe handling by state

- `prepared`: the protocol has not completed the atomic claim, so upstream dispatch has not begun.
  Confirm the row remains prepared and no active request owns it before retrying through the normal
  gateway path.
- `executing`: dispatch may be in progress or may have crossed the network boundary. Do not retry
  or change the row based only on age; inspect worker/request state and authoritative upstream data.
- `outcome_unknown`: never automatically replay. Require authoritative upstream evidence to decide
  whether the action occurred and use the approved recovery procedure.
- `succeeded` and `failed`: terminal states; the same correlation is not replayed. A failed status
  means dispatch was classified as not having an uncertain side effect; investigate the recorded
  error before starting a new, intentional action.

If terminal persistence failed, the row can remain `executing` even when the caller received an
uncertain result. Gateway logs and local journal state alone cannot establish upstream completion.
Do not invent a success result or manually clear an unresolved entry without authoritative
upstream evidence and the organization's change/audit process.
