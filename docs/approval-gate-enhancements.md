# Approval Gates and Central Enforcement — Proposal

**Status:** Phase A requester confirmation is implemented for direct MCP calls. External approvals and durable multi-step workflows remain future work.

## Purpose

Explore a gradual path from asking the person who initiated a tool call for simple input or confirmation to enforcing company approval policies across interactive and unattended agents.

The gateway can become a central enforcement point because it already authenticates each caller, applies RBAC/ABAC, routes calls to configured services, and records audit runs. It should not assume that every customer wants the gateway to own its approval directory, notification service, approver inbox, or long-running workflow engine. Begin with a narrow requester-input feature and make future enterprise approvals integrate cleanly.

## Terms and boundaries

- **Requester confirmation** means the caller confirms an action they initiated. It is consent to proceed, not a grant of permissions.
- **Authorization** means the gateway's policy decision that a principal may perform the action. A confirmation must never override a deny.
- **External approval** means a separately authenticated person or group, such as a manager or security reviewer, approves a pending action. The approver can differ from the requester.
- **Action execution** is a separate state from approval. `approved` does not mean the upstream action has completed.
- **MCP request state** is short-lived state for resuming one stateless MCP request. It is not an MCP session, a general conversation ID, or durable business workflow storage.
- **Business resource ID** identifies a domain object when a workflow refers to it across separate calls, such as a payment, export, or incident. It is optional and should be added only for a real workflow that needs it.

## Gate spectrum

| Gate | Example | Identity and state needed | Likely owner |
| --- | --- | --- | --- |
| Policy allow/deny | A finance user may read invoices but not issue refunds | Authenticated caller; tenant and resource policy | Gateway and upstream service |
| Missing input | Ask which environment to deploy to | Original caller and request-bound continuation state | MCP client collects input; gateway resumes the call |
| Requester confirmation | “Deploy version 4.2.1 to production?” | Original caller, exact action summary, single-use continuation | MCP client collects confirmation; gateway rechecks policy |
| Step-up authentication | Require fresh company login before exporting regulated data | Reauthenticated caller and required assurance policy | Company identity provider; gateway enforces |
| One-of-N external approval | Any one of five managers may approve a refund | Durable approval record; eligible approver identities; quorum 1 | Company approval workflow, enforced by gateway |
| Quorum or staged approval | Two of five managers, then one security reviewer | Durable record, distinct approvers, stage and quorum rules | Approval workflow and gateway policy |
| Break-glass | On-call admin temporarily isolates a compromised service | Elevated principal, reason, narrow scope, expiry, extra audit | Company incident process and gateway policy |

Every gate ends in an explicit outcome: allowed, denied, declined, cancelled, expired, invalidated by changed action/policy, or unable to execute. Missing or unavailable approval infrastructure must not silently permit a protected action.

## Rule-based and model-assisted decisions

Approval policy may eventually support automatic decisions, but the first version should use explicit rules and human/requester gates rather than a model that can independently approve actions.

- **Deterministic auto-approval:** An administrator defines a narrow envelope, such as approved read-only operations or a bounded low-impact update to an approved target. The gateway auto-approves only when every condition matches. A rule can never override RBAC/ABAC denial, a mandatory approval requirement, or an upstream authorization failure.
- **Model-assisted routing:** A classifier or model may recommend a risk class, identify missing evidence, summarize the action, or route a case to confirmation, an approver, or denial. Initially run it in observe/shadow mode or use it only to increase scrutiny. It must be able to abstain; uncertainty, missing inputs, and out-of-distribution cases go to a human or fail closed.
- **Model auto-approval:** Consider only later, for narrow action classes after a versioned evaluation demonstrates an acceptable false-approval rate and reliable behavior on holdout cases. A model score alone is not an authorization decision. Hard rules define the maximum automation envelope; model output cannot expand it.
- **Post-decision monitoring:** A separate monitor may flag unusual approvals, policy drift, or suspicious outcomes for review, pause future auto-approval, or open an incident. A post-action flag cannot undo an already-completed side effect. Any rollback or compensation is a new, separately authorized action.

An approval dataset must not contain only past approvals. That history is selected by previous policies and reviewers and does not show what should have been denied. Evaluation data should include approved, denied, declined, escalated, overridden, and outcome-labeled cases, including difficult and adversarial examples. Keep the dataset versioned with documented provenance and labeling rules; use held-out evaluation data instead of tuning a threshold against the same examples used to claim success.

Evaluate each action class and relevant user/resource segment separately. Track false approvals, false denials, escalation/abstention rates, reviewer overrides, downstream outcomes, and uncertainty. Approval errors have asymmetric costs, so optimize to the action's impact rather than one aggregate accuracy score. A gate owner may propose conditions or thresholds within a platform-admin-defined safety envelope; policy/model/threshold changes need versioning, replay against the evaluation set, staged rollout, and rollback. High-impact policy changes may require a second authorized reviewer.

Record the policy version, model/provider and model version when applicable, decision mode, reason codes, score or uncertainty where meaningful, approver/requester identities, overrides, and later action outcome. Minimize stored sensitive content; keep enough action evidence to explain and reproduce a decision without retaining credentials or unnecessary prompts. These practices align with NIST guidance to define human oversight, responsibilities, monitoring, and review for AI systems ([AI RMF Core](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/), [Generative AI Profile](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf)).

## Near-term: requester input via MCP `input_required`

The gateway uses the sessionless MCP `2026-07-28` request contract and now handles `input_required` for configured direct tool calls. This is a short-lived requester-confirmation round trip, not a durable approval service.

### Implemented first slice

1. Let an administrator mark a tool or operation as requiring requester confirmation, with a concise reason and a safe summary of the effect. Keep this as gateway policy metadata; it is not an upstream-provided security guarantee.
2. When the caller invokes the protected operation, authenticate and authorize the request as usual, then return an MCP `input_required` result with an elicitation asking for confirmation. Do not call the upstream yet.
3. On retry, authenticate and authorize again. Validate the answer and verify sealed request state against the tenant, subject, client identity, original method and tool, exact argument digest, policy digest/version, expiry, and a one-time nonce. Reject altered, expired, replayed, or wrong-principal continuations.
4. A decline or cancellation returns without dispatch. If confirmation is accepted, consume the one-time nonce before dispatch, invoke the original action once, and record the resulting execution under the caller's identity. Structured gateway logs record confirmation outcomes and policy changes. If the upstream call is mutating, use upstream-specific idempotency where available; never transparently replay it after an ambiguous timeout.
5. If the client did not declare the required elicitation capability, fail closed with a clear response. Do not call the tool and do not fall back to assuming consent.

The client renders the prompt to the requester. The onboarding flow and tool editor configure per-tool policies; changing them requires control-plane administrator authorization, increments the policy version, and invalidates pending request state. Form input is untrusted input and must not be used to collect passwords, tokens, or other secrets. The gateway cannot prove from the MCP response alone that a human clicked a button, so this is requester confirmation UX and must not be treated as separate enterprise approval.

This flow fits a user-driven call that can be resumed by its MCP client. It does not prove that a separate manager approved, and it is not a suitable durable wait mechanism for an agent process that may exit or lose its originating client.

### Code Mode and bulk-action boundary

The first approval-gated operation must be a **single, direct MCP tool call outside Code Mode**. Do not support confirmation or external approval for a tool invoked from `execute_code`, a Code Mode fan-out, or a bulk script in this first slice.

This boundary is necessary because Code Mode executes a script and its callbacks inside one gateway request. Returning `input_required` from a nested callback would require safely suspending and resuming the sandbox. Re-running the original script after confirmation could repeat earlier side effects, while keeping the sandbox and request open during a human wait is not durable. Static inspection of the script is not a security boundary: it cannot reliably prove which tools dynamic code will call or in what order.

Enforce the boundary in the host-side dispatcher immediately before every upstream call, not only by hiding a tool from `tools/list` or omitting its stub. Every call path, including direct tools, deferred discovery, `call_upstream_tool`, and Code Mode callbacks, must consult the same gateway policy. In the first slice:

- A direct call to an approval-gated operation can enter the requester `input_required` flow.
- A Code Mode callback to that operation is rejected before the upstream call and reports that the operation must be invoked directly. The script must not receive approval through a fabricated callback result.
- Do not claim that a multi-tool script is atomic. Ungated subcalls that completed before a gated callback was reached may already have taken effect; audit each subcall and explain this limitation. If a workflow needs all-or-nothing approval of several actions, reject that workflow until it is represented as a separately reviewed action plan.
- Never resume an arbitrary script from its source after approval. Sandbox checkpointing, if considered later, needs explicit replay-safety and persistence design; it is not part of the first input-required feature.

For a future approved bulk workflow, split **prepare** from **execute**. Preparation produces a bounded, immutable list of intended actions and a digest; approval binds to that exact list, policy version, tenant, caller, and expiry. A durable workflow executor—not a re-run of the agent's script—executes the approved steps with per-step authorization, idempotency, and audit. Define partial-failure and compensation behavior before allowing multi-step side effects. If a customer needs to approve an entire Code Mode-generated plan, this prepare/approve/execute flow is a separate feature and must not be simulated by prompting in the middle of Code Mode.

## External approvals: future central-enforcement path

For a manager or other reviewer, separate proposal, approval, and execution:

1. The gateway evaluates the request and creates a pending action record without dispatching the protected side effect.
2. The record stores the requester, tenant, target service/tool, sanitized human-readable action summary, exact action digest, policy version, requested quorum, eligible approver rule, expiry, and idempotency key. Keep the data needed to resume work; avoid storing credentials or unnecessary payloads.
3. The approval resolver selects approvers from configured company identity/directory data. A rule may mean any one member of a group, `k` distinct people from a group, a requester’s manager, or an ordered sequence of groups. The requester may be excluded from approving their own action.
4. Notify eligible approvers through a configured channel. A notification is a pointer to the pending record, not proof of identity or approval. An approver signs in through the company identity provider and submits an approve/deny decision against that exact record.
5. Recheck eligibility, quorum, expiry, policy version, and the stored action digest. Execute only the approved action, once. If the action or relevant policy changed, invalidate the approval and request a new decision.
6. Record requester identity, approver identities, decisions, timestamps, policy, execution result, and any retry or compensation. Do not expose one tenant's approval records to another tenant.

OIDC is useful for authenticating the approver and obtaining a stable issuer/subject. OIDC alone does not define a manager hierarchy, approval groups, quorum policy, email delivery, or a workflow inbox. Group or manager resolution may need provider-specific directory APIs, claims, SCIM data, or an existing approval product. Prefer references to identity-provider subjects/groups over maintaining a second manually managed user directory.

The gateway can be the enforcement point while an existing enterprise workflow product owns approver routing and its UI. A gateway-owned approval service is a larger product choice: it adds durable state, notification adapters, approver access, quorum and delegation rules, retention, escalation, and operational support. Consider it only after a real customer workflow requires a common approval inbox across upstream services.

## User-driven and unattended agents

The caller type changes how work is resumed, not whether policy applies.

### User-driven agent

- For a one-request confirmation, return `input_required`; the MCP client prompts the person and retries the call with its response and the sealed request state.
- For a manager approval, persist the pending action. The requester may leave; the gateway must not hold the original HTTP request, worker slot, or sandbox open while waiting.
- When the external decision arrives, expose the result through the task/workflow status and notify the originating client or orchestrator if it is still connected.

### Unattended agent

- Do not rely on a human-facing elicitation handler being available. An unattended client may have no person to prompt and may not declare elicitation support.
- Persist the agent workflow checkpoint and pending action. Apply policy before dispatch, then wait on a durable workflow signal, queue, callback, or task status.
- SSE or WebSocket can provide low-latency status to a connected orchestrator, but the persisted record remains the source of truth. On reconnect, the orchestrator resumes from a cursor or queries pending work; it must not depend on having kept one stream open.
- The approval service should signal the agent runner/orchestrator. Whether that causes an existing agent process to resume, a new process to start from a checkpoint, or the orchestrator to make the next tool call depends on that runtime. The gateway should publish a stable event/result contract rather than assume a specific agent lifecycle.

An approval event is not automatically permission to resume arbitrary agent code. The orchestrator should resume only the workflow associated with that pending action, re-establish the principal and policy context, and present the completed action result to the agent.

## Approval and action lifecycle

Keep the human decision separate from the side effect. A useful durable state model is:

```text
proposed -> pending_approval -> approved -> executing -> completed
                         |          |          |
                         |          |          +-> execution_failed
                         |          +-> invalidated
                         +-> denied | declined | cancelled | expired
```

`approved` means the decision threshold was met. It does not mean execution began or succeeded. The API and event model should report both approval status and action status, including a final result or safe error. For multi-step work, each later action is independently authorized and may create another approval. Where compensation is possible, represent it as a new action with its own policy and audit trail; do not silently roll back or continue with unreviewed side effects.

## Delivery choices after a decision

| Mechanism | Good for | Limitation |
| --- | --- | --- |
| MCP `input_required` | Requester answers during the original tool call | Client-driven round trip; not an external approver queue or durable background job |
| MCP Tasks extension | A supported MCP client needs to poll or receive status for long-running `tools/call` work, including work waiting for input | Optional extension; clients must support it, task access must be reauthorized per request, and task storage/retention still need design |
| SSE/WebSocket event | A currently connected UI or agent orchestrator wants fast status updates | Not durable delivery; reconnect and query state |
| Webhook/queue signal | A workflow engine or agent runner should resume after an outcome | Requires signed/authenticated delivery, retries, deduplication, and an idempotent receiver |
| Pollable approval/workflow API | Broad compatibility with agent runtimes and external approval tools | More client work and polling load; use bounded intervals and pagination |

MCP Tasks can represent long-running work and mid-task input for clients that opt in. It is not a manager directory, approval policy, or notification provider. Treat task IDs as sensitive handles, bind each task to its tenant and owner, and authorize every `tasks/get`, `tasks/update`, and `tasks/cancel` request.

## Onboarding and editing surface

Keep imported protocol capabilities separate from gateway policy:

- Import and display what the upstream actually exposes, beginning with tools and their input/output schemas.
- Let an administrator curate which operations are enabled and configure gateway-side risk class, requester confirmation, or an external approval rule.
- Show a preview of the exact effect and required approver rule. Require a clear action summary for confirmation prompts.
- Keep upstream credentials, identity-provider configuration, approval routing, and tool policy distinct so an imported server cannot grant itself approval bypasses.
- Preserve per-tenant ownership, audit policy changes, and make policy version changes invalidate pending approvals where they affect the decision.

Do not build a separate approval inbox UI for the first input-required slice. First integrate the requesting client's prompt for requester confirmation and, for external approval, use a company's existing workflow UI if available. A gateway approver inbox is a later option if customers need one place to approve actions across many connected services.

## Suggested delivery phases

### Phase A — Requester input (implemented)

The gateway supports per-tool requester-confirmation policy in onboarding, the tool editor, and upstream configuration. Direct MCP calls use `input_required`; the dispatcher enforces policy for every invocation, including Code Mode callbacks. Sealed state binds the tenant, subject, client, tool, exact argument digest, policy digest/version, expiry, and a one-time stored nonce. Retries authenticate and authorize again; the dispatcher checks that the confirmed policy still matches before the upstream call. Clients without form elicitation fail closed, and the admin live-test route will not bypass a configured gate. The approval golden journeys cover acceptance, decline and replay, argument and identity mismatch, policy invalidation, missing client capability, Code Mode rejection, concurrent duplicate confirmation, and admin-only policy edits. Expiry and persistence-backend failure paths remain follow-up coverage.

### Phase B — Approval contract

Before adding a manager-approval UI, define the pending-action schema, lifecycle, approver resolver interface, one-of-N and quorum semantics, requester/approver separation, expiry, notification contract, event delivery, idempotency, and tenant-scoped audit. Pilot with one real upstream and an existing approval system if possible.

### Phase C — Durable workflows and agent resumption

Add shared persistence, recovery/lease rules, task or workflow APIs, notification adapters, webhook/queue delivery, cancellation, and orchestrator resume contracts. Test two gateway replicas, approver outages, duplicate events, restart during approval/execution, stale approvals, and no duplicate side effects.

### Phase D — Productized approval operations

Only after a validated workflow, decide whether to build a gateway-owned approver inbox, policy editor, delegation/escalation controls, and reports across services. If model-assisted approval is considered, first run it in shadow mode against versioned, independently reviewed evaluation data; compare risk/coverage, false-approval rates, abstentions, reviewer overrides, and downstream outcomes per action class. Do not fine-tune a model or allow it to auto-approve as part of Phase A. Measure approval completion time, abandonment, duplicate prevention, and operational cost.

## Protocol and design references

- [MCP 2026-07-28 multi-round-trip request behavior](https://ts.sdk.modelcontextprotocol.io/v2/migration/support-2026-07-28) — `input_required`, client retry, and opaque request state.
- [MCP 2026-07-28 overview](https://blog.modelcontextprotocol.io/posts/2026-07-28/) — stateless request lifecycle and multi-round-trip requests.
- [MCP Tasks extension specification](https://tasks.extensions.modelcontextprotocol.io/specification/2026-07-28/tasks) — durable task state, polling, input, cancellation, and task authorization.
