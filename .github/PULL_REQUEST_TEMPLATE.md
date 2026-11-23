## Summary

<!-- Describe what this PR changes and why. -->

## Type of Change

- [ ] Bug fix
- [ ] New feature / enhancement
- [ ] Documentation / CI / tooling
- [ ] Security hardening

## Verification Checklist

- [ ] `./scripts/pre-ci.sh --fast` passes locally
- [ ] `./scripts/pre-ci.sh --full` passes locally (for backend, frontend, or benchmark changes)
- [ ] New or updated tests cover behavioral changes
- [ ] Security invariants preserved (`/admin/*` RBAC, `validate_upstream_url`, `OutputRedactor.redact_text()`)
- [ ] Documentation / `README.md` / `docs/` updated if behavior or configuration changed
