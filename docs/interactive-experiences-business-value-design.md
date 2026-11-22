# Interactive MCP Experiences and Business Value Design

**Status:** Product and architecture design; workflow authoring is an onboarding experience, while execution attribution remains explicit in v1.
**Evidence date:** 22 November 2026.  
**Scope:** Integration generation, hosted MCP tool execution, optional MCP Apps, workflow attribution, business-value telemetry and administration.

## 1. Executive summary

MCPcodeaway can add three related but independent capabilities without becoming an identity portal or workflow engine:

1. **Integration onboarding:** connect and inspect an upstream, then let an administrator build one or more business workflows from the discovered catalog tools. The builder stores a versioned integration definition that references those tools and current gateway policy; it does not copy tool schemas, credentials or authorization rules.
2. **Interactive MCP experiences:** optionally publish a constrained declarative UI rendered as an MCP Apps resource. The ordinary MCP tool remains available and is the fallback. UI actions are normal gateway tool calls.
3. **Business-value measurement:** let an administrator define named workflows and/or standalone tool outcomes during onboarding. A simple flow builder arranges selected catalog tools in an administrator-chosen display order, identifies required evidence and terminal completion, and records a human-time baseline and its provenance. The visual order describes the business activity; it does not force gateway dispatch order. In v1, the caller explicitly selects a workflow using a gateway tool. MCPcodeaway creates and owns an execution ID, associates later calls explicitly, verifies required evidence and the configured outcome, and records at most one value event per logical completion. Automatic workflow classification or passive sequence matching is a later capability.

The gateway exposes `mcpcodeaway_start_business_execution` for either a published multi-tool workflow or a standalone tool outcome. Both kinds return a gateway-owned execution ID. Only calls carrying that explicit ID can earn business-value credit. Classic and App calls use a gateway-owned attributed-call wrapper; Code Mode carries the ID on `execute_code` and the gateway associates each dispatcher callback with the execution. Hybrid uses both paths; Auto uses whichever route it selects. Missing, invalid, expired or lost context means ordinary tool dispatch may still happen, but it receives no business-value attribution. The ID never authorizes a tool.

Selecting a workflow starts attribution but is not evidence of completion. Completion requires a version-pinned, administrator-approved predicate evaluated against a successful gateway-observed result (and, for protected writes, successful completion of the existing sensitive-write journal). If the upstream effect may have occurred but the response or durable evidence is ambiguous, the workflow becomes `outcome_unknown`; it does not count toward value and is not blindly replayed.

Retain Classic-versus-Code token/payload savings as a clearly labelled **technical efficiency** metric. Add business value separately. Keep existing dispatcher security, authentication, authorization, approvals, sensitive-write journaling, tenant isolation and upstream authorization as the authorities.

The first end-to-end slice should begin in upstream onboarding: an admin builds and publishes a readable multi-tool workflow from the discovered catalog, specifies required evidence and terminal completion, and assigns a sourced manual-time baseline. At runtime, the caller explicitly selects the workflow from descriptions returned by MCPcodeaway and starts it; two or more calls carry the gateway ID through every execution mode. The gateway verifies evidence and the terminal result, then one deduplicated business-value event appears in the report. The builder order is descriptive, not an execution constraint. Cross-mode, concurrency and crash-safety acceptance are exit gates before this foundation is declared complete. MCP Apps and polished analytics follow independently.

## 2. Findings from the current repository

This is a focused implementation review, not a general capability audit. Status refers to current code and the cited tests/docs; tests were not run for this design-only work.

| Concern | Current behavior and evidence | Status |
|---|---|---|
| Onboarding and AI configuration | `web/app/pages/onboarding.vue` handles connect/inspect, policy/mode configuration, visual workflow authoring, comparison and save. The onboarding tool catalog includes the per-tool schema digest used to validate a published workflow reference. `build_agent_onboarding_prompt` in `src/gateway/upstream_clients.py` asks about transport/spec, auth, operation curation, RBAC/ABAC and execution mode, and offers the existing configuration paths. | The workflow builder can define multiple flows from enabled catalog tools, their descriptive order, required successful-call evidence, optional bounded result predicates, a terminal result predicate, and a sourced human-time baseline. It lists existing published workflows, adds flows or replaces one by reusing its name, and preserves other definitions. `tests/test_upstream_onboarding.py` verifies schema digests; `web/tests/onboarding-workflow-builder.spec.ts` covers authoring and publication. |
| OpenAPI and MCP discovery | `_load_openapi_spec`, `_discover_openapi_from_spec`, `_call_openapi`, MCP discovery methods and `DiscoveredTool` normalize tools. `DiscoveredTool` contains qualified name, description, input/output schema, mutability, routing fields, annotations and guidance. `ToolCatalog` owns discovered tools and digests. | Implemented. `tests/test_openapi_generation_golden.py` and onboarding/discovery tests cover generation and invocation. API documentation is not an authorization source. |
| Tool exposure | `to_mcp_tool_definition` exposes the existing schema; `GatewayRuntime.tools_for_mode` returns mode-specific definitions/stubs. Deferred discovery adds tools through search results. | Implemented. No workflow context argument or Apps UI resource metadata exists. |
| Execution modes | `tools_for_mode` builds Classic direct tools, Code Mode `execute_code` plus Python stubs, or Hybrid direct mutating tools plus Code Mode reads. Auto resolves execution/discovery through `resolve_dynamic_mode`; the effective route is in `/mcp`. | Implemented. See `src/gateway/server.py`, `src/gateway/catalog.py`, `src/gateway/adaptive_execution.py`; coverage includes `tests/test_gateway_modes_e2e.py` and `tests/test_adaptive_execution.py`. |
| Code Mode calls | `_execute_code_inner` creates host callbacks for accessible tools. Each callback calls `HostDispatcher.invoke_tool`; policy and argument checks occur at dispatch. The outer script and subcalls are recorded in telemetry; Monty does not persist a resumable business workflow. | Implemented. Code Mode can carry workflow context by closing over the validated ID and execution record during that one `execute_code` request. Cross-request persistence still needs an explicit ID. |
| Request/correlation context | `mcp_endpoint` sets request ID, tenant and caller in a `ContextVar` and clears it after the HTTP request. Sensitive-write journal correlation currently uses the request ID. MCP HTTP is documented and tested as sessionless. `X-Intent`/request ID are not suitable durable workflow context. | Implemented but request-scoped only. Reuse for logging/traces, not multi-call attribution. Do not replace sensitive-write correlation with a workflow ID. |
| Identity, policy and approvals | MCP authentication derives `CallerIdentity`; catalog visibility and `PolicyEvaluator` authorize tools. `tool_effects` and `tool_approval_policies` define read/write/high-impact and requester/manager requirements. Protected calls flow through approval handling and `HostDispatcher`. | Implemented. Workflow membership cannot grant visibility or permission. See `src/gateway/auth.py`, `config.py`, `server.py`, `dispatcher.py`, `tests/test_approval_golden.py`. |
| Sensitive writes and uncertain outcomes | `HostDispatcher` prepares/claims/finalizes a durable sensitive-write journal. It records digests and status, not raw arguments. If upstream success is followed by journal failure, it reports an uncertain result; the claim blocks blind duplicate replay. PostgreSQL uniqueness coordinates replicas. | Implemented safety control; must remain authoritative. `src/gateway/state.py`, `src/gateway/migrations.py`, `docs/postgres-state-architecture.md`, `tests/test_sensitive_write_audit.py`, `tests/test_process_crash_acceptance.py`. |
| Telemetry and savings | `TelemetryRecorder.record_execution` persists redacted outer runs/subcalls, duration, token estimates and model name when supplied. `/audit/value` and `/audit/analytics` aggregate them. The dashboard has technical efficiency, model cost estimates, mode mix and latency. | Implemented, with estimates and query/retention limits. No workflow execution or business outcome records. |
| Storage | `audit_runs` stores run JSON; `dynamic_upstreams` stores the current upstream config by name; `gateway_settings` stores keyed JSON; `adaptive_evidence` is tenant scoped. PostgreSQL schema changes use ordered, transactional migrations in `src/gateway/migrations.py`; SQLite bootstraps local tables in `state.py`. | Implemented. Current tables do not provide immutable integration revisions, workflow execution ownership or unique workflow completion events. PostgreSQL is sufficient; no new service is needed. |
| Hosted MCP Apps | `src/gateway/mcp_protocol.py` defines the current protocol metadata/version and Tasks extension. `/mcp` handles `server/discover`, `tools/list`, `tools/call` and task methods. There is no Apps capability, `resources/list`/`resources/read`, `ui://` retrieval or tool-to-UI metadata path. | Not implemented in gateway. Apps requires protocol changes, not only an onboarding UI. |
| Console | Relevant pages are `onboarding.vue`, `upstreams.vue`, `approvals.vue`, `audit.vue`, `analytics.vue`, `index.vue` and `settings.vue`. | Definition authoring is in the catalog-based onboarding workflow builder. The Business Value page is report-only. MCP Apps publication remains out of scope for this view. Preserve `DESIGN.md`, `web/AGENTS.md` and `.agents/rules/ui-craft.md` standards. |

### Existing token-savings assessment

`TelemetryRecorder.record_execution` estimates raw upstream tokens as `ceil(raw_bytes / 4)`, estimates returned tokens with `_approx_tokens`, and estimates schema savings as `classic_schema_tokens - schema_tokens`. These are byte/token heuristics and schema-footprint comparisons, not the downstream model tokenizer or the client's exact prompt serialization. The payload delta is calculated per run without a controlled same-task Classic counterfactual; the aggregate can include runs across modes. `/audit/value` multiplies estimated saved tokens by one configured input-token price. `/audit/analytics` applies selected model price assumptions to estimated token use and saved input tokens. Neither can see provider discounts/caching or downstream inference cost unless separately supplied.

`src/gateway/comparator.py` does run a defined side-by-side scenario in Classic and Code Mode; `src/gateway/ab_testing.py` can perform replay and optional live model arms. Those are more comparable experiments, but their scope is the chosen scenario/model and they are not evidence that every production request was paired. Execution duration is observed gateway time, not user labor time.

**Recommendation:** retain and reposition this feature as Technical Efficiency. Clarify estimate methodology and provider-price assumptions; preserve its history and mode comparisons. Do not turn it into business value or remove it. Report completion, time baseline, review effort and labor assumptions in a separate Business Value view.

## 3. Workflow setup and explicit execution contract

### 3.1 Onboarding workflow builder

Workflow authoring belongs in the upstream onboarding flow after catalog discovery, where the administrator already has the available tools and schemas in view. The first builder is intentionally small and visual: create multiple named workflows for one integration, choose tools from that integration catalog, arrange them in a readable order, set required evidence and a terminal outcome, and record manual baseline minutes with source/confidence. Show a plain-language summary of the measured work before publish. Do not require administrators to author or understand a JSON manifest. The gateway may still store the validated declarative manifest behind this UI.

The selected order is a documentation and organization aid only. It does not impose ordering constraints on calls at runtime, and a workflow may be completed when its unordered required evidence and deterministic terminal predicate are satisfied. In v1, attribution starts only after the caller explicitly selects a published workflow ID; the gateway does not infer workflow type by observing tool sequences. This avoids ambiguous matches when one integration has several workflows with shared tools. A later feature may add sequence-based or other automatic identification with explicit ambiguity handling, but it is not part of this release.

The Business Value page is a report: it explains the measures and displays attributed completions, baselines, provenance and cost coverage. Create and edit workflow definitions in onboarding, not in a raw JSON editor on the report page.

### 3.2 Model-declared intent and workflow execution contract

#### Options and recommendation

| Option | Fit with the actual gateway | Decision |
|---|---|---|
| Model sends workflow ID in arbitrary HTTP header or custom host extension | Current gateway reads some operational headers, but downstream clients/model tool loops do not provide a portable documented way to set a custom header per tool call. It will not reliably span clients or Code Mode callbacks. | Reject. |
| Put execution context only in MCP `_meta` | MCP metadata is a standard envelope, but ordinary model tool schemas do not generally let the model set per-call MCP metadata. Depending on a host to synthesize/preserve a gateway key would violate the portability requirement. | Do not rely on it. It can carry protocol metadata but not be the attribution contract. |
| Add a reserved field directly to every upstream tool schema | JSON Schema 2020-12 supports composition, references and closed objects. Injecting a field into arbitrary root schemas can change `$ref`, `allOf`/`oneOf`/`anyOf`, `additionalProperties` and dialect semantics. | Reject as unsafe general transformation. |
| Expose gateway-owned attributed-call wrapper tools around original tools | Keeps each upstream schema intact as a nested `arguments` schema, while the wrapper owns execution ID and closed outer envelope. Ordinary original tools remain available. Code Mode uses its gateway-owned `execute_code` envelope. | **Recommend.** More tool definitions for workflow participants, but preserves validation and makes attribution explicit. |
| A. Prohibit standalone value for tools that are terminal tools in any published workflow | Prevents one known overlap but is incomplete: a tool with standalone value can be an intermediate workflow step or an outcome tool whose role changes by context. It also removes valid independent standalone use. | Reject as the MVP attribution rule; optionally flag terminal overlaps during admin validation. |
| B. Require an explicit workflow or standalone execution before any attributed call | Generalize the start tool with `kind`; each wrapper call must carry the resulting execution ID and is credited only under that execution's selected definition. Missing/invalid context never falls back to standalone value. Reuses the same execution/event machinery. | **Recommend for MVP.** Small schema addition, deterministic omission behavior, and supports per-tool standalone value without ambient session inference. |
| Infer workflow from tool sequence or use ambient “active workflow” per caller | The HTTP endpoint is sessionless; parallel tasks from one caller and retries would be ambiguous. Sequence guessing creates false attribution. | Reject. |

The gateway tool is a business-outcome start contract, exposed only when eligible published definitions exist. Its `workflow_id`/`tool_value_id` fields enumerate the eligible published IDs; its generated description lists each ID with the administrator-provided name and description. The model sees this list through `tools/list`, selects a definition, and MCPcodeaway validates it. With deferred tool discovery, the definition list is still shown by the start tool; when a referenced tool is discovered, the gateway returns its attributed wrapper alongside the unchanged ordinary tool. There is no second LLM to classify free-text intent.

```json
{
  "name": "mcpcodeaway_start_business_execution",
  "arguments": {
    "kind": "workflow",
    "workflow_id": "handle_customer_support_request"
  }
}
```

For `kind: "workflow"`, `workflow_id` is required. For `kind: "standalone_tool"`, `tool_value_id` is required and selects an administrator-defined standalone outcome. The unselected kind's ID is forbidden. There is no no-op intent declaration: when no definition matches, the model calls ordinary tools and no business-value execution or record is created. The start tool returns a cryptographically random execution ID (at least 128 bits), selected definition ID/kind, pinned revision and expiry. Unknown/inactive IDs are rejected. The model cannot set a baseline, success predicate, execution ID, tenant or owner.

For each tool referenced by a published workflow or standalone outcome, expose an additional gateway-owned wrapper tool. A standalone execution accepts only its one selected tool and one initial logical invocation; same-call retries use the same call key, while a new call key is rejected. Its outer arguments are fixed and closed; the existing business schema is nested unchanged:

```json
{
  "name": "mcpcodeaway_attributed__support_create_ticket",
  "inputSchema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
      "business_execution_id": { "type": "string", "format": "uuid" },
      "arguments": { "$ref": "#/$defs/upstream_arguments" }
    },
    "required": ["business_execution_id", "arguments"],
    "additionalProperties": false,
    "$defs": {
      "upstream_arguments": {
        "type": "object",
        "properties": { "ticket_id": { "type": "string" } },
        "required": ["ticket_id"],
        "additionalProperties": false
      }
    }
  }
}
```

The original schema is copied as a whole into `$defs.upstream_arguments`; no keywords within it are edited. The wrapper uses MCP's JSON Schema 2020-12 default, preserving an explicit supported `$schema` on the nested schema. Local references retain their schema-resource scope: preserve an existing `$id`; if none exists, assign a deterministic synthetic `$id` to the nested original schema so `#/$defs/...` and root references resolve within that schema. Resolve/bundle approved external references before wrapping. Reject attributed-wrapper publication when a schema declares an unsupported dialect, contains unresolved external references/dynamic references the configured validator cannot preserve, or fails round-trip equivalence checks. Do not weaken or rewrite `additionalProperties`, `$defs`, `$ref`, `allOf`, `oneOf`, `anyOf`, nested objects or `required` fields. The model still sees the exact original business schema under `arguments`.

The original upstream tool stays exposed for ordinary use. Only its gateway wrapper carries business attribution. A call to the original tool without a wrapper remains technically observable but never earns standalone or workflow value. Thus forgetting, omitting, losing or supplying an invalid execution ID cannot fall back to standalone credit. Each wrapper call has exactly one explicit execution ID and can be attributed to only that execution's pinned kind/definition; a workflow execution can never be converted into a standalone outcome, and a standalone execution cannot complete a workflow. On wrapper invocation the gateway validates owner/state/scope and kind, validates `arguments` against the original schema, strips the wrapper envelope, and invokes the original tool through existing authorization/dispatcher code. No wrapper fields are sent upstream or included in exact-action approval digests. If safe wrapping is impossible, do not expose an attributed wrapper for that tool: retain the ordinary tool, mark business attribution unsupported for it, and fail publication if the workflow requires it.

The model selects only IDs and descriptions provisioned by the administrator. `workflow_id` is required for workflow execution; `tool_value_id` is required for standalone outcome execution. There is no LLM intent-classification step, no free-text intent field and no untracked start call. When no configured definition matches, do not call the start tool; ordinary tools continue as-is without business-value credit.

#### Execution context propagation by mode

| Mode/path | Context carrier | Gateway behavior |
|---|---|---|
| Classic upfront | A gateway attributed-wrapper tool takes `business_execution_id` plus nested original `arguments`; the ordinary original tool remains available. | Validate the wrapper envelope and unchanged original schema, validate owner/state/scope, strip the envelope, then call `call_direct_tool_for_identity` → `HostDispatcher.invoke_tool`. |
| Classic deferred | Search/hydration returns the same wrapper for discovered tools covered by the selected definition, alongside ordinary definitions. | Same attributed dispatch. Calling the ordinary tool is allowed but cannot earn any business-value credit. |
| Code Mode | The gateway-owned `execute_code` schema adds a fixed `business_execution_id` property; it is not part of the Python source. | Validate once at the outer request; bind the execution to `_make_wrapper`; each callback to `HostDispatcher.invoke_tool` receives a unique workflow call ID and is linked to the execution. Do not trust IDs embedded in code or callback arguments. |
| Hybrid | Code Mode envelope for reads; attributed-wrapper tools for direct writes; ordinary tools remain available. | Same execution record across both paths. Writes continue through current confirmation/approval and sensitive-write journal. |
| Auto | Both direct attributed wrappers and the `execute_code` envelope are exposed when needed. | Current Auto route chooses path as today; attribution reaches either dispatcher path and does not affect authorization or routing score. |
| MCP App | The view calls the start tool, then invokes a mapped attributed-wrapper tool through host-mediated `tools/call`, passing the returned ID. | Same API and validation as model invocation. UI state alone is not trusted or durable. No Apps dependency for model-initiated executions. |

A start call begins attribution but does not reserve or expand tool access. A workflow execution permits wrappers only for its participating tools; a standalone execution permits only its selected tool value. A mismatched wrapper returns an invalid-context error. The caller may call the ordinary original tool separately, subject to normal authorization, but it receives no business-value credit. Each attributed call still checks current tenant, RBAC/scopes, ABAC, server ceiling, effect policy, approvals, rate limits and upstream credentials. Definition membership is an attribution boundary, never an allowlist grant.

#### Ownership, lifetime and concurrency

Persist owner tenant, caller subject, client ID where stable, workflow ID, immutable definition revision, created/expiry timestamps and state. Validate every supplied execution ID against all owner fields and the authenticated identity. A guessed, cross-tenant, cross-user, expired, revoked, completed or unknown ID returns a generic invalid-context error without disclosing another owner's record. IDs are random and rate-limited, but authorization checks do not depend on secrecy.

Use a fixed bounded lifetime (initial proposal: 30 minutes idle, 24 hours absolute) and explicit terminal transitions. Calls do not indefinitely extend the absolute lifetime. A new user task starts a new execution; IDs are not reset/reused. Workflow retries reuse the same execution and same per-dispatch call key only where the existing request/recovery contract identifies a retry. A standalone execution accepts only its one initial logical invocation and retries of that same call key; it rejects any second distinct call key. A completed execution ID cannot be resumed. An `outcome_unknown` ID cannot dispatch another state-changing call until reconciled or explicitly abandoned.

Sequential state-changing calls are supported. Acquire an atomic durable write lease per execution for each dispatch; hold it across that call's approval wait and release/advance only after a terminal known result is durably recorded. A workflow can therefore perform write A, then write B, then verify a final outcome. Each write independently passes current dispatcher authorization, effect classification, requester/manager approval and sensitive-write prepare/claim/finalize. Pending approval is recorded on that call while the execution remains `in_progress`; do not allow the next state-changing call until the pending call is approved and completed, denied/expired, or reconciled. Parallel reads may be allowed. Parallel state-changing calls are rejected in the MVP; they are not needed to support multiple sequential writes. An expired lease after process failure does not authorize replay: reconcile the linked journal first. Authorization is re-evaluated every call.

JSON-RPC request IDs correlate requests and responses; they are not intrinsically durable business-operation idempotency keys. The supported-client contract is: each distinct outer logical MCP request uses a distinct request ID; retransmission of an unresolved request retains the same ID and exact request body; reuse of an ID with a different body is rejected; a deliberate repeat after completion uses a new ID and is a new logical invocation. These are gateway/client protocol requirements, not semantics guaranteed by JSON-RPC itself. For an attributed execution, bind `(tenant_id, business_execution_id, outer_request_id)` to one request-body digest before dispatch and reject digest mismatch. Persist a durable random `workflow_call_id` and a stable gateway-derived `call_key` for each dispatcher invocation, incorporating tenant, execution, outer request ID, request-body digest, effective tool, clean-arguments digest and callback occurrence ordinal. Code Mode callbacks within one `execute_code` request share its outer ID; assign ordinals deterministically in callback invocation order, so exact retransmission of the same outer body recreates the same ordinal sequence and call keys. Persist the request binding and call intent before dispatch. An exact retransmission of an unresolved request resolves to the existing call IDs/result or unknown status; it does not trigger blind redispatch. Internal retry is permitted only under the existing recovery/idempotency contract. This prevents duplicate gateway attribution events; it does not promise upstream exactly-once execution.

The outer request ID is not a per-dispatch identifier or an upstream idempotency key. Validate this contract through the supported real-client matrix using an instrumented MCP transport/proxy: capture IDs and bodies for separate calls, inject a lost response and exercise retransmission, mutate a reused ID's body to confirm rejection, and repeat a completed action to confirm a fresh ID. For Code Mode, replay the same `execute_code` outer body and assert identical callback ordinal/call-key assignment. A client path that cannot preserve the ID/body on unresolved retransmission is not supported for value attribution; its ordinary authorized tool access remains available, and no upstream exactly-once guarantee is made. Code Mode's existing telemetry run/subcall IDs remain linked as telemetry references: the outer `audit_runs.run_id` may only be known after execution completes, and a telemetry subcall ordinal is not the durable workflow identity. Record those identifiers when available, together with the outer request ID and `workflow_call_id`. The dispatcher invocation is linked directly to `workflow_call_id`; when the sensitive-write journal creates an operation ID, store that ID on the call event as a separate reference. Keep the journal's existing request-scoped correlation and idempotency behavior unchanged. Internal retries of the same dispatcher invocation retain its `workflow_call_id`; transport re-delivery resolves through `call_key` and must not dispatch again while prior outcome is unresolved. This linkage provides workflow-event idempotency without claiming upstream exactly-once behavior.

## 4. Workflow lifecycle, evidence and attribution

### 4.1 States and event model

Recommended execution states:

```text
started → in_progress → completed
                  ↘ failed
                  ↘ incomplete (explicitly abandoned or known partial stop)
                  ↘ expired
                  ↘ outcome_unknown
```

A workflow execution remains `started` until a linked call occurs; a standalone execution records the first accepted selected-tool call key as its one logical invocation. `approval_status` is an orthogonal per-call state (`none`, `pending_requester`, `pending_manager`, `approved`, `denied`, `expired`); a pending approval leaves execution state `in_progress` and blocks later writes. Denial/expiry closes that call and may leave the workflow `incomplete` or `failed` according to the definition. Known upstream rejection can mark a step failed; retryable failures may leave execution in progress. A timeout after dispatch, lost response, gateway crash, sensitive-write journal terminal-write failure or workflow evidence persistence failure can produce `outcome_unknown`.

Keep an append-only `workflow_execution_events` history (or equivalent immutable event rows) for `started`, tool attempt/result, approval state, completion evidence, expiry, abandon and reconciliation; maintain a current execution projection for fast validation. Do not store raw credentials, full tool arguments or arbitrary conversation payloads. Store tool reference, schema/manifest revision, run/journal IDs, request digest, outcome status, timestamps, and only the minimal redacted result fields needed for the completion predicate (for example a hash or redacted ticket ID where audit policy allows). Use the existing audit redactor and retention rules.

### 4.2 Completion evidence

Only configured, published rules can create a completion event. Completion eligibility requires every configured `required_evidence` entry to be satisfied by a successful logical call and its optional result predicate, plus the terminal `completion` predicate. An invocation is evidence only after the existing dispatch path returns success; protected writes also require sensitive-write journal status `succeeded`. Retries of the same `workflow_call_id` count once. Missing calls or failed result predicates keep the execution incomplete/in progress and cannot earn the workflow baseline. MVP evidence rules should be small and deterministic:

- `terminal_tool_result`: a named workflow tool returns a configured JSON path with an expected type/value (e.g. `/ticket/id` exists and is a non-empty string); optionally require an additional status field/value.
- `verified_followup`: a configured read/status tool confirms the created/updated entity reached an expected status.
- `human_confirmed`: an explicit user response through a supported, authenticated confirmation interaction, stored as *user-reported*, not machine-verified outcome.

No arbitrary code or LLM judgment in predicates. Validate tool/path/type/value against discovered output schemas and admin review. Tool outputs are untrusted data; JSON pointer traversal is structural, bounded and does not execute content. The model saying “done,” a free-form intent, a UI button click that is not a confirmed user action, a 2xx response alone, or a pending/accepted response do not satisfy completion.

When a result may have completed the upstream effect but MCPcodeaway cannot durably save the terminal evidence, keep the workflow `outcome_unknown`, emit no business-value record, and return the existing uncertain-write semantics. Do not silently count it as completed. The sensitive-write journal remains the authority for whether a protected operation ran, and its operation ID is linked to the workflow event; do not replace its request correlation/dedupe key with a workflow ID. Reconciliation may use the existing recovery/status contract or a configured follow-up verification tool. If evidence remains unavailable, an administrator can close it as unknown/incomplete; that is not a value event.

Retries and duplicate handling:

- One gateway-created workflow execution has at most one workflow completion event; one standalone execution has at most one standalone completion event. Both use a unique execution-scoped completion key. A standalone execution atomically claims its first distinct call key; a retry of that same key resolves through the stored call/journal state, while a second distinct key is rejected.
- Same outer JSON-RPC request ID plus same request-body digest returns stored call result/status and per-dispatch IDs; same ID with a different body is rejected. It cannot create duplicate call or completion events.
- A retryable read may be retried under current policy. A write is retried only under the existing idempotency/recovery contract.
- Multiple sequential state-changing calls within one workflow are allowed and serialized one at a time. An execution may have multiple successful writes, but only after all required evidence and its configured terminal evidence are satisfied can it create the single workflow completion event. Parallel state-changing calls are rejected in the MVP. A second terminal effect may be recorded as conflicting/unknown according to the completion policy; it never increments workflow value twice.
- Conflicting terminal evidence changes state to an inspectable conflict/unknown condition; it never increments completed value twice.

### 4.3 Per-tool business value and workflow business value

Both are optional and independently defined:

- **Standalone tool outcome:** a configured tool result predicate plus standalone baseline, created by starting a `kind: "standalone_tool"` business execution. It is one logical invocation of the selected tool: exactly one initial attributed dispatcher invocation is permitted. A retry is accepted only as the same `call_key`/logical call and follows the existing sensitive-write journal and upstream recovery/idempotency rules. A second distinct `call_key` under that standalone execution ID is rejected, even if the first invocation failed or completed; start a new standalone execution for a new attempt/task. Its attributed wrapper must carry that execution ID. An ordinary tool call without explicit business execution is never credited. For several distinct operations representing one business process, define a workflow instead.
- **Workflow outcome:** one configured workflow completion rule and baseline. Calls in `participating_tools` are attributable, but only calls named by `required_evidence` are prerequisites to completion; each may also require a validated result predicate. All required evidence plus terminal `completion` must pass. Multiple associated tool calls and Code Mode subcalls contribute evidence/timing, but only one completion event is valued.

Explicit execution kind determines attribution: workflow wrappers can only attach to workflow executions; standalone wrappers can only attach to the selected standalone tool execution. A tool call with no context, an invalid/expired/lost ID, or an ordinary original tool name has technical telemetry only and cannot fall back to standalone value. An incomplete workflow receives no standalone credit for any of its calls. This removes ambiguity when the model forgets the ID. Disallow nested/overlapping value attribution in the first release; an admin validation error flags definitions that claim the same terminal event.

Example: four calls (find customer, read account, get order history, create ticket) and a Code Mode `execute_code` outer request represent **one** completed five-minute workflow only if each required lookup evidence predicate and the terminal ticket completion predicate are met. They do not represent four five-minute tasks. A standalone `create_ticket` outcome requires a separate `kind: "standalone_tool"` start and its attributed wrapper call; a direct unwrapped call is never valued.

### 4.4 Cross-execution outcome identity

Execution-level deduplication is mandatory. Cross-execution deduplication is optional and may be enabled only when the upstream exposes a stable logical outcome identity. A manifest may define a small identity recipe such as:

```json
{
  "dedupe": {
    "scope": "tenant_upstream_operation",
    "namespace": "support.create_ticket.v1",
    "identity_parts": [{ "source": "terminal_result", "path": "/ticket/id" }]
  }
}
```

For a resource creation, the identity can combine tenant, upstream, semantic operation namespace and created resource ID. For a state-changing order operation, order ID alone is insufficient; require an upstream operation/event ID, version ID, or stable idempotency/event ID that distinguishes each legitimate update. Repeating an update to the same order at a new version is a new outcome. An idempotency key only deduplicates when it is the same logical upstream operation key, not merely because the tool is idempotent.

Store a digest of the canonical identity, not raw sensitive identifiers. Enforce uniqueness on `(tenant_id, upstream_name, dedupe_namespace, outcome_identity_digest)` when the digest is non-null, using a partial unique index or equivalent database constraint. Every row also has a unique `(tenant_id, execution_id, completion_key)`, so a retransmission cannot credit twice within one execution. The namespace is administrator-owned and stable across manifest revisions when semantics stay the same; change it when the credited outcome semantics change. If no sufficiently stable identity exists, leave the identity digest null and report “deduplication: per execution only.” If a cross-execution uniqueness conflict occurs, link the new execution to the existing outcome as duplicate and create no additional value event. Never claim cross-execution exactly-once outcomes from tool names or resource IDs alone.

### 4.5 Baselines, confidence and formulas

Pin these fields in each published workflow/tool revision: `manual_baseline_minutes`, `baseline_source` (`administrator_estimate`, `sampled_manual_measurement`, `validated_external_measurement`), `confidence` (`unvalidated`, `sampled`, `validated`), source note/evidence reference, effective date, optional `loaded_labor_cost_per_hour`, currency and attribution owner. AI can suggest values, never promote confidence or claim validation. Existing executions keep the definition/baseline revision they started with; a baseline edit creates a new revision and affects only new executions.

For completed logical outcomes only:

```text
gross_manual_minutes = completed_count × pinned_manual_baseline_minutes
net_time_avoided_minutes = max(0, gross_manual_minutes
                                  − measured_human_review_minutes
                                  − measured_correction_and_rework_minutes)
gross_estimated_labor_value = gross_manual_minutes / 60 × pinned_labor_cost_per_hour
net_estimated_labor_value = net_time_avoided_minutes / 60 × pinned_labor_cost_per_hour
known_cost_adjusted_labor_value = net_estimated_labor_value − measured_automation_cost
net_estimated_value = known only when all material automation-cost categories are covered
net_ROI_pct = available only when net_estimated_value is known and complete_cost > 0
```

If review/rework is configured as an expected average instead of captured minutes, report that deduction as an estimate. Clamp/display negative net time as zero avoided time and surface review/rework separately; never hide it. Show a known-cost-adjusted subtotal only with a **partial cost coverage** label when material costs are unknown. Do not label it net value or ROI. `net_estimated_value` is null/unavailable unless the administrator has declared all material categories covered (for example gateway, upstream and downstream LLM, as applicable). ROI is unavailable when cost is missing or zero. Do not add mixed currencies without a dated exchange-rate source.

Evidence labels:

- **Estimated:** administrator baseline/rate or model-proposed values; show source, confidence, count and revision.
- **Observed:** gateway completion evidence, tool attempts, failures, retries, approvals and elapsed gateway time. Observed execution is not validated labor saved.
- **Validated:** manual baseline and review/rework are supported by a customer-measured sample or accepted external evidence. Keep evidence reference, sampling period and owner.

Gateway duration is service elapsed time, not worker labor time avoided. Downstream LLM cost is unknown unless client/provider billing telemetry is imported. MCPcodeaway infrastructure cost may be shown only if attributable metering is available; upstream API fees need vendor usage/billing data. Missing costs are “not available,” never zero.

## 5. Integration manifest and examples

### 5.1 Shape, lifecycle and authorities

Use a versioned manifest for optional integration metadata, references to the existing upstream/tools, optional experiences, optional workflows/value assumptions, and validation/publication records. Continue to use `UpstreamConfig`, `ToolCatalog`, `tool_effects`, `tool_approval_policies`, current auth/delegation settings and `HostDispatcher` as operational authorities. The manifest references tools by qualified name plus schema digest; it does not re-declare schemas, HTTP routes, credentials, role rules or approval policy.

Authorship and assignment:

- **Onboarding agent proposes:** display metadata, descriptions, tool references, workflow steps/tool membership, UI component tree and mappings, baseline suggestions/source proposal, completion predicate proposal, confirmation/review UX.
- **Administrator reviews/owns:** business meaning, selected operations, baseline evidence/confidence, tool scope, mappings, outcome predicate, whether a human confirmation is acceptable, proposed links to existing approval policies, CSP/domain exceptions, publication.
- **Gateway validates/assigns:** manifest schema version, canonical names/schema digests, IDs and immutable revision, tenant/upstream binding, normalized paths/types, `required_evidence` tools are a subset of `participating_tools`, evidence predicates match the referenced output schemas, completion tool is participating, references to configured effects/approvals, validator version/hash, publisher/time, and workflow execution IDs/events.

The admin review must state in plain language what the baseline measures. `participating_tools` means eligible calls that may be attributed; `required_evidence` means successful logical calls that must be present before completion is eligible; optional evidence result predicates constrain what those calls must return; `completion` proves the terminal business outcome. The workflow description and baseline note must describe the same activity. A ticket-only baseline is represented as a ticket-only standalone outcome, not as a workflow that implies customer/account/order review.

Lifecycle: generated draft → validation errors/warnings → editable preview with synthetic data → administrator approval → immutable published revision. An edit forks a new draft/revision. Rollback appends a publication event that points the current pointer to an older valid revision; it never edits the old version or erases intervening publication history. Active execution remains pinned to the version and baseline at start. If a tool/schema/effect policy changes, mark dependent drafts/revisions stale; require revalidation and republishing before starting new attributed executions. Existing calls still pass current authorization and may be denied after policy changes.

Canonical content hash: serialize only the authored integration-definition payload using RFC 8785 JSON Canonicalization Scheme (JCS), then SHA-256 the UTF-8 bytes. Exclude publication/validation records, assigned revision/status, timestamps, publisher identity, validator metadata, and every derived field including `content_sha256` itself. Store the hash beside the immutable version and in append-only publication history; never hash a structure that includes its own hash.

### 5.2 Example A: ordinary integration, no workflows or UI

```json
{
  "manifest_version": "1.0",
  "integration": {
    "id": "inventory",
    "display_name": "Inventory",
    "upstream_name": "inventory_api",
    "status": "published",
    "revision": 1
  },
  "tool_refs": [
    { "name": "inventory_api__get_stock", "schema_sha256": "<catalog digest>" },
    { "name": "inventory_api__list_locations", "schema_sha256": "<catalog digest>" }
  ],
  "experiences": [],
  "workflows": [],
  "publication": {
    "validator_version": "1",
    "content_sha256": "<manifest hash>",
    "published_by": "admin-subject",
    "published_at": "2026-11-22T10:00:00Z"
  }
}
```

### 5.3 Example B: integration with optional MCP App

```json
{
  "manifest_version": "1.0",
  "integration": {
    "id": "orders",
    "display_name": "Order lookup",
    "upstream_name": "orders_api",
    "status": "published",
    "revision": 2
  },
  "tool_refs": [
    { "name": "orders_api__get_order", "schema_sha256": "<catalog digest>" }
  ],
  "experiences": [
    {
      "id": "order_detail",
      "resource_uri": "ui://mcpcodeaway/orders/order-detail/r2",
      "renderer_schema": "mcpcodeaway-ui/1",
      "tool": "orders_api__get_order",
      "fallback": "Use orders_api__get_order and read its text/structured result.",
      "input_mapping": { "order_id": "/form/order_id" },
      "components": [
        { "type": "text_input", "id": "order_id", "label": "Order ID", "required": true },
        { "type": "button", "label": "Look up order", "action": "get_order" },
        { "type": "record", "data": "tool_result" }
      ],
      "csp": { "connect_domains": [], "resource_domains": [], "frame_domains": [] }
    }
  ],
  "workflows": [],
  "publication": { "validator_version": "1", "content_sha256": "<manifest hash>" }
}
```

### 5.4 Example C: multi-tool business workflow with value assumption

```json
{
  "manifest_version": "1.0",
  "integration": {
    "id": "support_desk",
    "display_name": "Support Desk",
    "upstream_name": "support_api",
    "status": "published",
    "revision": 5
  },
  "tool_refs": [
    { "name": "support_api__find_customer", "schema_sha256": "<digest>" },
    { "name": "support_api__get_account", "schema_sha256": "<digest>" },
    { "name": "support_api__get_order_history", "schema_sha256": "<digest>" },
    { "name": "support_api__create_ticket", "schema_sha256": "<digest>" }
  ],
  "experiences": [],
  "workflows": [
    {
      "id": "handle_customer_support_request",
      "name": "Handle customer support request",
      "description": "Complete a support complaint by identifying the customer, reviewing account and order history, then creating a ticket. The five-minute baseline covers these lookups and ticket creation.",
      "participating_tools": [
        "support_api__find_customer",
        "support_api__get_account",
        "support_api__get_order_history",
        "support_api__create_ticket"
      ],
      "required_evidence": [
        {
          "tool": "support_api__find_customer",
          "min_successful_calls": 1,
          "result_when": { "path": "/customer/id", "type": "non_empty_string" }
        },
        {
          "tool": "support_api__get_account",
          "min_successful_calls": 1,
          "result_when": { "path": "/account/id", "type": "non_empty_string" }
        },
        {
          "tool": "support_api__get_order_history",
          "min_successful_calls": 1,
          "result_when": { "path": "/orders", "type": "array" }
        }
      ],
      "completion": {
        "kind": "terminal_tool_result",
        "tool": "support_api__create_ticket",
        "success_when": { "path": "/ticket/id", "type": "non_empty_string" },
        "require_sensitive_write_journal_status": "succeeded"
      },
      "dedupe": {
        "namespace": "support.create_ticket.v1",
        "identity_parts": [
          { "source": "terminal_result", "path": "/ticket/id" }
        ]
      },
      "business_value": {
        "manual_baseline_minutes": 5,
        "baseline_source": "administrator_estimate",
        "confidence": "unvalidated",
        "loaded_labor_cost_per_hour": 25,
        "currency": "EUR",
        "effective_from": "2026-11-22",
        "review_minutes": { "kind": "capture_actual_when_available" },
        "correction_minutes": { "kind": "capture_actual_when_available" }
      },
      "execution": {
        "absolute_ttl_minutes": 1440,
        "idle_ttl_minutes": 30,
        "allow_parallel_reads": true,
        "serialize_writes": true
      }
    }
  ],
  "publication": {
    "validator_version": "1",
    "content_sha256": "<manifest hash>",
    "published_by": "admin-subject",
    "published_at": "2026-11-22T11:00:00Z"
  }
}
```

The example output paths are illustrative and must be replaced or confirmed against the discovered upstream output schemas before publication. This is not a general workflow DSL. The order of `participating_tools` preserves the administrator’s visual organization in the onboarding builder, but its runtime meaning is membership only: it bounds which calls can be associated with the execution. `required_evidence` separately lists successful logical tool calls needed to qualify for completion, with optional result predicates; the model/agent may call participating tools in any order, and there are no runtime ordering or branching rules. The support example's five-minute baseline explicitly measures successful customer lookup, account review, order-history lookup, and ticket creation. If an administrator intends to value ticket creation alone, they must define a separate ticket-only standalone outcome with a ticket-creation baseline rather than publish this workflow baseline.

For MVP, each `required_evidence` entry names one participating tool, a bounded minimum count of successful logical calls (initially one), and optionally one deterministic `result_when` predicate. Counts use unique call keys, so retries do not create extra evidence. This is an unordered set of prerequisites, not a sequence.

`completion` is the deterministic terminal predicate; it cannot by itself waive missing required evidence. Every required evidence item and the terminal predicate must be satisfied for one workflow completion event. A required tool call counts once by logical `call_key`, not once per retry. Result predicates are optional and, when present, are bounded structural checks validated against that tool's output schema. No manifest ordering, branching, arbitrary scripts or general step DSL is introduced. The optional `dedupe` recipe is only enabled if the configured upstream response supplies stable outcome evidence; for this ticket-creation example the created ticket ID is combined with the tenant, upstream and `support.create_ticket.v1` namespace. Gateway ID, caller/tenant owner, call events and evidence are runtime records, not manifest fields. Existing effect/approval policies are linked by operation identity and always consulted at dispatch.

## 6. MCP Apps design

### 6.1 Current official protocol

SEP-1865 is final; the official `modelcontextprotocol/ext-apps` repository marks the independently versioned `2026-01-26` specification stable. Apps are optional and negotiated using `io.modelcontextprotocol/ui`. The standard uses predeclared `ui://` resources with MIME type `text/html;profile=mcp-app`, a tool metadata association (commonly `_meta.ui.resourceUri`), `resources/list` and `resources/read`, and a bidirectional MCP JSON-RPC bridge between sandboxed iframe and host. Hosts mediate app tool calls and are responsible for sandbox/CSP enforcement. Resource metadata declares CSP domains and optional permissions. The spec also defines tool visibility for model/app, which clients must enforce. [Stable MCP Apps specification](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx), [official overview](https://apps.extensions.modelcontextprotocol.io/api/documents/overview.html), [server/resource quickstart](https://apps.extensions.modelcontextprotocol.io/api/documents/Quickstart.html).

The MCP Apps announcement listed Claude, Goose, VS Code Insiders and ChatGPT support at that time; this is not a promise for every client version, host policy, transport or deployment. Maintain a tested compatibility matrix. [Official client support announcement](https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/)

### 6.2 MCPcodeaway-specific implementation

The current `/mcp` server lacks resources and Apps negotiation. Implement resource listing/read and capability advertisement only when the complete negotiated path is available. Publish static, predeclared resources keyed to immutable manifest revision; tool definitions carry a revision-specific resource URI. Apps-capable hosts can prefetch/review the resource; a no-Apps client sees the same ordinary tool with no UI hint and useful text/structured results.

For normal tool invocation with UI, the host reads the resource when calling the linked tool, passes tool input/result to the view, and proxies view-triggered `tools/call` to MCPcodeaway. All tool definitions still have meaningful input/output schemas and text content for the model. UI-only data may use structured content where supported, but do not depend on it for fallback.

An App can call `mcpcodeaway_start_business_execution` with a published workflow ID, display the returned `business_execution_id` as view state, and pass it to the mapped attributed wrapper. The start tool and attributed wrappers intended for both model and App carry explicit MCP Apps `_meta.ui.visibility: ["model", "app"]`; a wrapper linked to a view also carries `_meta.ui.resourceUri`. The stable Apps specification defines this visibility array and its model/app routing semantics; MCPcodeaway sets it explicitly instead of relying on the spec default. Example start-tool metadata:

```json
"_meta": {
  "ui": {
    "visibility": ["model", "app"]
  }
}
```

For an attributed wrapper associated with an experience, add its immutable resource URI alongside that visibility value. The host exposes these same tools to the app over the same server connection; do not create app-only hidden start or invocation tools. The model sees tools because visibility includes `model`; the host may route App `tools/call` because visibility includes `app`. These metadata values describe host visibility/routing only, not identity, authorization or workflow membership. Every call still authenticates normally, validates caller-bound `business_execution_id`, rechecks authorization and runs through the same dispatcher, approval and sensitive-write journal. Never trust request `_meta` or UI state as proof of identity or authorization. A model can use the same tools without an App. The UI does not keep the execution authoritative: on every call the gateway looks up the ID, checks ownership/state/scope/expiry and reauthorizes the requested tool. Reload or host loss may require the model to re-use the ID it received; an expired/lost ID starts a new execution, not a fabricated continuation.

Writes are supported only through mapped gateway tools. The UI shows a review/confirmation step, but gateway `tool_effects`, requester confirmation, manager approval and sensitive-write journal remain in force. Confirmation requirements in the manifest are review prompts linked to existing policies, not an alternate write gate. If client support/elicitation is absent, existing gateway behavior applies; do not silently bypass the approval requirement. A UI action's failure returns normal MCP error/result and can update execution state; show pending approval, denied, failed and unknown outcomes distinctly.

### 6.3 Constrained UI source and trust boundaries

Have onboarding generate strict declarative JSON, not arbitrary JavaScript/HTML. A product-owned renderer (bundled in MCPcodeaway) translates approved nodes into static HTML/resource output. Initial allowlist: text, input/select, button, result/record, table, status, confirmation; charts only from bounded structured data. Bindings use JSON Pointer into declared fields/results and named catalog tools. No arbitrary expressions, scripts, event handlers, dynamic URLs, external fetch or user-generated style code.

Validate renderer schema/version, field types, required inputs, tool/schema digest, input/output paths, component properties, value bounds, resource size, output escaping, no secret-like field bindings, and effect/approval links. Reject unknown nodes and undeclared tool actions. Use synthetic fixtures for preview; never execute write operations during onboarding. Every publication pins renderer/spec version, manifest revision and resource hash.

MCP Apps clients must sandbox iframe content and enforce CSP. MCPcodeaway should emit restrictive metadata: empty external `connectDomains`/`resourceDomains`, no nested frames, no device permissions unless a reviewed use case requires them. The gateway cannot force a client to implement its sandbox correctly, so compatibility tests are required. Never embed gateway/upstream credentials in resources, results or URLs. Treat API descriptions, examples, tool outputs and model-generated UI data as untrusted; escape text and keep returned data minimum/authorized. Tool-result content must not be interpreted as UI markup.

For the first release, expose the same model-and-app-visible tools to App actions and model calls. Do not add app-only hidden tools or a separate app identity/authorization path. The MCP Apps host uses visibility metadata to route calls; the gateway continues to enforce its authenticated caller and normal policy path and does not infer trust from the visibility marker or user-supplied `_meta`.

## 7. Persistence, APIs and historical reporting

### 7.1 Minimum durable records

Reuse SQLite/Postgres `AuditStore` and migrations; no Redis/event bus/workflow service is justified. Add a compact durable model:

1. **`integration_definition_versions`:** immutable authored payload and canonical JCS hash keyed by `(tenant_id, upstream_name, revision)`, with schema/validator version and author timestamps. Published definitions cannot be updated in place.
2. **`integration_definition_publications`:** append-only publication history with event ID, tenant/upstream, action (`publish` or `rollback`), target revision, previous/current revision, actor and timestamp. A current-revision pointer may be maintained as a projection, but the history is authoritative; rollback adds a new event referencing the old revision.
3. **`workflow_executions`:** random `business_execution_id` primary key, kind (`workflow` or `standalone_tool`), tenant/caller/client owner, selected workflow/tool-value ID, pinned manifest/baseline revision, state, timestamps/expiry, optional active write lease, standalone's atomically claimed initial `call_key`, terminal evidence pointer and unknown reason. Index tenant/owner/state/expiry.
4. **`workflow_request_bindings`, `workflow_calls` and `workflow_execution_events`:** bind `(tenant_id, business_execution_id, outer_request_id)` to one request-body digest before dispatch; the request ID is a response-correlation key, not a claim of business-operation idempotency. One durable `workflow_calls` row per logical dispatcher invocation has random unique `workflow_call_id` and gateway-derived `call_key` including request-body digest; persist effective tool, clean-argument digest, callback occurrence ordinal, Code Mode telemetry run/subcall IDs when available, current call/approval status and sensitive-write journal operation ID when available. Enforce unique request binding, `(tenant_id, call_key)` and `(tenant_id, workflow_call_id)`. Keep append-only `workflow_execution_events` for start, call intent/result, approval transitions, completion, expiry and reconciliation; each event has a unique event ID/idempotency key and links to `workflow_call_id` when applicable. Do not key calls by `(tenant_id, execution_id, request_id, event_kind)` because one Code Mode request can contain multiple dispatches. Completion event has one unique key per execution.
5. **`business_value_events`:** immutable credited outcomes with source kind, execution/completion key, pinned definition/baseline/rate/currency, evidence reference, completion time, cost coverage and optional cross-execution identity. Enforce unique `(tenant_id, execution_id, completion_key)` and at most one completion for a standalone execution. When a stable outcome identity is configured, enforce a partial unique constraint on `(tenant_id, upstream_name, dedupe_namespace, outcome_identity_digest)` where digest is non-null. A null digest means per-execution-only deduplication.

Persist the `workflow_calls` intent row and a call-intent event before dispatch. On successful call completion, append a uniquely keyed result/evidence event and update the call/execution projections as far as the store can guarantee. Link `audit_runs.run_id`, Code Mode subcall telemetry and sensitive-write journal operation ID as separate references; the call ID does not replace the outer MCP request ID or journal correlation ID. Store digests and minimal redacted predicate evidence only, not duplicated raw arguments or full outputs.

If upstream dispatch and the sensitive-write journal succeed but writing workflow result evidence fails, the prior durable `workflow_calls` intent remains. Keep the execution `outcome_unknown`, emit no business-value event, and do not replay the write. Reconcile using the journal operation status together with retained `audit_runs` result/status or a configured verified follow-up. Journal success proves the protected dispatch completed, but does not alone prove the workflow's terminal predicate. If terminal evidence cannot be recovered, retain unknown/incomplete with zero value credit.

`dynamic_upstreams` currently holds only the latest upstream row, and `gateway_settings` is not a revision/event ledger. A small purpose-built table set is justified, using existing store and migration mechanisms. Extend `AuditStore` protocol, SQLite DDL and `src/gateway/migrations.py` versions/contract checks. PostgreSQL migrations remain explicit, ordered and transactional per `docs/postgres-state-architecture.md`; SQLite must mirror logical uniqueness and state transitions.

### 7.2 Gateway API/tool contract

- Add admin-authenticated draft validate/save/publish/rollback/read endpoints (or admin MCP tools) scoped by tenant/upstream and enforcing current control-plane RBAC.
- Add `mcpcodeaway_start_business_execution` when eligible published definitions exist. It accepts exactly one of `{kind:"workflow", workflow_id}` or `{kind:"standalone_tool", tool_value_id}`; the selected ID is required. It returns `business_execution_id`, kind, selected definition/revision and expiry. It does not dispatch upstream. A standalone execution permits one initial logical invocation of its selected tool and same-call retries only; workflow execution supports multiple sequential calls/writes.
- Expose workflow IDs and administrator-authored descriptions in this tool's description/input choices for the authenticated caller. For deferred discovery, return the attributed wrapper for eligible discovered tools when the selected published definition references them. The model selects from those descriptions; gateway validates the ID; there is no second LLM classifier. If no definition matches, it does not start an execution and ordinary tools remain usable without value credit.
- Keep each ordinary upstream tool unchanged. Add gateway-owned attributed wrapper tools with a closed `{business_execution_id, arguments}` envelope and the exact upstream schema nested under `arguments`; Code Mode's gateway-owned `execute_code` carries the explicit ID and binds it to callback dispatches. A call to an ordinary original tool or any call without valid explicit context remains un-attributed, even if that tool has a standalone value definition.
- No general workflow API is required for the first vertical slice; admin analytics reads aggregate durable outcome records. A later status/list tool can be considered if users need recovery visibility, with ownership checks.
- Do not use current operational request headers as the workflow contract. The gateway `request_id` continues to identify one HTTP/MCP request; workflow ID spans requests.

### 7.3 Definition changes during execution

Execution pins manifest revision, workflow definition and all baseline/rate/currency assumptions at start. A new publication affects only new starts. Tool invocation checks current authorization and current effect/approval policy; if an operation was removed, disabled, its schema digest changed incompatibly, or its workflow revision was revoked for safety, reject the call and mark execution incomplete/unknown as appropriate. Do not grandfather authorization because an old workflow began. Keep historical event/value rows under existing audit/tenant retention policy; deleting user data follows explicit retention/deletion rules.

## 8. Analytics and console

Keep two top-level analytics categories. A compact example:

```text
Business value (selected period, tenant-scoped)
  Completed outcomes        138  [deduplicated; verified]
  Estimated manual time      11.5 h  [admin baselines; unvalidated]
  Captured review/rework     1.8 h
  Estimated labor value      €242.50 [€25/hour assumption]
  Known-cost-adjusted subtotal €211.50 [partial; known categories only]
  Net value / ROI            Unavailable [LLM provider cost not connected]
  Cost coverage              72%    [material cost categories missing]
  Outcomes                   94.2% completed · 3.1% failed · 2.7% partial/unknown

Technical efficiency
  Estimated payload/schema token delta   2.4M [~4 bytes/token; not provider count]
  Estimated token price impact           €5.60 [configured input rate; model X]
  Gateway execution                      p50 340 ms · p95 1.4 s
  Tool calls                             1,023 (Classic 414 · Code 503 · Hybrid/other 106)
  Classic vs Code replay                 selected scenario and date; experiment result
```

Business metrics: completed outcomes count deduplicated value events; estimated time/labor use pinned baselines; validated time includes only qualifying measured baseline and review/rework inputs; failure rate uses started logical executions with terminal state; in-progress/expired/unknown shown separately. Group by workflow/integration/tenant and completion date, with baseline source/confidence/version and evidence coverage visible. Separate observed gateway duration from human minutes. Show missing automation-cost categories, not a zero.

Technical metrics preserve existing `/audit/value` and `/audit/analytics` data, execution mix, token estimates, configured model pricing, tool calls and latency. Clarify the estimate method and avoid a “saved money” headline unless clearly labelled model-cost estimate. Side-by-side experiments remain separately identified.

Console: put visual workflow authoring, evidence and baseline configuration in upstream onboarding; keep upstream management for lifecycle/review as needed; make Business Value a report-only dashboard; keep `/dashboard/analytics` as Technical Efficiency and `/dashboard/audit` for evidence. No full console redesign. Continue the established component/palette/typography and responsive rules (`375px`, `768px`, `1280px+`) and complete loading/empty/error/permission states.

## 9. Security, reliability and abuse analysis

| Abuse/failure | Required behavior |
|---|---|
| Cross-tenant ID guessing or a different caller reuses an ID | Query by execution ID plus tenant, subject and client binding; return generic invalid context. ID does not confer access. |
| Model selects a fabricated or unavailable workflow/tool-value ID | Start tool accepts only a published administrator-owned ID visible to the caller; unknown IDs fail closed. No LLM classifier or free-text intent is involved. |
| Expired/completed ID replay | Check state, absolute/idle expiry and unique request/completion key atomically. Reject state changes; no reactivation. |
| Tool outside workflow scope | Reject attributed call. An ordinary call without the workflow context remains subject to normal policy and may be made separately. |
| Role, ABAC, upstream allowlist or approval changes mid-execution | Re-read current policy and upstream config on each dispatch. Pinned workflow version never pins authorization. |
| Duplicate request/retry or multiple terminal calls | Unique `workflow_call_id`/`call_key`, same-request digest handling and execution-scoped completion key; use existing upstream idempotency/recovery for writes; serialize workflow writes. |
| Concurrent parallel tool calls | Support sequential writes with one durable write lease at a time; allow safe reads in parallel. Reject parallel state-changing calls in the MVP. A lease timeout after an uncertain write requires journal/status reconciliation before release. |
| Requester confirmation/manager approval pending, denied or expired | Approval is per-call state; execution remains `in_progress` while approval is pending and later writes are blocked. Never credit while pending. Reauthorize and use existing exact-arguments/policy-version approval continuation. |
| Upstream and sensitive-write journal succeed, then workflow evidence persistence fails | Durable pre-dispatch call intent links the outer request/tool/argument digest. Mark `outcome_unknown`; reconcile journal plus retained audit result or verified follow-up. Journal success alone does not establish the terminal predicate; never replay or credit without recoverable evidence. |
| Timeout / connection reset / ambiguous upstream result | Record unknown when effect may have occurred; no completion claim; do not blindly retry. Use configured existing recovery/status contract if present. |
| Malicious output or prompt injection in docs/results | Treat as data; completion evaluator uses bounded structural predicates only; never evaluate tool output as code or workflow instructions. |
| Malicious generated UI / tool parameters | Strict declarative schema, allowlist, output escaping, fixed mappings, size limits, no arbitrary scripts/URLs, ordinary dispatch validation and redaction. |
| Definition changed during active execution | Immutable version pin for attribution; current policy is rechecked; incompatible/stale tool ref rejects new calls; old baseline retained for history. |
| App host or caller spoofs visibility metadata or UI state | Visibility controls host routing only. Do not trust `_meta` or UI state as identity/authorization; reauthenticate, validate execution ownership and apply the normal dispatcher/approval path. Human-confirmed outcomes are explicitly reported, not machine-verified. |

The workflow event store is attribution/evidence, not a second execution or approval system. The dispatcher and sensitive-write journal remain authoritative for whether a call could run and whether a write is safe to retry.

## 10. Phased implementation roadmap

The dependency order is: manifest → attributed execution/call identity → outcome ledger and cross-mode/concurrency/crash acceptance → optional MCP Apps → analytics and polished console → compatibility expansion. Cross-mode, concurrency and crash-safety acceptance are exit gates for the workflow/value foundation, not deferred work. A vertical-slice milestone is specified below; the companion [implementation plan](interactive-experiences-business-value-implementation-plan.md) contains task-level sequencing and acceptance criteria.

| Phase | Product outcome and scope | Main code/persistence surface | Complexity |
|---|---|---|---|
| 1. Manifest and onboarding builder | Strict manifest model, tool refs/digests, draft review/validation/publish/versioning; onboarding provides a visual catalog-based workflow builder with display ordering, evidence, completion and baseline fields; no workflow DSL or raw JSON authoring in Business Value. | `upstream_clients.py`, `config.py`, `catalog.py`, `server.py`, `state.py`, `migrations.py`; onboarding/upstream pages. New version table and admin endpoints. | Medium |
| 2. Explicit execution context and dispatch identity | Generalized `mcpcodeaway_start_business_execution`; closed wrapper envelope without upstream schema mutation; owner-bound execution ID; per-dispatch call identity; serialized sequential writes and orthogonal approval state. | `catalog.py`, `server.py` (`tools_for_mode`, `list_tools_for_request`, `mcp_endpoint`, Code callbacks/direct call), `dispatcher.py`, `state.py`, migrations. | Large |
| 3. Outcome evidence and value ledger | Deterministic terminal predicates; link call/run/journal evidence; complete/failed/incomplete/expired/unknown states; standalone tool outcomes and exclusive attribution; baselines and one deduped value record. | `dispatcher.py`, `server.py`, `telemetry.py`, `state.py`, migrations; additive APIs/admin definitions. | Large |
| 4. Optional MCP Apps | Negotiate stable Apps extension; resources list/read, resource URI/tool `_meta`, declarative renderer, host-mediated calls, fallback. Apps optional to workflow tracking. | `mcp_protocol.py`, `server.py`, `catalog.py`; renderer/resource module and bundled assets; manifest schema; APIs for preview. | Large |
| 5. Analytics and console | Separate Business Value and Technical Efficiency, provenance/confidence/cost coverage; visual workflow authoring and review in onboarding, with Business Value as a report. | `telemetry.py`, `server.py`, `web/app/pages/onboarding.vue`, `upstreams.vue`, `analytics.vue`, `index.vue`; `DESIGN.md` patterns. | Medium |
| 6. Compatibility and measurement expansion | Broader client matrix, human validation workflow, sampled baseline evidence and optional external cost imports after core reliability gates. | Existing tests, local/Postgres acceptance scripts, supported client docs; only add integrations if customers provide source contracts. | Medium–large |

Every phase keeps existing mode behavior and security gates unless a narrowly reviewed change is necessary to carry attribution. Do not add another authorization path, identity portal, workflow engine, infrastructure service or credential store.

## 11. Smallest end-to-end vertical slice

Implement this as the first product proof after the manifest skeleton; keep it on the real `/mcp` and dispatcher path:

1. During onboarding, the admin creates `handle_customer_support_request` from the discovered catalog. The builder displays `find_customer`, `get_account`, `get_order_history`, and `create_ticket` in an editable descriptive order, sets required evidence for all three successful lookups, chooses successful ticket creation with `/ticket/id` as terminal evidence and a ticket-ID dedupe identity, and records a five-minute unvalidated baseline that explicitly covers the three lookups plus ticket creation. This is a multi-step support workflow, not a ticket-only baseline. Existing tool effects/approval policies are configured separately in upstream settings.
2. `tools/list` exposes `mcpcodeaway_start_business_execution` with workflow IDs/descriptions visible to the caller and gateway-owned attributed wrappers for eligible tools. The model selects a published `workflow_id`, calls start, and receives a random `business_execution_id` tied to tenant/caller and manifest revision.
3. The model performs the three required reads and one protected ticket write. Classic and App wrapper calls carry the ID in their closed envelope. Code Mode carries it in the gateway-owned `execute_code` envelope and callbacks inherit the binding. Hybrid and Auto route the same context through their selected direct/Code paths. Dispatcher auth/policy/approval behavior is unchanged.
4. Gateway durably records each `workflow_call_id` before dispatch and links outer request ID, Code Mode telemetry run/subcall when available, dispatcher call and sensitive-write journal operation ID. Completion requires successful evidence for all three lookup calls and the final ticket result satisfying the admin predicate after journal status is `succeeded`; atomically transition once to completed and insert one `business_value_event` under execution and optional stable-outcome uniqueness.
5. Analytics shows one completed workflow, one five-minute estimated manual baseline, source/confidence, human review/rework if captured, and independent gateway duration/tool count. Duplicate request, result replay or a second terminal call cannot add a second value event. Unknown journal/evidence state shows no value credit.
6. Repeat the same acceptance scenario in all execution routes where available; run a tenant/caller crossing and a policy-change denial case. MCP Apps can be layered on later and must invoke this same start/call contract.

This proves the essential product contract, including the onboarding workflow builder and cross-call/multi-tool attribution. It is not a standalone demo and does not require changing token-savings behavior.

## 12. Risks, assumptions, open decisions and non-goals

### Risks / assumptions to validate

- Gateway-owned wrapper tools and the explicit Code Mode envelope are portable through target MCP clients and generated tool adapters; test real supported clients and validators. Unsupported original JSON Schema dialects or unresolved references fail closed for attribution while leaving the original ordinary tool available.
- A client may omit execution context or fail the required request-ID retransmission contract. Missing context receives no business attribution; invalid/expired IDs fail closed. A client that cannot preserve outer request ID/body on unresolved retransmission is unsupported for attribution on that path; ordinary authorized tool access remains available.
- Reliable business outcome is integration-specific. Predicate paths are not sufficient for APIs that return “accepted” before persistence; use follow-up status verification or leave uncredited.
- A new workflow execution/write-claim state adds cross-replica migration and failure paths. Must prove atomicity and crash behavior with PostgreSQL acceptance, not only SQLite unit tests.
- Sensitive-write journal success plus workflow evidence persistence is not one external transaction. Unknown remains a first-class result; reconciliation policy must be designed for customer APIs.
- Apps support/host sandbox enforcement differs. MCPcodeaway cannot guarantee host safety and must publish supported-client limitations.
- Loaded labor rates, currency and validated baseline evidence require customer ownership and provenance. No claim of realized cash savings.

### Open product/technical decisions

1. Which target MCP clients/transports are in the initial support matrix for attributed wrapper tools, Code Mode envelopes and the request-ID/body retransmission contract?
2. Which terminal predicate types ship first: output path/value only, or verified follow-up read? Recommendation: both deterministic forms, no scripts.
3. What user confirmation sources are trusted enough to label `human_confirmed`, and should those count as completions or be reported separately? Recommendation: report separately and do not call them verified.
4. What is the retention period for execution IDs, evidence and business-value rows? Align with audit and tenant deletion controls.
5. Which MCP Apps client versions and transports are launch-supported? What is the accepted exception process for CSP domains?
6. When an upstream operation is removed or its output schema changes, should active executions immediately stop or permit only calls still matching current schema/policy? Recommendation: revalidate current schema and policy for every new call; no authorization grandfathering.

### Non-goals

- General agent orchestration, arbitrary workflow DSL, arbitrary generated JavaScript, automatic workflow classification in v1, automatic authorization/approval, or automatic write execution during onboarding.
- Making Apps/UI mandatory for tools or business value.
- Replacing identity providers, upstream authorization, `HostDispatcher`, sensitive-write journal, existing approval path, or current technical analytics.
- Claiming LLM/provider charges or cash savings without an actual cost source.
- Reintroducing a user connections or credential-management screen, Redis, a new database, event bus, workflow microservice or credential vault.

## 13. Existing features to preserve unchanged

- Existing Classic, Code, Hybrid and Auto dispatch semantics and the per-call authentication, tenant, RBAC/ABAC, effect and approval checks.
- `HostDispatcher.invoke_tool` as the single execution path for normal, Code Mode and future App actions.
- Sensitive-write journal's request correlation, digest, atomic claim, uncertain outcome and no-blind-replay behavior. Workflow ID is a separate attribution reference.
- Existing OpenAPI/MCP discovery and `DiscoveredTool` schemas as canonical tool definitions; manifests reference them.
- Existing token/payload/schema-savings reporting, model-price assumptions, latency, tool-call counts and replay/A-B comparators as Technical Efficiency metrics.
- Existing upstream auth/delegation rules and admin RBAC; a workflow or UI never grants access.

## References

- MCP Apps stable specification (SEP-1865, 2026-01-26): <https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx>
- MCP Apps official overview: <https://apps.extensions.modelcontextprotocol.io/api/documents/overview.html>
- MCP Apps server quickstart/resources and tool association: <https://apps.extensions.modelcontextprotocol.io/api/documents/Quickstart.html>
- MCP Apps client support announcement (dated): <https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/>
- SEP-1865 discussion/status: <https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp.md>
- MCP tool `inputSchema` contract (2026-07-28; default JSON Schema 2020-12 and explicit `$schema` dialect selection): <https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/tools.mdx>
- JSON Schema 2020-12 core specification (references, resources, applicators): <https://json-schema.org/draft/2020-12/json-schema-core.html>

## Files and documents inspected

`src/gateway/upstream_clients.py`, `catalog.py`, `config.py`, `server.py`, `dispatcher.py`, `mcp_protocol.py`, `telemetry.py`, `state.py`, `migrations.py`, `adaptive_execution.py`, `comparator.py`, `ab_testing.py`; `web/app/pages/onboarding.vue`, `upstreams.vue`, `approvals.vue`, `audit.vue`, `analytics.vue`, `index.vue`, `settings.vue`; `docs/gateway-flow-map.md`, `docs/postgres-state-architecture.md`, `docs/approval-gate-enhancements.md`, `docs/state-machines.md`; `tests/test_upstream_onboarding.py`, `test_hitl_onboarding_and_abac.py`, `test_openapi_generation_golden.py`, `test_gateway_modes_e2e.py`, `test_adaptive_execution.py`, `test_provenance_and_axes.py`, `test_approval_golden.py`, `test_sensitive_write_audit.py`, `test_process_crash_acceptance.py`, `test_profiles_and_observability.py`, and `test_postgres_tls_acceptance.py`. These files were inspected only; no code or test files were changed.
