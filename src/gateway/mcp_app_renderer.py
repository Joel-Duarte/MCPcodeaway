"""Render a deliberately small, safe subset of declarative MCP App experiences.

Supported nodes are ``heading`` and ``text`` with a plain-text ``text`` value;
``text_input`` with ``id``, ``label``, optional ``required`` and bounded
``max_length``; ``select`` with ``id``, ``label``, ``options``, and optional
``required``; ``button`` with one safe action ID; and ``record`` with
``data: "tool_result"``. The action ID is descriptive only: the single button
can call only the experience's declared gateway tool through the standard MCP
Apps host ``tools/call`` bridge. Input
mapping values must be ``/form/<text_input id>`` JSON-Pointer-like references.

No manifest HTML, JavaScript, CSS, event attributes, external URLs, resources,
permissions, branching or arbitrary action code is accepted. The HTML and bridge
are product-owned and all dynamic values are escaped or written with DOM
``textContent``.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from jsonschema.validators import validator_for

MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"
MCP_APP_EXTENSION = "io.modelcontextprotocol/ui"
MCP_APP_PROTOCOL_VERSION = "2026-01-26"
RENDERER_SCHEMA = "mcpcodeaway-ui/1"
MAX_COMPONENTS = 40
MAX_COMPONENT_TEXT = 2000
MAX_SPEC_BYTES = 16_384
MAX_HTML_BYTES = 65_536
MAX_RESULT_TEXT = 12_000

_RESOURCE_URI = re.compile(r"^ui://[a-z0-9][a-z0-9.-]*(?:/[A-Za-z0-9._~-]+)*$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.-]*__[A-Za-z][A-Za-z0-9_.-]*$")
_NODE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_FIELD_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,127}$")
_ACTION_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_URL_OR_ACTIVE_MARKUP = re.compile(
    r"(?:\b[a-z][a-z0-9+.-]{1,20}:\s*//|\b(?:javascript|data|mailto)\s*:|"
    r"\bwww\.|<|>|\bon[a-z]+\s*=)",
    re.IGNORECASE,
)
_SECRET_NAME = re.compile(
    r"(?:password|passwd|secret|token|credential|authorization|api[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)

_CSP_DOMAINS = {
    "connect_domains": "connectDomains",
    "resource_domains": "resourceDomains",
    "frame_domains": "frameDomains",
}


def validate_experience(
    experience: dict[str, Any], *, tool_input_schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Validate and normalize the supported declarative experience subset.

    ``tool_input_schema`` is required for an interactive ``button`` and prevents
    mappings from calling undeclared or secret-like tool parameters.
    """
    if not isinstance(experience, dict):
        raise ValueError("experience must be an object")
    allowed = {
        "id",
        "resource_uri",
        "renderer_schema",
        "tool",
        "fallback",
        "input_mapping",
        "components",
        "csp",
    }
    if set(experience) - allowed:
        raise ValueError("experience contains unsupported fields")
    normalized = dict(experience)
    if normalized.get("renderer_schema") != RENDERER_SCHEMA:
        raise ValueError(f"renderer_schema must be {RENDERER_SCHEMA}")
    resource_uri = normalized.get("resource_uri")
    if not isinstance(resource_uri, str) or not _RESOURCE_URI.fullmatch(resource_uri):
        raise ValueError("resource_uri must be a local ui:// URI")
    tool = normalized.get("tool")
    if not isinstance(tool, str) or not _TOOL_NAME.fullmatch(tool):
        raise ValueError("experience tool must be a qualified gateway tool name")
    fallback = normalized.get("fallback")
    if not isinstance(fallback, str) or not fallback.strip() or len(fallback) > 500:
        raise ValueError("fallback must be non-empty plain text of at most 500 characters")
    _validate_plain_text(fallback, "fallback")

    csp = normalized.get("csp", {})
    if not isinstance(csp, dict) or set(csp) - set(_CSP_DOMAINS):
        raise ValueError("CSP may contain only connect_domains, resource_domains and frame_domains")
    if any(not isinstance(values, list) or values for values in csp.values()):
        raise ValueError("experience CSP domains must be empty lists")

    components = normalized.get("components", [])
    mapping = normalized.get("input_mapping", {})
    if not isinstance(components, list) or len(components) > MAX_COMPONENTS:
        raise ValueError(f"components must be a list of at most {MAX_COMPONENTS} nodes")
    if not isinstance(mapping, dict) or len(mapping) > 32:
        raise ValueError("input_mapping must be an object with at most 32 entries")
    try:
        spec_size = len(
            json.dumps(
                normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("experience must contain only JSON-compatible values") from exc
    if spec_size > MAX_SPEC_BYTES:
        raise ValueError(f"experience declarative data exceeds {MAX_SPEC_BYTES} bytes")

    input_ids: set[str] = set()
    button_count = 0
    for index, node in enumerate(components):
        if not isinstance(node, dict) or not isinstance(node.get("type"), str):
            raise ValueError(f"component {index} must be an object with a type")
        kind = node["type"]
        schemas = {
            "heading": {"type", "text"},
            "text": {"type", "text"},
            "text_input": {"type", "id", "label", "required", "max_length"},
            "select": {"type", "id", "label", "options", "required"},
            "button": {"type", "label", "action"},
            "record": {"type", "data"},
        }
        if kind not in schemas or set(node) - schemas[kind]:
            raise ValueError(f"component {index} uses an unsupported node or property")
        if kind in {"heading", "text"}:
            text = node.get("text")
            if not isinstance(text, str) or len(text) > MAX_COMPONENT_TEXT:
                raise ValueError(f"component {index} text is invalid or too long")
            _validate_plain_text(text, f"component {index} text")
        elif kind == "text_input":
            field_id = node.get("id")
            label = node.get("label")
            max_length = node.get("max_length", 256)
            if not isinstance(field_id, str) or not _NODE_ID.fullmatch(field_id):
                raise ValueError(f"component {index} has an invalid text input id")
            if field_id in input_ids:
                raise ValueError(f"duplicate text input id: {field_id}")
            input_ids.add(field_id)
            if not isinstance(label, str) or not label.strip() or len(label) > 200:
                raise ValueError(f"component {index} has an invalid text input label")
            _validate_plain_text(label, f"component {index} label")
            if not isinstance(node.get("required", False), bool):
                raise ValueError(f"component {index} required must be boolean")
            if (
                isinstance(max_length, bool)
                or not isinstance(max_length, int)
                or not 1 <= max_length <= 2000
            ):
                raise ValueError(f"component {index} max_length must be between 1 and 2000")
        elif kind == "select":
            field_id = node.get("id")
            label = node.get("label")
            options = node.get("options")
            if not isinstance(field_id, str) or not _NODE_ID.fullmatch(field_id):
                raise ValueError(f"component {index} has an invalid select id")
            if field_id in input_ids:
                raise ValueError(f"duplicate text input id: {field_id}")
            input_ids.add(field_id)
            if not isinstance(label, str) or not label.strip() or len(label) > 200:
                raise ValueError(f"component {index} has an invalid select label")
            _validate_plain_text(label, f"component {index} label")
            if not isinstance(node.get("required", False), bool):
                raise ValueError(f"component {index} required must be boolean")
            if not isinstance(options, list) or not 1 <= len(options) <= 50:
                raise ValueError(f"component {index} options must contain 1 to 50 items")
            for opt_index, opt in enumerate(options):
                if not isinstance(opt, dict) or set(opt) - {"value", "label"}:
                    raise ValueError(
                        f"component {index} option {opt_index} must declare value and label"
                    )
                opt_val = opt.get("value")
                opt_lbl = opt.get("label")
                if not isinstance(opt_val, str) or not opt_val or len(opt_val) > 120:
                    raise ValueError(f"component {index} option {opt_index} value is invalid")
                if not isinstance(opt_lbl, str) or not opt_lbl.strip() or len(opt_lbl) > 120:
                    raise ValueError(f"component {index} option {opt_index} label is invalid")
                _validate_plain_text(opt_val, f"component {index} option value")
                _validate_plain_text(opt_lbl, f"component {index} option label")
        elif kind == "button":
            button_count += 1
            if button_count > 1:
                raise ValueError("an experience supports one deterministic invoke_tool button")
            label = node.get("label")
            if not isinstance(label, str) or not label.strip() or len(label) > 100:
                raise ValueError(f"component {index} has an invalid button label")
            _validate_plain_text(label, f"component {index} button label")
            if not isinstance(node.get("action"), str) or not _ACTION_ID.fullmatch(node["action"]):
                raise ValueError("button action must be a safe action ID")
        elif kind == "record" and node.get("data") != "tool_result":
            raise ValueError("record nodes may display only tool_result")

    if input_ids and not button_count:
        raise ValueError(
            "text inputs require an invoke_tool button; inert controls are not supported"
        )
    if button_count and tool_input_schema is None:
        raise ValueError("interactive experiences require the catalog tool input schema")
    if not button_count and mapping:
        raise ValueError("input_mapping requires an invoke_tool button")
    if button_count:
        properties = (
            tool_input_schema.get("properties") if isinstance(tool_input_schema, dict) else None
        )
        required = (
            tool_input_schema.get("required", []) if isinstance(tool_input_schema, dict) else []
        )
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ValueError("tool input schema must declare properties and required fields")
        validator_for(tool_input_schema).check_schema(tool_input_schema)
        seen_inputs: set[str] = set()
        for field_name, source in mapping.items():
            if not isinstance(field_name, str) or not _FIELD_NAME.fullmatch(field_name):
                raise ValueError("input_mapping contains an invalid tool field name")
            if field_name not in properties:
                raise ValueError(f"input_mapping references undeclared tool field: {field_name}")
            property_schema = properties[field_name]
            if not isinstance(property_schema, dict) or property_schema.get("type") != "string":
                raise ValueError(
                    f"input_mapping field must be a declared string property: {field_name}"
                )
            if _SECRET_NAME.search(field_name) or (
                isinstance(property_schema, dict)
                and (
                    property_schema.get("writeOnly") is True
                    or property_schema.get("format") == "password"
                    or _SECRET_NAME.search(str(property_schema.get("format", "")))
                )
            ):
                raise ValueError(f"input_mapping cannot expose secret-like field: {field_name}")
            if not isinstance(source, str) or not source.startswith("/form/"):
                raise ValueError("input_mapping values must point to a declared /form/<id>")
            input_id = source.removeprefix("/form/")
            if input_id not in input_ids or "/" in input_id or "~" in input_id:
                raise ValueError(f"input_mapping references undeclared text input: {source}")
            if input_id in seen_inputs:
                raise ValueError("each text input may map to only one tool field")
            seen_inputs.add(input_id)
        missing_required = set(required) - set(mapping)
        if missing_required:
            raise ValueError(
                "interactive action must map every required tool field: "
                + ", ".join(sorted(str(value) for value in missing_required))
            )
    if input_ids != {value.removeprefix("/form/") for value in mapping.values()}:
        if input_ids or mapping:
            raise ValueError("every text input must map to exactly one tool field")
    return normalized


def render_mcp_app_resource(
    experience: dict[str, Any], *, tool_input_schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return a resources/read content item for a validated experience."""
    spec = validate_experience(experience, tool_input_schema=tool_input_schema)
    body = _render_html(spec)
    if len(body.encode("utf-8")) > MAX_HTML_BYTES:
        raise ValueError(f"rendered App exceeds {MAX_HTML_BYTES} bytes")
    return {
        "uri": spec["resource_uri"],
        "mimeType": MCP_APP_MIME_TYPE,
        "text": body,
        "_meta": {
            "ui": {
                "csp": {
                    "connectDomains": [],
                    "resourceDomains": [],
                    "frameDomains": [],
                    "baseUriDomains": [],
                },
                "permissions": {},
            }
        },
    }


def describe_mcp_app_resource(
    experience: dict[str, Any], *, tool_input_schema: dict[str, Any] | None = None
) -> dict[str, str]:
    """Return the resource descriptor suitable for a resources/list response."""
    spec = validate_experience(experience, tool_input_schema=tool_input_schema)
    return {
        "uri": spec["resource_uri"],
        "name": spec["id"],
        "description": spec["fallback"],
        "mimeType": MCP_APP_MIME_TYPE,
    }


def _validate_plain_text(value: str, label: str) -> None:
    if _URL_OR_ACTIVE_MARKUP.search(value):
        raise ValueError(f"{label} must not contain HTML, JavaScript, event attributes or URLs")


def _render_html(spec: dict[str, Any]) -> str:
    rendered_nodes: list[str] = []
    input_mapping = {
        source.removeprefix("/form/"): target for target, source in spec["input_mapping"].items()
    }
    has_action = False
    has_record = False
    for node in spec["components"]:
        kind = node["type"]
        if kind == "heading":
            rendered_nodes.append(f'<h2 class="mca-title">{html.escape(node["text"])}</h2>')
        elif kind == "text":
            rendered_nodes.append(f'<p class="mca-desc">{html.escape(node["text"])}</p>')
        elif kind == "text_input":
            field_id = html.escape(node["id"], quote=True)
            label = html.escape(node["label"])
            required = " required" if node.get("required", False) else ""
            maximum = int(node.get("max_length", 256))
            req_badge = '<span class="mca-req">*</span>' if node.get("required", False) else ""
            rendered_nodes.append(
                f'<div class="mca-field">'
                f'<label class="mca-label" for="{field_id}">{label}{req_badge}</label>'
                f'<input class="mca-input" id="{field_id}" name="{field_id}" type="text" '
                f'maxlength="{maximum}" autocomplete="off"{required}>'
                f"</div>"
            )
        elif kind == "select":
            field_id = html.escape(node["id"], quote=True)
            label = html.escape(node["label"])
            required = " required" if node.get("required", False) else ""
            req_badge = '<span class="mca-req">*</span>' if node.get("required", False) else ""
            opt_html = "".join(
                f'<option value="{html.escape(opt["value"], quote=True)}">'
                f"{html.escape(opt['label'])}</option>"
                for opt in node["options"]
            )
            rendered_nodes.append(
                f'<div class="mca-field">'
                f'<label class="mca-label" for="{field_id}">{label}{req_badge}</label>'
                f'<select class="mca-select" id="{field_id}" name="{field_id}"{required}>'
                f"{opt_html}</select>"
                f"</div>"
            )
        elif kind == "button":
            has_action = True
            rendered_nodes.append(
                f'<div class="mca-actions"><button class="mca-btn" id="mca-submit" type="submit">'
                f"{html.escape(node['label'])}</button></div>"
            )
        elif kind == "record":
            has_record = True
            rendered_nodes.append(
                '<div id="mca-result" class="mca-result" aria-live="polite"></div>'
            )
    if has_action and not has_record:
        rendered_nodes.append('<div id="mca-result" class="mca-result" aria-live="polite"></div>')
    fallback = html.escape(spec["fallback"])
    form_start = (
        '<form id="mca-form" class="mca-form">'
        if has_action
        else '<section id="mca-view" class="mca-form">'
    )
    form_end = "</form>" if has_action else "</section>"
    config = {
        "tool": spec["tool"],
        "inputMap": input_mapping,
        "fallback": spec["fallback"],
        "hasAction": has_action,
    }
    config_json = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
    config_json = (
        config_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(spec['id'])}</title>"
        f"<style>{_PRODUCT_CSS}</style></head><body>"
        '<main class="mca-card">'
        f"{form_start}{''.join(rendered_nodes)}{form_end}"
        f'<p id="mca-status" class="mca-status" role="status" aria-live="polite">{fallback}</p>'
        "</main>"
        '<script type="application/json" id="mca-config">'
        f"{config_json}</script><script>{_PRODUCT_BRIDGE_JS}</script></body></html>"
    )


_PRODUCT_CSS = (
    "*,*::before,*::after{box-sizing:border-box}"
    "body{margin:0;padding:14px;background:#fafafa;color:#09090b;"
    'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;'
    "font-size:13px;line-height:1.45}"
    ".mca-card{background:#fff;border:1px solid #e4e4e7;border-radius:10px;padding:18px;"
    "max-width:780px;margin:0 auto;box-shadow:0 1px 2px rgba(9,9,11,.04)}"
    ".mca-title{margin:0 0 4px;font-size:16px;font-weight:600;"
    "letter-spacing:-0.01em;color:#09090b}"
    ".mca-desc{margin:0 0 14px;font-size:13px;color:#52525b}"
    ".mca-field{display:flex;flex-direction:column;gap:5px;margin-bottom:12px}"
    ".mca-label{font-size:12px;font-weight:500;color:#27272a}"
    ".mca-req{color:#dc2626;margin-left:3px}"
    ".mca-input,.mca-select{width:100%;padding:8px 10px;border:1px solid #d4d4d8;"
    "border-radius:6px;font-size:13px;color:#09090b;background:#fff;outline:none;"
    "transition:border-color .15s,box-shadow .15s}"
    ".mca-input:focus,.mca-select:focus{border-color:#0f766e;"
    "box-shadow:0 0 0 3px rgba(15,118,110,.12)}"
    ".mca-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}"
    ".mca-btn{display:inline-flex;align-items:center;justify-content:center;padding:8px 14px;"
    "border:1px solid #0f766e;border-radius:6px;background:#0f766e;color:#fff;font-size:13px;"
    "font-weight:500;cursor:pointer;transition:background .15s}"
    ".mca-btn:hover:not(:disabled){background:#0d9488}"
    ".mca-btn:disabled{opacity:.6;cursor:not-allowed}"
    ".mca-result{margin-top:14px;display:flex;flex-direction:column;gap:10px}"
    ".mca-result:empty{display:none}"
    ".mca-badges{display:flex;flex-wrap:wrap;gap:6px}"
    ".mca-badge{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;"
    "border-radius:999px;font-size:11px;font-weight:600;border:1px solid #e4e4e7;"
    "background:#f4f4f5;color:#27272a}"
    ".mca-badge--ok{background:#f0fdfa;border-color:#99f6e4;color:#0f766e}"
    ".mca-badge--warn{background:#fffbeb;border-color:#fde68a;color:#b45309}"
    ".mca-badge--danger{background:#fef2f2;border-color:#fecaca;color:#dc2626}"
    ".mca-kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px}"
    ".mca-kv-item{background:#fafafa;border:1px solid #e4e4e7;border-radius:6px;"
    "padding:8px 10px;min-width:0}"
    ".mca-kv-k{font-size:10.5px;font-weight:600;text-transform:uppercase;"
    "letter-spacing:.03em;color:#71717a;margin-bottom:2px}"
    ".mca-kv-v{font-size:12.5px;font-weight:500;color:#09090b;word-break:break-word}"
    ".mca-kv-v--num{font-variant-numeric:tabular-nums;"
    "font-family:ui-monospace,SFMono-Regular,Menlo,monospace}"
    ".mca-section-label{font-size:11.5px;font-weight:600;color:#27272a;margin-bottom:4px}"
    ".mca-table-wrap{overflow-x:auto;border:1px solid #e4e4e7;border-radius:6px;background:#fff}"
    ".mca-table{width:100%;border-collapse:collapse;font-size:12px;text-align:left}"
    ".mca-table th{background:#fafafa;color:#52525b;font-weight:600;padding:7px 10px;"
    "border-bottom:1px solid #e4e4e7;white-space:nowrap}"
    ".mca-table td{padding:7px 10px;border-bottom:1px solid #f4f4f5;color:#09090b;"
    "vertical-align:top}"
    ".mca-table tr:last-child td{border-bottom:none}"
    ".mca-raw{border-top:1px solid #e4e4e7;padding-top:8px;margin-top:2px;font-size:11.5px;"
    "color:#71717a}"
    ".mca-raw summary{cursor:pointer;font-weight:500;user-select:none}"
    ".mca-json{margin:8px 0 0;padding:10px;border-radius:6px;border:1px solid #e4e4e7;"
    "background:#f4f4f5;color:#09090b;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
    "font-size:11px;line-height:1.45;max-height:220px;overflow:auto;white-space:pre-wrap;"
    "word-break:break-word}"
    ".mca-status{margin:14px 0 0;padding:8px 10px;border-radius:6px;background:#f4f4f5;"
    "border:1px solid #e4e4e7;font-size:12px;color:#52525b}"
    '.mca-status[data-tone="ok"]{background:#f0fdfa;border-color:#99f6e4;color:#0f766e}'
    '.mca-status[data-tone="error"]{background:#fef2f2;border-color:#fecaca;color:#dc2626}'
)


_PRODUCT_BRIDGE_JS = r"""(() => {
  'use strict';
  const config = JSON.parse(document.getElementById('mca-config').textContent);
  const status = document.getElementById('mca-status');
  const resultNode = document.getElementById('mca-result');
  let nextId = 1;
  let hostCapabilities = {};
  let toolInput = null;
  const pending = new Map();
  const setStatus = (value, tone) => {
    status.textContent = String(value).slice(0, 2000);
    if (tone) status.dataset.tone = tone;
    else delete status.dataset.tone;
  };
  const BADGE_KEYS = new Set([
    'status', 'priority', 'severity', 'state', 'region', 'tier', 'sla_breached'
  ]);
  const badgeTone = (val) => {
    const s = String(val).toLowerCase();
    if (['resolved', 'ok', 'succeeded', 'low', 'false'].includes(s)) return 'mca-badge--ok';
    if (['critical', 'escalated', 'failed', 'error', 'true'].includes(s)) {
      return 'mca-badge--danger';
    }
    if (['open', 'in_progress', 'high', 'medium', 'pending'].includes(s)) return 'mca-badge--warn';
    return '';
  };
  const labelize = (k) => String(k).replace(/_/g, ' ');
  const buildTable = (rows) => {
    const firstObj = rows.find((r) => r && typeof r === 'object' && !Array.isArray(r));
    if (!firstObj) {
      const p = document.createElement('p');
      p.className = 'mca-desc';
      p.textContent = rows.slice(0, 12).map((item) => String(item)).join(', ');
      return p;
    }
    const cols = Object.keys(firstObj).slice(0, 6);
    const wrap = document.createElement('div');
    wrap.className = 'mca-table-wrap';
    const table = document.createElement('table');
    table.className = 'mca-table';
    const thead = document.createElement('thead');
    const hr = document.createElement('tr');
    for (const c of cols) {
      const th = document.createElement('th');
      th.textContent = labelize(c);
      hr.appendChild(th);
    }
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    for (const row of rows.slice(0, 20)) {
      const tr = document.createElement('tr');
      for (const c of cols) {
        const td = document.createElement('td');
        const cell = row && typeof row === 'object' ? row[c] : '';
        if (cell === null || cell === undefined) {
          td.textContent = '-';
        } else {
          td.textContent = typeof cell === 'object' ? JSON.stringify(cell) : String(cell);
        }
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  };
  const showResult = (value) => {
    if (!resultNode) return;
    while (resultNode.firstChild) resultNode.removeChild(resultNode.firstChild);
    if (value === null || value === undefined) return;
    const isEnvelope =
      value &&
      typeof value === 'object' &&
      !Array.isArray(value) &&
      value.structuredContent &&
      typeof value.structuredContent === 'object';
    const payload = isEnvelope ? value.structuredContent : value;
    if (Array.isArray(payload)) {
      resultNode.appendChild(buildTable(payload));
    } else if (payload && typeof payload === 'object') {
      const badgeEntries = [];
      const scalarEntries = [];
      const arrayEntries = [];
      const nestedEntries = [];
      for (const [k, v] of Object.entries(payload)) {
        if (BADGE_KEYS.has(k) && (typeof v === 'string' || typeof v === 'boolean')) {
          badgeEntries.push([k, v]);
        }
        else if (Array.isArray(v)) arrayEntries.push([k, v]);
        else if (v !== null && typeof v === 'object') nestedEntries.push([k, v]);
        else scalarEntries.push([k, v]);
      }
      if (badgeEntries.length) {
        const row = document.createElement('div');
        row.className = 'mca-badges';
        for (const [k, v] of badgeEntries) {
          const span = document.createElement('span');
          span.className = ('mca-badge ' + badgeTone(v)).trim();
          span.textContent = labelize(k) + ': ' + String(v);
          row.appendChild(span);
        }
        resultNode.appendChild(row);
      }
      if (scalarEntries.length) {
        const grid = document.createElement('div');
        grid.className = 'mca-kv';
        for (const [k, v] of scalarEntries.slice(0, 18)) {
          const item = document.createElement('div');
          item.className = 'mca-kv-item';
          const keyEl = document.createElement('div');
          keyEl.className = 'mca-kv-k';
          keyEl.textContent = labelize(k);
          const valEl = document.createElement('div');
          valEl.className = typeof v === 'number' ? 'mca-kv-v mca-kv-v--num' : 'mca-kv-v';
          valEl.textContent = v === null || v === undefined ? '-' : String(v);
          item.appendChild(keyEl);
          item.appendChild(valEl);
          grid.appendChild(item);
        }
        resultNode.appendChild(grid);
      }
      for (const [k, arr] of arrayEntries.slice(0, 3)) {
        const sec = document.createElement('div');
        const lbl = document.createElement('div');
        lbl.className = 'mca-section-label';
        lbl.textContent = labelize(k) + ' (' + arr.length + ')';
        sec.appendChild(lbl);
        sec.appendChild(buildTable(arr));
        resultNode.appendChild(sec);
      }
      for (const [k, obj] of nestedEntries.slice(0, 3)) {
        const sec = document.createElement('div');
        const lbl = document.createElement('div');
        lbl.className = 'mca-section-label';
        lbl.textContent = labelize(k);
        sec.appendChild(lbl);
        const subGrid = document.createElement('div');
        subGrid.className = 'mca-kv';
        for (const [sk, sv] of Object.entries(obj).slice(0, 8)) {
          const item = document.createElement('div');
          item.className = 'mca-kv-item';
          const keyEl = document.createElement('div');
          keyEl.className = 'mca-kv-k';
          keyEl.textContent = labelize(sk);
          const valEl = document.createElement('div');
          valEl.className = typeof sv === 'number' ? 'mca-kv-v mca-kv-v--num' : 'mca-kv-v';
          valEl.textContent = typeof sv === 'object' ? JSON.stringify(sv) : String(sv);
          item.appendChild(keyEl);
          item.appendChild(valEl);
          subGrid.appendChild(item);
        }
        sec.appendChild(subGrid);
        resultNode.appendChild(sec);
      }
    }
    const details = document.createElement('details');
    details.className = 'mca-raw';
    const summary = document.createElement('summary');
    summary.textContent = 'Raw JSON payload';
    const pre = document.createElement('pre');
    pre.className = 'mca-json';
    let text;
    try { text = JSON.stringify(payload, null, 2); } catch (_) { text = String(payload); }
    pre.textContent = (text || '').slice(0, 12000);
    details.appendChild(summary);
    details.appendChild(pre);
    resultNode.appendChild(details);
  };
  const request = (method, params) => new Promise((resolve, reject) => {
    const id = nextId++;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error('Host request timed out'));
    }, 15000);
    pending.set(id, { resolve, reject, timer });
    window.parent.postMessage({ jsonrpc: '2.0', id, method, params }, '*');
  });
  window.addEventListener('message', (event) => {
    if (event.source !== window.parent || !event.data || typeof event.data !== 'object') return;
    const message = event.data;
    if (message.id !== undefined && pending.has(message.id)) {
      const item = pending.get(message.id);
      pending.delete(message.id);
      clearTimeout(item.timer);
      if (message.error) item.reject(new Error(message.error.message || 'Host request failed'));
      else item.resolve(message.result || {});
      return;
    }
    if (message.method === 'ui/notifications/tool-input') {
      toolInput = message.params && message.params.arguments;
      if (resultNode && toolInput && typeof toolInput === 'object') {
        const values = toolInput.arguments && typeof toolInput.arguments === 'object'
          ? toolInput.arguments : toolInput;
        for (const [inputId, fieldName] of Object.entries(config.inputMap)) {
          const input = document.getElementById(inputId);
          if (input && typeof values[fieldName] === 'string') {
            input.value = values[fieldName].slice(0, input.maxLength || 2000);
          }
        }
      }
    } else if (message.method === 'ui/notifications/tool-result') {
      showResult(message.params);
      setStatus('Host pushed latest tool result.', 'ok');
    } else if (message.method === 'ui/notifications/tool-cancelled') {
      setStatus('The host cancelled this tool call.', 'error');
    }
  });
  const initialize = async () => {
    try {
      const result = await request('ui/initialize', {
        protocolVersion: '2026-01-26',
        appInfo: { name: 'MCPcodeaway declarative experience', version: '1.0.0' },
        appCapabilities: { availableDisplayModes: ['inline'] }
      });
      hostCapabilities = result.hostCapabilities || {};
      window.parent.postMessage(
        { jsonrpc: '2.0', method: 'ui/notifications/initialized', params: {} }, '*');
      if (!hostCapabilities.serverTools && config.hasAction) {
        setStatus('This host does not support server tool calls. ' + config.fallback);
      }
    } catch (_) {
      setStatus('The App host is unavailable. ' + config.fallback);
    }
  };
  const form = document.getElementById('mca-form');
  if (form) form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!hostCapabilities.serverTools) {
      setStatus('The App host does not support server tool calls.', 'error');
      return;
    }
    const argumentsObject = {};
    for (const [inputId, fieldName] of Object.entries(config.inputMap)) {
      const input = document.getElementById(inputId);
      if (input) argumentsObject[fieldName] = input.value;
    }
    const button = document.getElementById('mca-submit');
    if (button) button.disabled = true;
    setStatus('Sending request through the MCP host…');
    try {
      const response = await request('tools/call', {
        name: config.tool,
        arguments: argumentsObject
      });
      showResult(response.structuredContent || response);
      setStatus(
        response.isError ? 'The tool returned an error.' : 'Tool call completed.',
        response.isError ? 'error' : 'ok'
      );
    } catch (error) {
      setStatus(String(error && error.message || 'Tool call failed.'), 'error');
    } finally {
      if (button) button.disabled = false;
    }
  });
  initialize();
})();"""
