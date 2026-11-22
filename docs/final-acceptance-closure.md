# Final Acceptance Closure

## 1. Approval tests added and results

- `tests/test_approval_golden.py`: deterministic coverage for an expired decision, decision-store write failure, authorization revoked after approval, policy-version change after approval, and duplicate/conflicting decisions. Existing concurrent duplicate-approval assertions now verify only one accepted decision and no conflicting state.
- `tests/test_postgres_acceptance.py`: spawned two independent gateway runtimes against disposable PostgreSQL and raced approve vs. deny. Exactly one durable decision won.
- `uv run pytest -q tests/test_approval_golden.py` — **14 passed**.
- The PostgreSQL race ran as part of the TLS acceptance command below — **passed**.

## 2. Browser E2E command and actual result

- Started local mock services: `docker compose up -d --build gateway mock-backends`.
- Ran `E2E_BASE_URL=http://127.0.0.1:8000 npx playwright test` in `web/` — **3 passed**, covering desktop console navigation/onboarding/settings, mobile at 375 px, and tablet/wide desktop responsiveness. **0 skipped.**
- Stopped and removed the local demo containers, network, and volume after the run.

## 3. PostgreSQL TLS acceptance command and actual result

- `MCP_TEST_POSTGRES_PORT=55439 uv run python scripts/local_acceptance.py --with-postgres-tls --with-postgres-processes --with-process-crash --with-tcp-tls` — **112 passed** in the focused TLS/PostgreSQL/process/TCP suite, followed by **5 passed** hard process-crash tests. The disposable database and volume were removed by the runner; **0 skipped**.
- The default host port had produced migration connection resets in earlier attempts. The run on a separate host port completed. The earlier reset's precise cause was not established.

## 4. Defects found and exact fixes made

- Fixed `scripts/local_acceptance.py`: cross-process acceptance uses the restricted `gateway_limited` runtime DSN, while hard-crash acceptance requires the isolated `gateway_test` DSN. The runner now invokes crash tests separately with `MCP_TEST_POSTGRES_MIGRATION_DSN`; role separation remains intact.
- Corrected `docs/gateway-flow-map.md`: personal credential management is API/integration-driven; MCPcodeaway does not provide an end-user identity or credential-management portal. Documented the existing PostgreSQL OAuth reconnect limitation. Updated the prior product report to remove the obsolete UI defect.
- No approval runtime defect was reproduced; the added tests passed without changing approval behavior.
- `./scripts/pre-ci.sh --fast` — **passed**, including preflight, Ruff format/lint, golden flows and benchmark regression (**14 tests passed**).

## 5. Remaining blockers

None for this targeted closure pass. Per-user OAuth tokens remain process-local in PostgreSQL deployments, and real customer IdP/upstream behavior remains outside local acceptance; these are documented MVP/deployment limitations, not blockers to the requested checks.

## 6. Final status

**ACCEPTED WITH EXPLICIT LIMITATIONS**
