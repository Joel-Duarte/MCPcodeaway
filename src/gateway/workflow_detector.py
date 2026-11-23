"""Passive workflow correlation, readback verification, schema discovery, and System-1 judging."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import re
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, Field

from gateway.business_value_runtime import (
    apply_regional_baseline,
    canonical_json_digest,
    required_evidence_satisfied,
    resolve_json_pointer,
    result_matches_predicate,
    terminal_completion_satisfied,
)
from gateway.catalog import ToolCatalog
from gateway.config import GatewaySettings
from gateway.dispatcher import OutputRedactor, extract_entity_identifiers, summarize_value_shape
from gateway.integration_definition import (
    IntegrationDefinition,
    WorkflowDefinition,
    compute_workflow_fingerprint,
)
from gateway.state import AuditStore
from gateway.upstream_clients import DiscoveredTool, compute_schema_digest, validate_upstream_url

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ID_KEY_RE = re.compile(r"(?:^id$|_id$|Id$|_key$|_ref$)")
_MUTATION_INTENT_RE = re.compile(
    r"\b(update|change|set|modify|edit|patch|create|add|open|submit|delete|remove|approve|close)\b"
)
_VERIFY_INTENT_RE = re.compile(r"\b(verify|confirm|check|read\s*back|validate|ensure)\b")
_MULTI_COUNT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:all|multiple|both|two|three|four|five|[2-9]|[1-9]\d+)(?![A-Za-z0-9_-])"
)
_VERSION_KEYS = {"version", "rev", "revision", "etag", "updated_at", "modified_at"}


class System1JudgeVerdict(BaseModel):
    """Unified boolean + calibrated confidence/percentage verdict from any System-1 backend."""

    completed: bool
    confidence: float = Field(ge=0.0, le=1.0)
    completion_pct: int = Field(ge=0, le=100)
    verdict: Literal[
        "completed_verified",
        "completed_unverified",
        "partial_or_incomplete",
        "abandoned",
        "read_only",
    ]
    matched_workflow_id: str | None = None
    suggested_workflow_name: str | None = None
    missing_goals: list[str] = Field(default_factory=list)
    backend: str = "synthetic_heuristic"
    latency_ms: float = 0.0


def _tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    return set(_TOKEN_RE.findall(text.replace("_", " ").replace("-", " ").lower()))


def _slugify(value: str, fallback: str = "discovered_workflow") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"wf_{slug}" if slug else fallback
    return slug[:48].rstrip("_")


def extract_hashed_entity_fingerprints(
    value: Any, *, tenant_id: str = "", salt: str = ""
) -> list[str]:
    """Extract business entity IDs and *_id values and return salted SHA-256 fingerprints."""

    raw_ids: set[str] = set(extract_entity_identifiers(value))

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if (
                    _ID_KEY_RE.search(str(key))
                    and isinstance(item, str | int)
                    and not isinstance(item, bool)
                ):
                    text = str(item).strip()
                    if text and len(text) <= 128:
                        raw_ids.add(f"{str(key).lower()}:{text}")
                _walk(item)
        elif isinstance(node, list | tuple):
            for item in node[:25]:
                _walk(item)

    _walk(value)
    fingerprints: list[str] = []
    for raw in sorted(raw_ids):
        digest = hashlib.sha256(f"{tenant_id}:{salt}:{raw}".encode()).hexdigest()[:20]
        fingerprints.append(f"ent_{digest}")
    return fingerprints


def extract_field_digests(value: Any, *, prefix: str = "") -> dict[str, str]:
    """Build a PII-free map of scalar field paths -> short SHA-256 digests."""

    digests: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in list(value.items())[:40]:
            path = f"{prefix}/{key}" if prefix else f"/{key}"
            if isinstance(item, str | int | float | bool) or item is None:
                digests[path] = canonical_json_digest(item)[:16]
            elif isinstance(item, dict):
                digests.update(extract_field_digests(item, prefix=path))
            elif isinstance(item, list):
                digests[path] = canonical_json_digest({"len": len(item)})[:16]
    return digests


def compute_readback_diff(
    prior_state: dict[str, dict[str, str]],
    *,
    entity_fingerprints: list[str],
    effect: str,
    status: str,
    arguments: dict[str, Any],
    result: Any,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Compare read/write field digests to detect state mutations and readback verification."""

    next_state = {k: dict(v) for k, v in prior_state.items()}
    arg_digests = extract_field_digests(arguments)
    res_digests = extract_field_digests(result)
    keys_for_entities = entity_fingerprints or ["_episode_default"]
    diff: dict[str, Any] = {
        "result_shape": summarize_value_shape(result),
        "changed_keys": [],
        "version_bumped": False,
        "fields_matched_write": False,
        "readback_verified": False,
    }
    if status != "succeeded":
        return diff, next_state

    if effect == "write":
        for ent in keys_for_entities:
            bucket = next_state.setdefault(ent, {})
            for path, digest in arg_digests.items():
                if not _ID_KEY_RE.search(path.rsplit("/", 1)[-1]):
                    bucket[f"__written:{path}"] = digest
            for path, digest in res_digests.items():
                prev = bucket.get(path)
                if prev is not None and prev != digest:
                    diff["changed_keys"].append(path)
                bucket[path] = digest
                if path.rsplit("/", 1)[-1].lower() in _VERSION_KEYS:
                    bucket[f"__pre_write_ver:{path}"] = prev or ""
                    if prev is not None and prev != digest:
                        diff["version_bumped"] = True
        return diff, next_state

    for ent in keys_for_entities:
        bucket = next_state.setdefault(ent, {})
        written = {
            k.removeprefix("__written:"): v for k, v in bucket.items() if k.startswith("__written:")
        }
        has_prior_write = bool(written) or any(k.startswith("__pre_write_ver:") for k in bucket)
        matched_writes = [
            path for path, w_digest in written.items() if res_digests.get(path) == w_digest
        ]
        changed_keys: list[str] = []
        version_bumped = False
        for path, digest in res_digests.items():
            prev = bucket.get(path)
            if prev is not None and prev != digest:
                changed_keys.append(path)
                if path.rsplit("/", 1)[-1].lower() in _VERSION_KEYS:
                    version_bumped = True
            bucket[path] = digest
        if has_prior_write and (matched_writes or version_bumped):
            diff["readback_verified"] = True
            diff["fields_matched_write"] = bool(matched_writes)
            diff["version_bumped"] = version_bumped
            diff["changed_keys"] = sorted(set(changed_keys + matched_writes))
    return diff, next_state


class SyntheticWorkflowClassifier:
    """Zero-ML calibrated System-1 baseline using structural motifs and intent entailment."""

    name = "synthetic_heuristic"

    def judge_episode(
        self,
        *,
        redacted_intent: str | None,
        steps: list[dict[str, Any]],
        matched_workflow_id: str | None = None,
        required_evidence_tools: list[str] | None = None,
    ) -> System1JudgeVerdict:
        intent = (redacted_intent or "").lower()
        writes = [s for s in steps if s.get("effect") == "write"]
        reads = [s for s in steps if s.get("effect") != "write"]
        ok_writes = [
            s
            for s in writes
            if s.get("status") == "succeeded"
            and s.get("sensitive_write_journal_status") in {None, "succeeded"}
        ]
        succeeded_tools = {
            str(s.get("tool_qualified_name") or "")
            for s in steps
            if s.get("status") in {"succeeded", "ok"}
        }
        failed_calls = [s for s in steps if s.get("status") not in {"succeeded", "ok"}]
        readback_verified = any(
            bool((s.get("shape_diff") or {}).get("readback_verified")) for s in steps
        )
        completion_matched = any(bool(s.get("completion_matched")) for s in steps)

        wants_mutation = bool(_MUTATION_INTENT_RE.search(intent))
        wants_verify = bool(_VERIFY_INTENT_RE.search(intent))
        wants_multi = bool(_MULTI_COUNT_RE.search(intent))

        missing_goals: list[str] = []
        logit = 0.0

        if not steps:
            return System1JudgeVerdict(
                completed=False,
                confidence=0.05,
                completion_pct=0,
                verdict="abandoned",
                matched_workflow_id=matched_workflow_id,
                missing_goals=["no_tool_calls_recorded"],
                backend=self.name,
            )

        if wants_mutation and not ok_writes:
            missing_goals.append("expected_mutation_never_succeeded")
            logit -= 2.4
        if required_evidence_tools:
            missing_req = [
                tool for tool in required_evidence_tools if tool and tool not in succeeded_tools
            ]
            if missing_req:
                missing_goals.extend(f"missing_required_evidence:{tool}" for tool in missing_req)
                logit -= 2.2
        if ok_writes:
            logit += 1.2
        if readback_verified or completion_matched:
            logit += 1.9
        elif wants_verify and ok_writes and not readback_verified:
            missing_goals.append("post_write_verification_missing")
            logit -= 1.3
        if wants_multi and len(ok_writes) < 2 and wants_mutation:
            missing_goals.append("multi_target_mutation_incomplete")
            logit -= 1.5
        if failed_calls:
            last_status = steps[-1].get("status")
            if last_status not in {"succeeded", "ok"}:
                missing_goals.append("terminal_step_failed")
                logit -= 2.0
            else:
                logit -= 0.4

        intent_tokens = _tokenize(intent)
        tool_tokens: set[str] = set()
        for step in steps:
            tool_tokens |= _tokenize(str(step.get("tool_qualified_name") or ""))
        overlap = len(intent_tokens & tool_tokens)
        if intent_tokens and overlap:
            logit += min(0.8, 0.25 * overlap)

        if not wants_mutation and not writes:
            all_reads_ok = bool(reads) and not failed_calls
            comp_prob = 0.12 if all_reads_ok else 0.05
            verdict: Literal[
                "completed_verified",
                "completed_unverified",
                "partial_or_incomplete",
                "abandoned",
                "read_only",
            ] = "read_only" if all_reads_ok else "abandoned"
            return System1JudgeVerdict(
                completed=False,
                confidence=comp_prob,
                completion_pct=int(round(comp_prob * 100)),
                verdict=verdict,
                matched_workflow_id=matched_workflow_id,
                missing_goals=missing_goals,
                backend=self.name,
            )

        prob = 1.0 / (1.0 + math.exp(-logit))
        confidence = round(max(0.01, min(0.99, prob)), 4)
        completion_pct = int(round(confidence * 100))

        if ok_writes and (readback_verified or completion_matched) and not missing_goals:
            final_verdict = "completed_verified"
            completed = True
        elif ok_writes and not missing_goals and confidence >= 0.65:
            final_verdict = "completed_unverified"
            completed = True
        elif ok_writes or (reads and wants_mutation):
            final_verdict = "partial_or_incomplete"
            completed = False
        else:
            final_verdict = "abandoned"
            completed = False

        return System1JudgeVerdict(
            completed=completed,
            confidence=confidence,
            completion_pct=completion_pct,
            verdict=final_verdict,
            matched_workflow_id=matched_workflow_id,
            missing_goals=missing_goals,
            backend=self.name,
        )


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("judge response must be a JSON object")
    return parsed


def normalize_judge_payload(
    raw: dict[str, Any], *, backend: str, latency_ms: float = 0.0
) -> System1JudgeVerdict:
    """Normalize boolean + 0..1 or 0..100 confidence outputs into System1JudgeVerdict."""

    raw_conf = raw.get("confidence", raw.get("probability", raw.get("score")))
    raw_pct = raw.get("completion_pct", raw.get("percentage"))
    if raw_conf is None and raw_pct is not None:
        conf_val = float(raw_pct) / 100.0
    elif raw_conf is not None:
        conf_val = float(raw_conf)
        if conf_val > 1.0:
            conf_val /= 100.0
    else:
        conf_val = 0.85 if bool(raw.get("completed")) else 0.25
    conf_val = max(0.0, min(1.0, round(conf_val, 4)))
    pct_val = int(round(float(raw_pct))) if raw_pct is not None else int(round(conf_val * 100))
    pct_val = max(0, min(100, pct_val))
    completed = bool(raw.get("completed", pct_val >= 50))
    verdict = str(raw.get("verdict") or "")
    if verdict not in {
        "completed_verified",
        "completed_unverified",
        "partial_or_incomplete",
        "abandoned",
        "read_only",
    }:
        verdict = "completed_unverified" if completed else "partial_or_incomplete"
    missing = raw.get("missing_goals") or []
    if not isinstance(missing, list):
        missing = [str(missing)]
    return System1JudgeVerdict(
        completed=completed,
        confidence=conf_val,
        completion_pct=pct_val,
        verdict=cast(
            Literal[
                "completed_verified",
                "completed_unverified",
                "partial_or_incomplete",
                "abandoned",
                "read_only",
            ],
            verdict,
        ),
        matched_workflow_id=(
            str(raw["matched_workflow_id"]) if raw.get("matched_workflow_id") else None
        ),
        suggested_workflow_name=(
            str(raw["suggested_workflow_name"]) if raw.get("suggested_workflow_name") else None
        ),
        missing_goals=[str(item) for item in missing[:10]],
        backend=backend,
        latency_ms=round(latency_ms, 3),
    )


async def call_openai_compatible_judge(
    *,
    base_url: str,
    model: str,
    redacted_intent: str | None,
    steps: list[dict[str, Any]],
    candidate_workflows: list[dict[str, str]] | None = None,
    api_key: str | None = None,
    timeout_seconds: float = 5.0,
    app_env: str = "dev",
    block_private_ips_in_prod: bool = True,
    allowed_hosts: list[str] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> System1JudgeVerdict:
    """Query an OpenAI-compatible System-1 model (e.g., LM Studio) with strictly redacted traces."""

    validate_upstream_url(
        base_url,
        app_env=app_env,
        block_private_ips_in_prod=block_private_ips_in_prod,
        allowed_hosts=allowed_hosts,
    )
    endpoint = base_url.rstrip("/") + "/chat/completions"
    compact_steps = [
        {
            "tool": s.get("tool_qualified_name"),
            "effect": s.get("effect"),
            "status": s.get("status"),
            "journal": s.get("sensitive_write_journal_status"),
            "readback_verified": bool((s.get("shape_diff") or {}).get("readback_verified")),
            "changed_keys": (s.get("shape_diff") or {}).get("changed_keys", []),
        }
        for s in steps
    ]
    prompt_payload = {
        "intent": OutputRedactor.redact_intent(redacted_intent) or "",
        "steps": compact_steps,
        "candidate_workflows": candidate_workflows or [],
    }
    system_msg = (
        "You are a System-1 workflow completion classifier. Given ONLY redacted intent and "
        "structural tool execution steps, return ONLY a compact JSON object with keys: "
        "completed (boolean), confidence (float 0.0-1.0), completion_pct (integer 0-100), "
        "verdict ('completed_verified'|'completed_unverified'|'partial_or_incomplete'|"
        "'abandoned'|'read_only'), matched_workflow_id (string|null), "
        "suggested_workflow_name (string|null), missing_goals (array of strings)."
    )
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": 640,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": json.dumps(prompt_payload, separators=(",", ":"))},
        ],
    }
    started = datetime.now(UTC)
    if http_client is not None:
        resp = await http_client.post(endpoint, headers=headers, json=body, timeout=timeout_seconds)
    else:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(endpoint, headers=headers, json=body)
    resp.raise_for_status()
    latency_ms = (datetime.now(UTC) - started).total_seconds() * 1000.0
    data = resp.json()
    message = data["choices"][0]["message"]
    raw_content = str(message.get("content") or "").strip()
    if not raw_content and message.get("reasoning_content"):
        raw_content = str(message["reasoning_content"])
    parsed = _extract_json_object(raw_content)
    return normalize_judge_payload(parsed, backend=f"openai:{model}", latency_ms=latency_ms)


def pick_schema_predicate(tool: DiscoveredTool | None) -> dict[str, Any]:
    """Select a schema-valid JSON Pointer completion predicate for an upstream tool."""

    if tool is None:
        return {"path": "/status", "type": "non_empty_string"}
    schema = tool.output_schema if isinstance(tool.output_schema, dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for key in ("version", "revision"):
        if key in props and isinstance(props[key], dict):
            kind = props[key].get("type")
            if kind in {"integer", "number", "string"}:
                return {"path": f"/{key}", "type": kind}
    for key, prop in props.items():
        if not isinstance(prop, dict):
            continue
        ptype = prop.get("type")
        if ptype == "string":
            return {"path": f"/{key}", "type": "non_empty_string"}
        if ptype == "boolean":
            return {"path": f"/{key}", "type": "boolean", "equals": True}
        if ptype == "integer":
            return {"path": f"/{key}", "type": "integer"}
        if ptype == "number":
            return {"path": f"/{key}", "type": "number"}
        if ptype == "array":
            return {"path": f"/{key}", "type": "array"}
        if ptype == "object" and isinstance(prop.get("properties"), dict):
            for sub_key, sub_prop in prop["properties"].items():
                if isinstance(sub_prop, dict) and sub_prop.get("type") == "string":
                    return {"path": f"/{key}/{sub_key}", "type": "non_empty_string"}
            return {"path": f"/{key}", "type": "object"}
    first_key = next(iter(props), "status")
    return {"path": f"/{first_key}", "type": "non_empty_string"}


def synthesize_workflows_from_catalog(
    upstream_name: str,
    tools: list[DiscoveredTool],
) -> list[dict[str, Any]]:
    """Day-0 schema-graph synthesizer producing candidate WorkflowDefinitions."""

    if not tools:
        return []
    writes = [t for t in tools if t.mutating]
    reads = [
        t
        for t in tools
        if not t.mutating and not t.name.lower().endswith(("_health", "_metrics", "_summary"))
    ]
    candidates: list[dict[str, Any]] = []

    def _param_keys(tool: DiscoveredTool) -> set[str]:
        props = (tool.input_schema or {}).get("properties") or {}
        return {str(k).lower() for k in props if _ID_KEY_RE.search(str(k))}

    def _output_id_keys(tool: DiscoveredTool) -> set[str]:
        keys: set[str] = set()

        def _walk_schema(node: Any) -> None:
            if not isinstance(node, dict):
                return
            props = node.get("properties")
            if isinstance(props, dict):
                for key, sub in props.items():
                    key_s = str(key).lower()
                    if _ID_KEY_RE.search(key_s):
                        keys.add(key_s)
                    _walk_schema(sub)
            items = node.get("items")
            if isinstance(items, dict):
                _walk_schema(items)

        _walk_schema(tool.output_schema)
        return keys

    def _write_priority(tool: DiscoveredTool) -> tuple[int, str]:
        lname = tool.name.lower()
        for rank, prefix in enumerate(
            ("create_", "resolve_", "escalate_", "update_", "assign_", "approve_", "submit_")
        ):
            if lname.startswith(prefix):
                return (rank, lname)
        if any(hint in lname for hint in ("tag", "watcher", "reopen")):
            return (20, lname)
        return (10, lname)

    for write_tool in sorted(writes, key=_write_priority):
        if len(candidates) >= 5:
            break
        write_keys = _param_keys(write_tool)
        matching_reads = [
            r
            for r in reads
            if (write_keys and ((_param_keys(r) & write_keys) or (_output_id_keys(r) & write_keys)))
            or (
                _tokenize(r.name) & _tokenize(write_tool.name) - {"get", "list", "update", "create"}
            )
        ]
        if not matching_reads:
            matching_reads = reads[:2]
        verify_read = next(
            (
                r
                for r in matching_reads
                if any(tok in r.name.lower() for tok in ("get_", "read_", "fetch_", "verify_"))
            ),
            None,
        )
        focused_reads: list[DiscoveredTool] = []
        if verify_read is not None:
            focused_reads.append(verify_read)
        for r in matching_reads:
            if r not in focused_reads and len(focused_reads) < 2:
                focused_reads.append(r)
        participating = list(
            dict.fromkeys([*(r.qualified_name for r in focused_reads), write_tool.qualified_name])
        )
        completion_tool = verify_read or write_tool
        required_evidence: list[dict[str, Any]] = []
        for tool in [*focused_reads, write_tool]:
            entry: dict[str, Any] = {"tool": tool.qualified_name, "min_successful_calls": 1}
            if isinstance(tool.output_schema, dict):
                pred = pick_schema_predicate(tool)
                if pred["path"]:
                    entry["result_when"] = pred
            required_evidence.append(entry)

        completion_pred = pick_schema_predicate(completion_tool)
        completion_kind = "verified_followup" if verify_read is not None else "terminal_tool_result"

        workflow_slug = _slugify(f"{write_tool.name}_workflow")
        display_action = write_tool.name.replace("_", " ").strip().capitalize()
        draft = {
            "id": workflow_slug,
            "name": f"{display_action} workflow",
            "description": (
                f"Auto-synthesized from {upstream_name} tool schemas: execute "
                f"{', '.join(participating)} and verify completion via "
                f"{completion_tool.qualified_name}."
            ),
            "origin": "auto_schema",
            "fingerprint": compute_workflow_fingerprint(
                upstream_name=upstream_name,
                participating_tools=participating,
                completion_tool=completion_tool.qualified_name,
                completion_kind=completion_kind,
            ),
            "intent_examples": [display_action.lower()],
            "participating_tools": participating,
            "required_evidence": required_evidence,
            "completion": {
                "kind": completion_kind,
                "tool": completion_tool.qualified_name,
                "success_when": completion_pred,
                **(
                    {"require_sensitive_write_journal_status": "succeeded"}
                    if completion_tool.mutating
                    else {}
                ),
            },
            "tool_refs": [
                {
                    "name": t.qualified_name,
                    "schema_sha256": compute_schema_digest([t]),
                }
                for t in tools
                if t.qualified_name in participating
            ],
            "needs_baseline_confirmation": True,
        }
        candidates.append(draft)
    return candidates


def mine_workflows_from_runs(
    upstream_name: str,
    runs: list[dict[str, Any]],
    *,
    min_support: int = 2,
) -> list[dict[str, Any]]:
    """Day-N process miner extracting recurring multi-step tool sequences from audit_runs."""

    sequence_counts: Counter[tuple[str, ...]] = Counter()
    intents_by_seq: dict[tuple[str, ...], list[str]] = defaultdict(list)

    for run in runs:
        if not run.get("ok"):
            continue
        subcalls = run.get("subcalls") or []
        seq: list[str] = []
        for sc in subcalls:
            qname = sc.get("tool") or sc.get("tool_qualified_name") or ""
            if isinstance(qname, str) and qname.startswith(f"{upstream_name}__"):
                seq.append(qname)
        if not seq and isinstance(run.get("tool_name"), str):
            tname = str(run["tool_name"])
            if tname.startswith(f"{upstream_name}__"):
                seq.append(tname)
        if len(seq) < 2:
            continue
        compact: list[str] = []
        for item in seq:
            if not compact or compact[-1] != item:
                compact.append(item)
        key = tuple(compact[:8])
        sequence_counts[key] += 1
        intent = run.get("intent") or run.get("redacted_intent")
        if isinstance(intent, str) and intent.strip():
            cleaned = OutputRedactor.redact_intent(intent)
            if cleaned:
                intents_by_seq[key].append(cleaned[:200])

    mined: list[dict[str, Any]] = []
    for seq, count in sequence_counts.most_common(20):
        if count < min_support:
            continue
        unique_tools = list(dict.fromkeys(seq))
        terminal = seq[-1]
        slug = _slugify(f"mined_{terminal.split('__', 1)[-1]}_{len(unique_tools)}step")
        sample_intents = list(dict.fromkeys(intents_by_seq.get(seq, [])))[:5]
        mined.append(
            {
                "id": slug,
                "name": f"Mined: {terminal.split('__', 1)[-1].replace('_', ' ')}",
                "description": f"Observed {count} times in audit_runs: {' -> '.join(seq)}.",
                "origin": "auto_mined",
                "fingerprint": compute_workflow_fingerprint(
                    upstream_name=upstream_name,
                    participating_tools=unique_tools,
                    completion_tool=terminal,
                    completion_kind="terminal_tool_result",
                ),
                "observed_count": count,
                "intent_examples": sample_intents,
                "participating_tools": unique_tools,
                "required_evidence": [
                    {"tool": t, "min_successful_calls": 1} for t in unique_tools[:-1]
                ],
                "completion": {
                    "kind": "terminal_tool_result",
                    "tool": terminal,
                    "success_when": {"path": "/status", "type": "non_empty_string"},
                },
                "needs_baseline_confirmation": True,
            }
        )
    return mined


class WorkflowEpisodeTracker:
    """Stateful multi-replica workflow detector, readback verifier, and async System-1 judge."""

    def __init__(
        self,
        settings: GatewaySettings,
        store: AuditStore,
        catalog: ToolCatalog,
        *,
        http_client: httpx.AsyncClient | None = None,
        registry: Any | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.catalog = catalog
        self.http_client = http_client
        self.registry = registry
        self.heuristic = SyntheticWorkflowClassifier()
        self._judge_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=2000)
        self._worker_task: asyncio.Task[None] | None = None
        self._sweeper_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self.settings.workflow_detection_mode == "off":
            return
        self._stop_event.clear()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._judge_loop())
        if self._sweeper_task is None or self._sweeper_task.done():
            self._sweeper_task = asyncio.create_task(self._sweeper_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        for task in (self._worker_task, self._sweeper_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._worker_task = None
        self._sweeper_task = None

    async def _judge_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                tenant_id, episode_id = await asyncio.wait_for(self._judge_queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            try:
                await self.evaluate_closed_episode(tenant_id, episode_id)
            except Exception:
                continue
            finally:
                self._judge_queue.task_done()

    async def _sweeper_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=15.0)
                break
            except TimeoutError:
                pass
            try:
                await self.sweep_expired_episodes()
            except Exception:
                continue

    async def sweep_expired_episodes(self) -> int:
        if not hasattr(self.store, "list_expired_open_workflow_episodes"):
            return 0
        now_iso = datetime.now(UTC).isoformat()
        expired = await self.store.list_expired_open_workflow_episodes(now_iso, limit=100)
        closed = 0
        for episode in expired:
            await self.close_and_finalize_episode(
                episode, close_reason="idle_ttl_expired", run_judge=True
            )
            closed += 1
        return closed

    async def _published_workflows(
        self, tenant_id: str
    ) -> list[tuple[dict[str, Any], WorkflowDefinition]]:
        if not hasattr(self.store, "list_current_published_integration_definitions"):
            return []
        rows = await self.store.list_current_published_integration_definitions(tenant_id)
        out: list[tuple[dict[str, Any], WorkflowDefinition]] = []
        for row in rows:
            try:
                manifest = IntegrationDefinition.model_validate(row["definition"])
            except Exception:
                continue
            for wf in manifest.workflows:
                if wf.enabled:
                    out.append((row, wf))
        return out

    def _match_published_workflow(
        self,
        workflows: list[tuple[dict[str, Any], WorkflowDefinition]],
        *,
        redacted_intent: str | None,
        observed_tools: set[str],
    ) -> tuple[dict[str, Any], WorkflowDefinition] | None:
        intent_tokens = _tokenize(redacted_intent)
        best_score = 0.0
        best_match: tuple[dict[str, Any], WorkflowDefinition] | None = None
        for row, wf in workflows:
            participants = set(wf.participating_tools)
            if not (observed_tools & participants):
                continue
            tool_overlap = len(observed_tools & participants) / max(len(participants), 1)
            wf_tokens = _tokenize(f"{wf.name} {wf.description} {' '.join(wf.intent_examples)}")
            intent_overlap = (
                len(intent_tokens & wf_tokens) / max(len(intent_tokens), 1)
                if intent_tokens and wf_tokens
                else 0.0
            )
            score = 0.7 * tool_overlap + 0.3 * intent_overlap
            if score > best_score:
                best_score = score
                best_match = (row, wf)
        return best_match if best_score >= 0.25 else None

    async def observe_execution(
        self,
        *,
        tenant_id: str,
        user_sub: str,
        client_id: str,
        session_key: str | None,
        intent: str | None,
        run_id: str,
        calls: list[dict[str, Any]],
        is_single_turn_batch: bool = False,
        caller_regions: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        """Record one or more tool calls into a passive episode and evaluate completion."""

        if self.settings.workflow_detection_mode == "off" or not calls:
            return None
        if not hasattr(self.store, "list_open_workflow_episodes"):
            return None

        redacted_intent = OutputRedactor.redact_intent(intent)
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        ttl_seconds = self.settings.workflow_episode_idle_ttl_seconds
        expires_iso = (now + timedelta(seconds=ttl_seconds)).isoformat()
        eff_session = (session_key or "stateless").strip()[:128] or "stateless"
        salt = self._entity_salt()

        all_call_entities: list[str] = []
        all_arg_entities: list[str] = []
        for c in calls:
            arg_ents = extract_hashed_entity_fingerprints(
                c.get("arguments") or {},
                tenant_id=tenant_id,
                salt=salt,
            )
            res_ents = extract_hashed_entity_fingerprints(
                c.get("result"),
                tenant_id=tenant_id,
                salt=salt,
            )
            ents = list(dict.fromkeys([*arg_ents, *res_ents]))
            c["_entity_fingerprints"] = ents
            c["_arg_entity_fingerprints"] = arg_ents
            for ent in ents:
                if ent not in all_call_entities:
                    all_call_entities.append(ent)
            for ent in arg_ents:
                if ent not in all_arg_entities:
                    all_arg_entities.append(ent)

        open_episodes = await self.store.list_open_workflow_episodes(
            tenant_id, user_sub, client_id, session_key=eff_session if session_key else None
        )
        episode = self._select_or_shift_episode(
            open_episodes,
            redacted_intent=redacted_intent,
            call_entities=set(all_call_entities),
            arg_entities=set(all_arg_entities),
            upstream_name=str(calls[0].get("upstream_name") or ""),
            session_key=eff_session,
            is_single_turn_batch=is_single_turn_batch,
        )
        if episode is None and open_episodes and redacted_intent and not is_single_turn_batch:
            latest = open_episodes[0]
            if latest.get("redacted_intent") and self._intent_shifted(
                str(latest["redacted_intent"]), redacted_intent
            ):
                await self.close_and_finalize_episode(
                    latest, close_reason="intent_shift", run_judge=True
                )

        if episode is None:
            episode = {
                "episode_id": uuid.uuid4().hex,
                "tenant_id": tenant_id,
                "user_sub": user_sub,
                "client_id": client_id,
                "session_key": eff_session,
                "upstream_name": str(calls[0].get("upstream_name") or ""),
                "matched_workflow_id": None,
                "attribution_source": "passive",
                "status": "open",
                "completion_tier": "pending",
                "confidence": 0.0,
                "completion_pct": 0,
                "redacted_intent": redacted_intent,
                "entity_fingerprints": all_call_entities,
                "created_at": now_iso,
                "updated_at": now_iso,
                "expires_at": expires_iso,
                "closed_at": None,
                "judge_verdict": None,
                "payload": {
                    "field_state": {},
                    "ordinal_counter": 0,
                    "caller_regions": list(caller_regions),
                    "arg_entities": all_arg_entities,
                },
            }
            episode = await self.store.upsert_workflow_episode(episode)
        else:
            merged_entities = list(
                dict.fromkeys([*(episode.get("entity_fingerprints") or []), *all_call_entities])
            )[:50]
            episode["entity_fingerprints"] = merged_entities
            ep_payload = dict(episode.get("payload") or {})
            ep_payload["arg_entities"] = list(
                dict.fromkeys([*(ep_payload.get("arg_entities") or []), *all_arg_entities])
            )[:50]
            episode["payload"] = ep_payload
            if caller_regions and not (episode.get("payload") or {}).get("caller_regions"):
                ep_payload = dict(episode.get("payload") or {})
                ep_payload["caller_regions"] = list(caller_regions)
                episode["payload"] = ep_payload
            if redacted_intent and not episode.get("redacted_intent"):
                episode["redacted_intent"] = redacted_intent
            episode["updated_at"] = now_iso
            episode["expires_at"] = expires_iso

        payload = dict(episode.get("payload") or {})
        field_state: dict[str, dict[str, str]] = payload.get("field_state") or {}
        ordinal = int(payload.get("ordinal_counter") or 0)

        published = await self._published_workflows(tenant_id)
        prior_calls = await self.store.list_workflow_episode_calls(tenant_id, episode["episode_id"])
        observed_tools = {str(c.get("tool_qualified_name") or "") for c in prior_calls} | {
            str(c.get("tool_qualified_name") or "") for c in calls
        }
        matched = self._match_published_workflow(
            published,
            redacted_intent=episode.get("redacted_intent"),
            observed_tools=observed_tools,
        )
        matched_row, matched_wf = matched if matched is not None else (None, None)
        if matched_row is not None and matched_wf is not None:
            episode["matched_workflow_id"] = f"{matched_row['upstream_name']}/{matched_wf.id}"
            episode["upstream_name"] = matched_row["upstream_name"]

        for call in calls:
            qname = str(call["tool_qualified_name"])
            effect = str(call.get("effect") or "read")
            status = str(call.get("status") or "failed")
            result = call.get("result")
            arguments = call.get("arguments") or {}
            diff, field_state = compute_readback_diff(
                field_state,
                entity_fingerprints=call["_entity_fingerprints"],
                effect=effect,
                status=status,
                arguments=arguments,
                result=result,
            )
            ev_matched = False
            comp_matched = False
            outcome_ns: str | None = None
            outcome_digest: str | None = None
            wf_predicates: dict[str, dict[str, Any]] = {}
            if status == "succeeded":
                for p_row, p_wf in published:
                    if qname not in p_wf.participating_tools:
                        continue
                    wf_key = f"{p_row['upstream_name']}/{p_wf.id}"
                    p_ev = False
                    p_comp = False
                    req_rule = next((r for r in p_wf.required_evidence if r.tool == qname), None)
                    if req_rule is not None:
                        p_ev = (
                            True
                            if req_rule.result_when is None
                            else result_matches_predicate(result, req_rule.result_when)
                        )
                    if p_wf.completion.tool == qname:
                        p_comp = result_matches_predicate(result, p_wf.completion.success_when)
                    wf_predicates[wf_key] = {"ev": p_ev, "comp": p_comp}
                    if p_comp and p_wf.dedupe is not None:
                        values: dict[str, Any] = {}
                        for part in p_wf.dedupe.identity_parts:
                            found, val = resolve_json_pointer(result, part.path)
                            if not found or val is None:
                                values = {}
                                break
                            values[part.path] = val
                        if values:
                            wf_predicates[wf_key]["outcome_namespace"] = p_wf.dedupe.namespace
                            wf_predicates[wf_key]["outcome_digest"] = canonical_json_digest(values)
            if matched_row is not None and matched_wf is not None:
                matched_key = f"{matched_row['upstream_name']}/{matched_wf.id}"
                matched_pred = wf_predicates.get(matched_key, {})
                ev_matched = bool(matched_pred.get("ev"))
                comp_matched = bool(matched_pred.get("comp"))
                outcome_ns = matched_pred.get("outcome_namespace")
                outcome_digest = matched_pred.get("outcome_digest")
            if wf_predicates:
                diff["wf_predicates"] = wf_predicates
            if outcome_ns and outcome_digest:
                diff["outcome_namespace"] = outcome_ns
                diff["outcome_digest"] = outcome_digest

            await self.store.append_workflow_episode_call(
                {
                    "episode_id": episode["episode_id"],
                    "tenant_id": tenant_id,
                    "run_id": run_id,
                    "ordinal": ordinal,
                    "upstream_name": str(call.get("upstream_name") or ""),
                    "tool_qualified_name": qname,
                    "effect": effect,
                    "status": status,
                    "sensitive_write_journal_status": call.get("sensitive_write_journal_status"),
                    "entity_fingerprints": call["_entity_fingerprints"],
                    "shape_diff": diff,
                    "evidence_matched": ev_matched,
                    "completion_matched": comp_matched,
                    "created_at": now_iso,
                }
            )
            ordinal += 1

        payload["field_state"] = field_state
        payload["ordinal_counter"] = ordinal
        episode["payload"] = payload
        episode = await self.store.upsert_workflow_episode(episode)

        all_calls = await self.store.list_workflow_episode_calls(tenant_id, episode["episode_id"])
        should_close, reason = self._should_close_now(
            episode=episode,
            calls=all_calls,
            matched_wf=matched_wf,
            is_single_turn_batch=is_single_turn_batch,
        )
        if should_close:
            episode = await self.close_and_finalize_episode(
                episode,
                close_reason=reason,
                matched_pair=matched,
                calls=all_calls,
                run_judge=True,
            )
        return episode

    def _entity_salt(self) -> str:
        secrets = [
            item.strip()
            for item in (
                self.settings.credential_encryption_key
                or self.settings.admin_session_secret
                or self.settings.workflow_classifier_secret
            ).split(",")
            if item.strip()
        ]
        return secrets[0] if secrets else "local-dev-entity-salt"

    def _select_or_shift_episode(
        self,
        open_episodes: list[dict[str, Any]],
        *,
        redacted_intent: str | None,
        call_entities: set[str],
        arg_entities: set[str],
        upstream_name: str,
        session_key: str = "stateless",
        is_single_turn_batch: bool,
    ) -> dict[str, Any] | None:
        explicit_run_id = session_key if session_key not in {"", "stateless"} else None
        if not open_episodes or (is_single_turn_batch and explicit_run_id is None):
            return None
        for ep in open_episodes:
            same_explicit_run = (
                explicit_run_id is not None and ep.get("session_key") == explicit_run_id
            )
            ep_arg_entities = set((ep.get("payload") or {}).get("arg_entities") or [])
            ep_entities = set(ep.get("entity_fingerprints") or [])
            shared_entity = bool(call_entities & ep_entities)
            if (
                not shared_entity
                and ep.get("redacted_intent")
                and redacted_intent
                and self._intent_shifted(str(ep["redacted_intent"]), redacted_intent)
            ):
                continue
            if not same_explicit_run:
                if arg_entities and ep_arg_entities and not (arg_entities & ep_entities):
                    continue
                if call_entities and ep_entities and not (call_entities & ep_entities):
                    continue
            if (
                not same_explicit_run
                and ep.get("upstream_name")
                and upstream_name
                and ep["upstream_name"] != upstream_name
                and not (call_entities & ep_entities)
            ):
                continue
            return ep
        return None

    @staticmethod
    def _intent_shifted(prev_intent: str, next_intent: str) -> bool:
        a = _tokenize(prev_intent)
        b = _tokenize(next_intent)
        if not a or not b:
            return False
        jaccard = len(a & b) / len(a | b)
        return jaccard < 0.15

    def _should_close_now(
        self,
        *,
        episode: dict[str, Any],
        calls: list[dict[str, Any]],
        matched_wf: WorkflowDefinition | None,
        is_single_turn_batch: bool,
    ) -> tuple[bool, str]:
        has_explicit_run_id = episode.get("session_key") not in {None, "", "stateless"}
        has_write_or_error = any(
            c.get("effect") == "write" or c.get("status") != "succeeded" for c in calls
        )
        if is_single_turn_batch and (not has_explicit_run_id or has_write_or_error):
            return True, "codemode_turn_complete"
        if matched_wf is not None and self._published_rules_satisfied(
            matched_wf, calls, upstream_name=episode.get("upstream_name")
        ):
            return True, "published_predicate_satisfied"
        if any(bool((c.get("shape_diff") or {}).get("readback_verified")) for c in calls):
            intent = str(episode.get("redacted_intent") or "").lower()
            if not _MULTI_COUNT_RE.search(intent):
                return True, "readback_verified"
        return False, ""

    @staticmethod
    def _published_rules_satisfied(
        wf: WorkflowDefinition,
        calls: list[dict[str, Any]],
        *,
        upstream_name: str | None = None,
    ) -> bool:
        wf_key = f"{upstream_name}/{wf.id}" if upstream_name else None
        adapted = []
        for c in calls:
            wf_preds = ((c.get("shape_diff") or {}).get("wf_predicates") or {}).get(
                wf_key or "", {}
            )
            ev_ok = bool(c.get("evidence_matched")) or bool(wf_preds.get("ev"))
            comp_ok = bool(c.get("completion_matched")) or bool(wf_preds.get("comp"))
            adapted.append(
                {
                    "call_key": c["call_id"],
                    "tool_qualified_name": c["tool_qualified_name"],
                    "status": c["status"],
                    "evidence": {
                        "evidence_predicate_matched": ev_ok,
                        "completion_predicate_matched": comp_ok,
                        "sensitive_write_journal_status": c.get("sensitive_write_journal_status"),
                    },
                }
            )
        return required_evidence_satisfied(wf, adapted) and terminal_completion_satisfied(
            wf, adapted
        )

    async def close_and_finalize_episode(
        self,
        episode: dict[str, Any],
        *,
        close_reason: str,
        matched_pair: tuple[dict[str, Any], WorkflowDefinition] | None = None,
        calls: list[dict[str, Any]] | None = None,
        run_judge: bool = True,
    ) -> dict[str, Any]:
        tenant_id = episode["tenant_id"]
        now_iso = datetime.now(UTC).isoformat()
        all_calls = (
            calls
            if calls is not None
            else await self.store.list_workflow_episode_calls(tenant_id, episode["episode_id"])
        )
        if matched_pair is None:
            published = await self._published_workflows(tenant_id)
            observed = {str(c.get("tool_qualified_name") or "") for c in all_calls}
            matched_pair = self._match_published_workflow(
                published,
                redacted_intent=episode.get("redacted_intent"),
                observed_tools=observed,
            )

        tier1 = self._evaluate_tier1(episode, all_calls, matched_pair)
        if hasattr(self.store, "claim_open_workflow_episode"):
            claimed = await self.store.claim_open_workflow_episode(
                tenant_id,
                episode["episode_id"],
                next_status=tier1["status"],
                closed_at=now_iso,
            )
            if claimed is None:
                existing = (
                    await self.store.get_workflow_episode(tenant_id, episode["episode_id"])
                    if hasattr(self.store, "get_workflow_episode")
                    else episode
                )
                return existing or episode
            episode = claimed

        payload = dict(episode.get("payload") or {})
        payload["close_reason"] = close_reason
        if matched_pair is not None:
            episode["matched_workflow_id"] = (
                f"{matched_pair[0]['upstream_name']}/{matched_pair[1].id}"
            )
            episode["upstream_name"] = matched_pair[0]["upstream_name"]
        episode.update(
            {
                "status": tier1["status"],
                "completion_tier": tier1["completion_tier"],
                "confidence": tier1["confidence"],
                "completion_pct": tier1["completion_pct"],
                "closed_at": episode.get("closed_at") or now_iso,
                "updated_at": now_iso,
                "payload": payload,
            }
        )
        saved = await self.store.upsert_workflow_episode(episode)

        should_defer_for_async_judge = (
            run_judge
            and self.settings.workflow_classifier_backend != "none"
            and tier1["completion_tier"] != "predicate_matched"
            and self._worker_task is not None
            and not self._worker_task.done()
        )
        if run_judge:
            if self._worker_task is not None and not self._worker_task.done():
                with contextlib.suppress(asyncio.QueueFull):
                    self._judge_queue.put_nowait((tenant_id, saved["episode_id"]))
            else:
                saved = await self.evaluate_closed_episode(
                    tenant_id, saved["episode_id"], matched_pair=matched_pair, calls=all_calls
                )
                return saved

        if not should_defer_for_async_judge:
            await self._record_outcomes_and_candidates(saved, all_calls, matched_pair)
        return saved

    def _evaluate_tier1(
        self,
        episode: dict[str, Any],
        calls: list[dict[str, Any]],
        matched_pair: tuple[dict[str, Any], WorkflowDefinition] | None,
    ) -> dict[str, Any]:
        if matched_pair is not None:
            row, wf = matched_pair
            if self._published_rules_satisfied(wf, calls, upstream_name=row["upstream_name"]):
                return {
                    "status": "completed_verified",
                    "completion_tier": "predicate_matched",
                    "confidence": 1.0,
                    "completion_pct": 100,
                }
        readback_verified = any(
            bool((c.get("shape_diff") or {}).get("readback_verified")) for c in calls
        )
        ok_writes = [
            c
            for c in calls
            if c.get("effect") == "write"
            and c.get("status") == "succeeded"
            and c.get("sensitive_write_journal_status") in {None, "succeeded"}
        ]
        failed_writes = [
            c for c in calls if c.get("effect") == "write" and c.get("status") != "succeeded"
        ]
        if ok_writes and readback_verified and not failed_writes:
            return {
                "status": "completed_verified",
                "completion_tier": "verified_readback",
                "confidence": 0.95,
                "completion_pct": 95,
            }
        if ok_writes and not failed_writes:
            return {
                "status": "completed_unverified",
                "completion_tier": "unverified_write",
                "confidence": 0.70,
                "completion_pct": 70,
            }
        if failed_writes or (ok_writes and calls and calls[-1].get("status") != "succeeded"):
            return {
                "status": "partial_or_incomplete",
                "completion_tier": "failed_or_rework",
                "confidence": 0.20,
                "completion_pct": 20,
            }
        if any(c.get("status") == "succeeded" for c in calls):
            wants_mut = bool(
                _MUTATION_INTENT_RE.search(str(episode.get("redacted_intent") or "").lower())
            )
            if wants_mut:
                return {
                    "status": "partial_or_incomplete",
                    "completion_tier": "missing_write",
                    "confidence": 0.15,
                    "completion_pct": 15,
                }
            return {
                "status": "read_only",
                "completion_tier": "read_only",
                "confidence": 0.85,
                "completion_pct": 85,
            }
        return {
            "status": "abandoned",
            "completion_tier": "abandoned",
            "confidence": 0.05,
            "completion_pct": 5,
        }

    async def evaluate_closed_episode(
        self,
        tenant_id: str,
        episode_id: str,
        *,
        matched_pair: tuple[dict[str, Any], WorkflowDefinition] | None = None,
        calls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        episode = (
            await self.store.get_workflow_episode(tenant_id, episode_id)
            if hasattr(self.store, "get_workflow_episode")
            else next(
                (
                    e
                    for e in await self.store.list_workflow_episodes(tenant_id, limit=500)
                    if e["episode_id"] == episode_id
                ),
                None,
            )
        )
        if episode is None:
            raise KeyError(f"workflow episode {episode_id} not found")
        all_calls = (
            calls
            if calls is not None
            else await self.store.list_workflow_episode_calls(tenant_id, episode_id)
        )
        if matched_pair is None:
            published = await self._published_workflows(tenant_id)
            observed = {str(c.get("tool_qualified_name") or "") for c in all_calls}
            matched_pair = self._match_published_workflow(
                published,
                redacted_intent=episode.get("redacted_intent"),
                observed_tools=observed,
            )
        req_tools = (
            [item.tool for item in matched_pair[1].required_evidence]
            if matched_pair is not None
            else None
        )
        verdict = await self._run_tier2_judge(episode, all_calls, required_evidence_tools=req_tools)
        saved = episode
        if verdict is not None:
            has_failed_write = any(
                c.get("effect") == "write" and c.get("status") != "succeeded" for c in all_calls
            )
            updated = dict(episode)
            updated["judge_verdict"] = verdict.model_dump(mode="json")
            if (
                not has_failed_write
                and updated["status"] in {"completed_verified", "completed_unverified"}
                and (
                    not verdict.completed
                    or verdict.confidence < self.settings.workflow_judge_confidence_threshold
                    or bool(verdict.missing_goals)
                )
                and updated["completion_tier"] != "predicate_matched"
            ):
                updated["status"] = "partial_or_incomplete"
                updated["completion_tier"] = "judge_downgraded"
                updated["confidence"] = verdict.confidence
                updated["completion_pct"] = verdict.completion_pct
            elif updated["status"] not in {"partial_or_incomplete", "abandoned"}:
                updated["confidence"] = round(
                    min(float(updated.get("confidence", 0.0)), verdict.confidence)
                    if updated["completion_tier"] != "predicate_matched"
                    else float(updated.get("confidence", 1.0)),
                    4,
                )
                updated["completion_pct"] = int(round(float(updated["confidence"]) * 100))
            saved = await self.store.upsert_workflow_episode(updated)

        if not (saved.get("payload") or {}).get("outcomes_recorded"):
            await self._record_outcomes_and_candidates(saved, all_calls, matched_pair)
            ep_payload = dict(saved.get("payload") or {})
            ep_payload["outcomes_recorded"] = True
            saved["payload"] = ep_payload
            saved = await self.store.upsert_workflow_episode(saved)
        return saved

    async def _run_tier2_judge(
        self,
        episode: dict[str, Any],
        calls: list[dict[str, Any]],
        *,
        required_evidence_tools: list[str] | None = None,
    ) -> System1JudgeVerdict | None:
        backend = self.settings.workflow_classifier_backend
        if backend == "none":
            return None
        if backend == "synthetic_heuristic":
            return self.heuristic.judge_episode(
                redacted_intent=episode.get("redacted_intent"),
                steps=calls,
                matched_workflow_id=episode.get("matched_workflow_id"),
                required_evidence_tools=required_evidence_tools,
            )
        if backend == "local_http" and self.settings.workflow_classifier_url:
            try:
                validate_upstream_url(
                    self.settings.workflow_classifier_url,
                    app_env=self.settings.app_env,
                    block_private_ips_in_prod=self.settings.block_private_ips_in_prod,
                    allowed_hosts=self.settings.workflow_classifier_allowed_hosts,
                )
            except ValueError:
                return self.heuristic.judge_episode(
                    redacted_intent=episode.get("redacted_intent"),
                    steps=calls,
                    matched_workflow_id=episode.get("matched_workflow_id"),
                    required_evidence_tools=required_evidence_tools,
                )
            url = self.settings.workflow_classifier_url.rstrip("/") + "/v1/judge-episode"
            headers = {"Content-Type": "application/json"}
            secrets = self.settings.workflow_classifier_secrets
            if secrets:
                headers["Authorization"] = f"Bearer {secrets[0]}"
            payload = {
                "redacted_intent": episode.get("redacted_intent"),
                "matched_workflow_id": episode.get("matched_workflow_id"),
                "steps": [
                    {
                        "tool_qualified_name": c.get("tool_qualified_name"),
                        "effect": c.get("effect"),
                        "status": c.get("status"),
                        "sensitive_write_journal_status": c.get("sensitive_write_journal_status"),
                        "shape_diff": c.get("shape_diff"),
                        "completion_matched": c.get("completion_matched"),
                    }
                    for c in calls
                ],
            }
            try:
                if self.http_client is not None:
                    resp = await self.http_client.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=self.settings.workflow_classifier_timeout_seconds,
                    )
                else:
                    async with httpx.AsyncClient(
                        timeout=self.settings.workflow_classifier_timeout_seconds
                    ) as client:
                        resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                return System1JudgeVerdict.model_validate(resp.json())
            except Exception:
                return self.heuristic.judge_episode(
                    redacted_intent=episode.get("redacted_intent"),
                    steps=calls,
                    matched_workflow_id=episode.get("matched_workflow_id"),
                    required_evidence_tools=required_evidence_tools,
                )
        if (
            backend == "openai_compatible"
            and self.settings.workflow_classifier_url
            and self.settings.workflow_classifier_model
        ):
            try:
                secrets = self.settings.workflow_classifier_secrets
                return await call_openai_compatible_judge(
                    base_url=self.settings.workflow_classifier_url,
                    model=self.settings.workflow_classifier_model,
                    redacted_intent=episode.get("redacted_intent"),
                    steps=calls,
                    api_key=secrets[0] if secrets else None,
                    timeout_seconds=self.settings.workflow_classifier_timeout_seconds,
                    app_env=self.settings.app_env,
                    block_private_ips_in_prod=self.settings.block_private_ips_in_prod,
                    allowed_hosts=self.settings.workflow_classifier_allowed_hosts,
                    http_client=self.http_client,
                )
            except Exception:
                return self.heuristic.judge_episode(
                    redacted_intent=episode.get("redacted_intent"),
                    steps=calls,
                    matched_workflow_id=episode.get("matched_workflow_id"),
                    required_evidence_tools=required_evidence_tools,
                )
        return None

    async def _record_outcomes_and_candidates(
        self,
        episode: dict[str, Any],
        calls: list[dict[str, Any]],
        matched_pair: tuple[dict[str, Any], WorkflowDefinition] | None,
    ) -> None:
        tenant_id = episode["tenant_id"]
        if episode["status"] == "completed_verified" and matched_pair is not None:
            row, wf = matched_pair
            if not self._published_rules_satisfied(wf, calls, upstream_name=row["upstream_name"]):
                return
            wf_key = f"{row['upstream_name']}/{wf.id}"
            terminal_call = next(
                (
                    c
                    for c in reversed(calls)
                    if c.get("tool_qualified_name") == wf.completion.tool
                    and c.get("status") == "succeeded"
                    and (
                        c.get("completion_matched")
                        or (
                            ((c.get("shape_diff") or {}).get("wf_predicates") or {})
                            .get(wf_key, {})
                            .get("comp")
                        )
                    )
                ),
                None,
            )
            if terminal_call is None:
                return
            terminal_diff = terminal_call.get("shape_diff") or {}
            wf_pred = (terminal_diff.get("wf_predicates") or {}).get(wf_key, {})
            outcome_ns = terminal_diff.get("outcome_namespace") or wf_pred.get("outcome_namespace")
            outcome_digest = terminal_diff.get("outcome_digest") or wf_pred.get("outcome_digest")
            if wf.dedupe is not None and not outcome_digest:
                return
            ep_regions = (episode.get("payload") or {}).get("caller_regions") or []
            baseline = apply_regional_baseline(
                wf.business_value.model_dump(mode="json"),
                ep_regions,
            )
            event = {
                "event_id": uuid.uuid4().hex,
                "tenant_id": tenant_id,
                "business_execution_id": episode["episode_id"],
                "completion_key": "passive-business-value-completion-v1",
                "upstream_name": row["upstream_name"],
                "outcome_namespace": outcome_ns,
                "outcome_digest": outcome_digest,
                "credited": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "evidence": {
                    "definition_revision": row["revision"],
                    "selected_id": f"{row['upstream_name']}/{wf.id}",
                    "selected_kind": "workflow",
                    "attribution_mode": "passive_matched",
                    "completion_tier": episode["completion_tier"],
                    "confidence": episode["confidence"],
                    "completion_pct": episode["completion_pct"],
                    "integration_id": row["definition"].get("integration", {}).get("id"),
                    "integration_display_name": row["definition"]
                    .get("integration", {})
                    .get("display_name"),
                    "baseline_snapshot": baseline,
                    "episode_id": episode["episode_id"],
                    "workflow_call_id": terminal_call["call_id"],
                    "required_evidence_satisfied": True,
                    "completion_predicate_matched": True,
                },
            }
            if hasattr(self.store, "record_business_value_event"):
                await self.store.record_business_value_event(event)
            return

        if (
            matched_pair is None
            and len(calls) >= 2
            and any(c.get("effect") == "write" and c.get("status") == "succeeded" for c in calls)
            and hasattr(self.store, "upsert_discovered_workflow_candidate")
        ):
            upstream = str(episode.get("upstream_name") or calls[0]["upstream_name"])
            if self.registry is not None:
                upstream_cfg = self.registry.get(upstream)
                if upstream_cfg is not None and not getattr(
                    upstream_cfg, "auto_discovery_enabled", True
                ):
                    return
            seq_tools = list(
                dict.fromkeys(
                    str(c["tool_qualified_name"])
                    for c in calls
                    if str(c["tool_qualified_name"]).startswith(f"{upstream}__")
                )
            )
            if len(seq_tools) < 2:
                return
            last_qname = str(calls[-1].get("tool_qualified_name") or "")
            last_readback = bool((calls[-1].get("shape_diff") or {}).get("readback_verified"))
            if (
                episode["status"] == "completed_verified"
                and last_readback
                and last_qname.startswith(f"{upstream}__")
                and calls[-1].get("status") == "succeeded"
            ):
                terminal = last_qname
                terminal_ok_count = sum(
                    1
                    for c in calls
                    if c.get("tool_qualified_name") == terminal and c.get("status") == "succeeded"
                )
                req_evidence = [
                    {"tool": t, "min_successful_calls": 1} for t in seq_tools if t != terminal
                ]
                if terminal_ok_count >= 2:
                    req_evidence.append(
                        {"tool": terminal, "min_successful_calls": min(terminal_ok_count, 10)}
                    )
                completion_kind = "verified_followup"
            else:
                terminal = seq_tools[-1]
                req_evidence = [{"tool": t, "min_successful_calls": 1} for t in seq_tools[:-1]]
                completion_kind = "terminal_tool_result"
            terminal_tool = self.catalog.get_tool(terminal)
            completion_pred = pick_schema_predicate(terminal_tool)
            slug = _slugify(f"auto_{terminal.split('__', 1)[-1]}_{len(seq_tools)}step")
            sample_intents = (
                [str(episode["redacted_intent"])] if episode.get("redacted_intent") else []
            )
            draft = {
                "id": slug,
                "name": f"Auto-discovered: {terminal.split('__', 1)[-1].replace('_', ' ')}",
                "description": (f"Auto-discovered passive workflow over {', '.join(seq_tools)}."),
                "origin": "auto_mined",
                "intent_examples": sample_intents,
                "participating_tools": seq_tools,
                "required_evidence": req_evidence,
                "completion": {
                    "kind": completion_kind,
                    "tool": terminal,
                    "success_when": completion_pred,
                    **(
                        {"require_sensitive_write_journal_status": "succeeded"}
                        if terminal_tool and terminal_tool.mutating
                        else {}
                    ),
                },
                "needs_baseline_confirmation": True,
            }
            await self.store.upsert_discovered_workflow_candidate(
                {
                    "tenant_id": tenant_id,
                    "upstream_name": upstream,
                    "workflow_slug": slug,
                    "fingerprint": compute_workflow_fingerprint(
                        upstream_name=upstream,
                        participating_tools=seq_tools,
                        completion_tool=terminal,
                        completion_kind=completion_kind,
                    ),
                    "origin": "auto_mined",
                    "status": "pending_review",
                    "observed_count": 1,
                    "verified_count": 1 if episode["status"] == "completed_verified" else 0,
                    "sample_intents": sample_intents,
                    "draft_definition": draft,
                }
            )
