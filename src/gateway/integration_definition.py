"""Validated, administrator-authored integration definitions.

These models contain attribution and outcome declarations only. They deliberately do
not contain credentials, gateway authorization rules, or copied upstream schemas.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import date
from typing import Any, Literal

from jsonschema import Draft7Validator, Draft201909Validator, Draft202012Validator
from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict, Field, model_validator

_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_SUPPORTED_JSON_SCHEMA_DIALECTS = {
    Draft7Validator.META_SCHEMA["$id"],
    Draft201909Validator.META_SCHEMA["$id"],
    Draft202012Validator.META_SCHEMA["$id"],
}


class DefinitionModel(BaseModel):
    """Base for strict authored manifest models."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolReference(DefinitionModel):
    name: str = Field(min_length=1, max_length=240)
    schema_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class IntegrationMetadata(DefinitionModel):
    id: str = Field(pattern=_SLUG_PATTERN)
    display_name: str = Field(min_length=1, max_length=120)
    upstream_name: str = Field(min_length=1, max_length=120)
    status: Literal["draft", "published", "retired"] = "draft"
    revision: int = Field(default=1, ge=1)


class ResultPredicate(DefinitionModel):
    """A bounded structural result check, never executable code."""

    path: str = Field(min_length=1, max_length=512)
    type: Literal[
        "string",
        "non_empty_string",
        "object",
        "non_empty_object",
        "array",
        "non_empty_array",
        "boolean",
        "number",
        "integer",
        "null",
    ]
    equals: str | int | float | bool | None = None
    one_of: list[str | int | float | bool | None] | None = Field(
        default=None, min_length=1, max_length=20
    )

    @model_validator(mode="after")
    def _validate_comparison(self) -> ResultPredicate:
        _decode_json_pointer(self.path)
        has_equals = self.equals is not None
        if has_equals and self.one_of is not None:
            raise ValueError("result predicate cannot specify both equals and one_of")
        if has_equals:
            expected_type = {
                "string": str,
                "non_empty_string": str,
                "boolean": bool,
                "number": (int, float),
                "integer": int,
                "null": type(None),
            }.get(self.type)
            if expected_type is None or not isinstance(self.equals, expected_type):
                raise ValueError("equals value must match a scalar result predicate type")
        if self.type in {"object", "non_empty_object", "array", "non_empty_array"} and (
            self.equals is not None or self.one_of is not None
        ):
            raise ValueError("object and array result predicates cannot use scalar comparisons")
        return self


class RequiredEvidence(DefinitionModel):
    tool: str = Field(min_length=1, max_length=240)
    min_successful_calls: int = Field(default=1, ge=1, le=10)
    result_when: ResultPredicate | None = None


class CompletionRule(DefinitionModel):
    kind: Literal["terminal_tool_result", "verified_followup"] = "terminal_tool_result"
    tool: str = Field(min_length=1, max_length=240)
    success_when: ResultPredicate
    require_sensitive_write_journal_status: Literal["succeeded"] | None = None


class IdentityPart(DefinitionModel):
    source: Literal["terminal_result"] = "terminal_result"
    path: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def _validate_path(self) -> IdentityPart:
        _decode_json_pointer(self.path)
        return self


class OutcomeDeduplication(DefinitionModel):
    namespace: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_.-]+$")
    identity_parts: list[IdentityPart] = Field(min_length=1, max_length=8)


class CapturedMinutes(DefinitionModel):
    kind: Literal["capture_actual_when_available"]


class EstimatedMinutes(DefinitionModel):
    kind: Literal["expected_average"]
    minutes: float = Field(ge=0, le=1440)


MinuteAssumption = CapturedMinutes | EstimatedMinutes


class BusinessValueBaseline(DefinitionModel):
    manual_baseline_minutes: float = Field(gt=0, le=100_000)
    baseline_source: Literal[
        "administrator_estimate", "sampled_manual_measurement", "validated_external_measurement"
    ]
    confidence: Literal["unvalidated", "sampled", "validated"]
    loaded_labor_cost_per_hour: float | None = Field(default=None, ge=0, le=1_000_000)
    currency: str | None = Field(default=None, pattern=_CURRENCY_PATTERN)
    effective_from: date
    evidence_note: str | None = Field(default=None, max_length=1000)
    review_minutes: MinuteAssumption | None = None
    correction_minutes: MinuteAssumption | None = None

    @model_validator(mode="after")
    def _validate_rate_currency(self) -> BusinessValueBaseline:
        if (self.loaded_labor_cost_per_hour is None) != (self.currency is None):
            raise ValueError("loaded labor cost and currency must be supplied together")
        if self.confidence == "validated" and self.baseline_source == "administrator_estimate":
            raise ValueError("an administrator estimate cannot be marked validated")
        if self.confidence == "unvalidated" and self.baseline_source != "administrator_estimate":
            raise ValueError("sampled or external measurements cannot be marked unvalidated")
        return self


class WorkflowExecutionSettings(DefinitionModel):
    absolute_ttl_minutes: int = Field(default=1440, ge=1, le=100_800)
    idle_ttl_minutes: int = Field(default=30, ge=1, le=1440)
    allow_parallel_reads: bool = True
    serialize_writes: Literal[True] = True

    @model_validator(mode="after")
    def _validate_ttls(self) -> WorkflowExecutionSettings:
        if self.idle_ttl_minutes > self.absolute_ttl_minutes:
            raise ValueError("idle TTL cannot exceed absolute TTL")
        return self


def compute_workflow_fingerprint(
    *,
    upstream_name: str,
    participating_tools: Iterable[str],
    completion_tool: str,
    completion_kind: str = "terminal_tool_result",
) -> str:
    """Build a canonical structural fingerprint shared across schema, live, and mined drafts."""

    payload = {
        "upstream_name": upstream_name.strip(),
        "participating_tools": sorted(
            {str(t).strip() for t in participating_tools if str(t).strip()}
        ),
        "completion_tool": completion_tool.strip(),
        "completion_kind": completion_kind.strip() or "terminal_tool_result",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"wfp_{hashlib.sha256(encoded).hexdigest()[:24]}"


class WorkflowDefinition(DefinitionModel):
    id: str = Field(pattern=_SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    enabled: bool = True
    origin: Literal["manual", "auto_schema", "auto_mined", "agent_prompt"] = "manual"
    intent_examples: list[str] = Field(default_factory=list, max_length=25)
    participating_tools: list[str] = Field(min_length=1, max_length=64)
    required_evidence: list[RequiredEvidence] = Field(default_factory=list, max_length=64)
    completion: CompletionRule
    dedupe: OutcomeDeduplication | None = None
    business_value: BusinessValueBaseline
    execution: WorkflowExecutionSettings = Field(default_factory=WorkflowExecutionSettings)

    @model_validator(mode="after")
    def _validate_membership(self) -> WorkflowDefinition:
        if len(set(self.participating_tools)) != len(self.participating_tools):
            raise ValueError("participating_tools must not contain duplicates")
        participants = set(self.participating_tools)
        if self.completion.tool not in participants:
            raise ValueError("completion tool must be in participating_tools")
        evidence_tools = [item.tool for item in self.required_evidence]
        if len(set(evidence_tools)) != len(evidence_tools):
            raise ValueError("required_evidence must have one entry per tool")
        unknown_evidence = set(evidence_tools) - participants
        if unknown_evidence:
            raise ValueError("required_evidence tools must be in participating_tools")
        return self


class StandaloneToolValueDefinition(DefinitionModel):
    id: str = Field(pattern=_SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    tool: str = Field(min_length=1, max_length=240)
    completion: CompletionRule
    dedupe: OutcomeDeduplication | None = None
    business_value: BusinessValueBaseline

    @model_validator(mode="after")
    def _validate_tool(self) -> StandaloneToolValueDefinition:
        if self.completion.tool != self.tool:
            raise ValueError("standalone completion tool must match selected tool")
        return self


class ExperienceDefinition(DefinitionModel):
    """Small manifest reference to a separately validated constrained UI spec."""

    id: str = Field(pattern=_SLUG_PATTERN)
    resource_uri: str = Field(min_length=1, max_length=512)
    renderer_schema: str = Field(min_length=1, max_length=100)
    tool: str = Field(min_length=1, max_length=240)
    fallback: str = Field(min_length=1, max_length=500)
    input_mapping: dict[str, str] = Field(default_factory=dict, max_length=64)
    components: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    csp: dict[str, list[str]] = Field(default_factory=dict, max_length=4)


class PublicationMetadata(DefinitionModel):
    validator_version: str = Field(min_length=1, max_length=40)
    content_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    published_by: str | None = Field(default=None, max_length=240)
    published_at: str | None = Field(default=None, max_length=64)


class IntegrationDefinition(DefinitionModel):
    manifest_version: Literal["1.0"] = "1.0"
    integration: IntegrationMetadata
    tool_refs: list[ToolReference] = Field(default_factory=list, max_length=256)
    experiences: list[ExperienceDefinition] = Field(default_factory=list, max_length=32)
    workflows: list[WorkflowDefinition] = Field(default_factory=list, max_length=64)
    standalone_tool_values: list[StandaloneToolValueDefinition] = Field(
        default_factory=list, max_length=64
    )
    publication: PublicationMetadata | None = None

    @model_validator(mode="after")
    def _validate_definition_references(self) -> IntegrationDefinition:
        refs = [item.name for item in self.tool_refs]
        if len(set(refs)) != len(refs):
            raise ValueError("tool_refs must be unique")
        known_tools = set(refs)
        if len({item.id for item in self.workflows}) != len(self.workflows):
            raise ValueError("workflow IDs must be unique")
        if len({item.id for item in self.standalone_tool_values}) != len(
            self.standalone_tool_values
        ):
            raise ValueError("standalone tool-value IDs must be unique")
        if len({item.id for item in self.experiences}) != len(self.experiences):
            raise ValueError("experience IDs must be unique")
        workflow_terminal_tools = {item.completion.tool for item in self.workflows}
        overlapping = workflow_terminal_tools & {item.tool for item in self.standalone_tool_values}
        if overlapping:
            raise ValueError(
                "workflow and standalone definitions cannot share terminal tools: "
                + ", ".join(sorted(overlapping))
            )
        for workflow in self.workflows:
            missing = set(workflow.participating_tools) - known_tools
            if missing:
                raise ValueError(
                    f"workflow references tools missing from tool_refs: {sorted(missing)}"
                )
        for standalone in self.standalone_tool_values:
            if standalone.tool not in known_tools:
                raise ValueError("standalone tool value references a tool missing from tool_refs")
        for experience in self.experiences:
            if experience.tool not in known_tools:
                raise ValueError("experience references a tool missing from tool_refs")
        if self.integration.status == "published" and self.publication is None:
            raise ValueError("published integration requires publication metadata")
        return self


def _decode_json_pointer(pointer: str) -> list[str]:
    """Decode and minimally validate an RFC 6901 JSON Pointer."""

    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must start with '/'")
    tokens: list[str] = []
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError("JSON Pointer has an invalid '~' escape")
        tokens.append(token.replace("~1", "/").replace("~0", "~"))
    return tokens


def validate_result_predicate_schema(
    predicate: ResultPredicate, output_schema: dict[str, Any]
) -> None:
    """Ensure a result predicate path exists and its declared type fits an output schema.

    This helper intentionally supports local `$ref`s and ordinary `properties`/`items`
    nesting. A path hidden behind ambiguous composition or an external reference fails
    closed instead of guessing about the upstream contract.
    """

    dialect = output_schema.get("$schema")
    if dialect is not None and dialect not in _SUPPORTED_JSON_SCHEMA_DIALECTS:
        raise ValueError(f"unsupported JSON Schema dialect: {dialect}")
    validator = validator_for(output_schema)
    validator.check_schema(output_schema)
    tokens = _decode_json_pointer(predicate.path)
    root = output_schema
    current: list[dict[str, Any]] = [output_schema]
    for token in tokens:
        next_nodes = [
            child
            for node in current
            for child in _descend_schema(node, token, root, ref_stack=frozenset())
        ]
        if not next_nodes:
            raise ValueError(
                f"predicate path {predicate.path!r} is not declared by the output schema"
            )
        current = next_nodes
    declared_types = {
        schema_type
        for node in current
        for schema_type in _schema_type(
            _resolve_local_ref(node, root)[1] if _resolve_local_ref(node, root) else node
        )
    }
    expected = _PREDICATE_TYPES[predicate.type]
    if declared_types and not (declared_types & expected):
        raise ValueError(
            f"predicate type {predicate.type!r} is incompatible with output schema type(s) "
            f"{sorted(declared_types)}"
        )


def _resolve_local_ref(
    node: dict[str, Any], root: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    ref = node.get("$ref")
    if ref is None:
        return None
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ValueError("only resolvable local JSON Schema references are supported here")
    resolved: Any = root
    for token in _decode_json_pointer(ref[1:]):
        if not isinstance(resolved, dict) or token not in resolved:
            raise ValueError(f"unresolved local JSON Schema reference: {ref}")
        resolved = resolved[token]
    if not isinstance(resolved, dict):
        raise ValueError("referenced JSON Schema node must be an object")
    return ref, resolved


def _descend_schema(
    node: dict[str, Any], token: str, root: dict[str, Any], *, ref_stack: frozenset[str]
) -> list[dict[str, Any]]:
    """Find schemas at one pointer token, failing closed on ambiguous branches."""

    result: list[dict[str, Any]] = []
    ref_target = _resolve_local_ref(node, root)
    if ref_target is not None:
        ref, target = ref_target
        if ref in ref_stack:
            raise ValueError("recursive JSON Schema references are unsupported for predicate paths")
        siblings = {key: value for key, value in node.items() if key != "$ref"}
        result.extend(_descend_schema(target, token, root, ref_stack=ref_stack | {ref}))
        if siblings:
            result.extend(_descend_schema(siblings, token, root, ref_stack=ref_stack))
        return result

    for operator in ("oneOf", "anyOf"):
        if operator in node:
            branches = node[operator]
            if not isinstance(branches, list) or not branches:
                raise ValueError(f"{operator} must have at least one schema branch")
            branch_nodes = [
                _descend_schema(branch, token, root, ref_stack=ref_stack)
                for branch in branches
                if isinstance(branch, dict)
            ]
            if len(branch_nodes) != len(branches) or any(not branch for branch in branch_nodes):
                raise ValueError(f"predicate path must exist in every {operator} branch")
            result.extend(child for branch in branch_nodes for child in branch)

    if "allOf" in node:
        branches = node["allOf"]
        if not isinstance(branches, list) or not branches:
            raise ValueError("allOf must have at least one schema branch")
        for branch in branches:
            if isinstance(branch, dict):
                result.extend(_descend_schema(branch, token, root, ref_stack=ref_stack))

    properties = node.get("properties")
    if isinstance(properties, dict) and isinstance(properties.get(token), dict):
        result.append(properties[token])
    if token.isdigit() and isinstance(node.get("items"), dict):
        result.append(node["items"])
    return result


_PREDICATE_TYPES: dict[str, set[str]] = {
    "string": {"string"},
    "non_empty_string": {"string"},
    "object": {"object"},
    "non_empty_object": {"object"},
    "array": {"array"},
    "non_empty_array": {"array"},
    "boolean": {"boolean"},
    "number": {"number", "integer"},
    "integer": {"integer"},
    "null": {"null"},
}


def _schema_type(schema: dict[str, Any]) -> set[str]:
    value = schema.get("type")
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    return set()


def validate_definition_output_schemas(
    definition: IntegrationDefinition, output_schemas: dict[str, dict[str, Any]]
) -> None:
    """Validate every declared completion/evidence predicate against catalog outputs."""

    for workflow in definition.workflows:
        for evidence in workflow.required_evidence:
            if evidence.result_when is not None:
                _validate_tool_predicate(evidence.tool, evidence.result_when, output_schemas)
        _validate_tool_predicate(
            workflow.completion.tool, workflow.completion.success_when, output_schemas
        )
    for standalone in definition.standalone_tool_values:
        _validate_tool_predicate(
            standalone.tool, standalone.completion.success_when, output_schemas
        )


def _validate_tool_predicate(
    tool_name: str, predicate: ResultPredicate, output_schemas: dict[str, dict[str, Any]]
) -> None:
    schema = output_schemas.get(tool_name)
    if schema is None:
        raise ValueError(f"output schema is unavailable for predicate tool {tool_name!r}")
    try:
        validate_result_predicate_schema(predicate, schema)
    except ValueError as exc:
        raise ValueError(f"invalid result predicate for {tool_name!r}: {exc}") from exc
