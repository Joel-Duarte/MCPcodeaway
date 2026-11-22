# Security freshness acceptance

This document records effective behavior from the gateway implementation and deterministic
two-runtime tests. It is not a live identity-provider or Kubernetes test.

| Authorization input | Default lifetime | Maximum lifetime | Refresh behavior | Failure behavior | Revocation delay |
|---|---:|---:|---|---|---|
| OIDC JWKS | 300 seconds, fixed in `OidcTokenValidator` | 300 seconds | Each runtime caches its own JWKS. An unknown `kid` forces an immediate refresh. | A failed refresh is logged as `auth.jwks_refresh_failed` and propagates; an expired cached set is not used as fallback. | A removed key already present in a cache can remain accepted for up to 300 seconds after the last successful fetch. Unknown new keys refresh immediately. |
| Entra group membership | `entra_group_cache_ttl_seconds`: 300 seconds | 3,600 seconds; configured range is 30–3,600 | Graph results are cached per tenant and object ID in each runtime cache; expiry triggers a new Graph resolution. | Graph errors, malformed responses and timeouts raise. The validator converts resolution errors to empty groups and incomplete membership; group-dependent policy denies. A Graph 401 claims challenge is not forwarded by this catch path. | Grant or removal can take effect per replica at cache refresh, up to the configured TTL. Refresh failure does not return stale membership. |
| Admin session claims | `admin_session_ttl_seconds`: 28,800 seconds (8 hours) | No code-level maximum; the configured TTL is capped by the source identity token's numeric `exp` when present | Self-contained signed claims are checked on each request; roles and groups are not reloaded. | Expired or invalidly signed sessions verify as absent. There is no per-user revocation record. | Role or group changes can remain effective until session expiry. Removing the signing key invalidates sessions using it immediately; retaining a previous key for rotation overlap continues to accept them. |

## Evidence

`tests/test_authorization_freshness.py` uses signed RSA tokens, mock JWKS and Graph responses,
monotonic clocks, and two separately constructed `GatewayRuntime` objects. The tests establish:

- A valid key is accepted; an unknown `kid` triggers refresh; removed-key acceptance ends at cache
  expiry; failed refresh fails closed; and two runtime caches rotate independently.
- Entra grants and removals converge independently at cache expiry. Graph 401 (including a
  claims challenge), 403, 429, 5xx, malformed data and timeout do not grant access on incomplete
  membership. The validator's suppression of the Graph challenge is explicit in a test.
- A signed admin session is accepted by a second runtime with the same key, retains the original
  role claims after an identity role change, is capped by source-token expiry, and is invalid after
  its signing key is removed.

This is a two-instance simulation. Requests were not routed through live Kubernetes gateway pods
for this acceptance. Each runtime owns independent JWKS and Entra cache state, which proves the
documented local-cache convergence behavior without claiming identity-provider propagation timing.

## Policy decision

The implementation defaults remain unchanged. Operators must account for up to five minutes for a
removed signing key, up to the configured Entra group cache TTL (maximum one hour), and up to eight
hours for admin claims at the default session setting. Production should choose and record an
acceptable Entra TTL and admin session lifetime before rollout. The code does not currently impose
an upper bound on `admin_session_ttl_seconds`; source-token expiry still caps each issued session
when the source token contains `exp`.
