---
name: delete-comment
description: Audit source comments, classify them as tautology, excuse, stale, or decision record, and apply the repository's deletion, issue, ADR, and comment-ratchet policies.
---

# Delete Comment

Use this skill when asked to audit or remove code comments.

## Workflow

1. Read repository instructions and identify all tracked source, test, config,
   and frontend files that contain code comments. Do not count prose in
   Markdown or docstrings as code comments.
2. Review comments in their surrounding code. Classify each one:
   - **Tautology:** restates syntax, names, UI structure, test assertions, or
     behavior already apparent from the code. Delete it.
   - **Stale:** describes removed or changed behavior, APIs, paths, or
     assumptions. Delete it.
   - **Excuse:** rationalizes a workaround, silent failure, stale fallback, or
     other hack. Delete the comment and file an actionable issue describing
     the implementation change. Use the repository issue tracker when a
     suitable connector is available; otherwise add a clearly marked issue
     record in the repository's existing issue location.
   - **Decision record:** explains an important, non-obvious product,
     architecture, security, protocol, or testing choice. Move the decision
     and its rationale into `docs/adr/` before deleting the source comment.
3. Preserve compiler/interpreter directives and shebangs when removing ordinary
   comments. They are tool metadata, not explanatory prose.
4. Maintain the comment ratchet in `scripts/check_comments.py` and
   `scripts/comment-baseline.json`. The checker allows existing fingerprints
   but rejects any new comment. Removing comments should shrink the baseline;
   never refresh it to admit new comments.
5. Report counts by classification, removed physical lines, retained directive
   comments, created ADRs/issues, and any portions of the repository that could
   not be inventoried. Estimate potential total savings from comments still
   present, clearly separating that estimate from actual lines removed.

## Repository-specific checks

- The comment inventory supports Python, JavaScript/TypeScript, Vue, CSS/SCSS,
  HTML, shell, YAML, TOML, nginx-style configuration, SQL, and related config
  files. Extend the scanner if the repository adds another source language.
- `scripts/agent_preflight.py` enforces the baseline check. Keep the scanner
  compatible with the repository's pre-CI flow.
- Follow existing ADR and issue formats. Do not put new local-only instructions
  under `.agents/`.
