"""Fast deterministic pre-CI policy & integrity gate for agents and local commits."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import check_comments

_REQUIRED_GOVERNANCE_FILES = (
    "DESIGN.md",
    "AGENTS.md",
    "CLAUDE.md",
    "web/AGENTS.md",
)

_BARE_NOQA_RE = re.compile(r"#\s*noqa\b(?!:)", re.IGNORECASE)
_BARE_TYPE_IGNORE_RE = re.compile(r"#\s*type:\s*ignore\b(?!\[)")
_ESLINT_DISABLE_RE = re.compile(r"eslint-disable(?:-next-line|-line)?\b(?![^\n]*-- )")
_SECRET_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|"
    r"AKIA[0-9A-Z]{16}|sk-proj-[A-Za-z0-9]{20,}"
)


def _check_required_files(root: Path, errors: list[str]) -> None:
    for rel in _REQUIRED_GOVERNANCE_FILES:
        if not (root / rel).is_file():
            errors.append(f"Missing governance/architecture file: {rel}")


def _check_python_src(root: Path, errors: list[str]) -> None:
    src_dir = root / "src"
    class_methods: dict[str, set[str]] = {}
    trees: list[tuple[str, ast.AST]] = []
    for py_file in sorted(src_dir.rglob("*.py")):
        rel = py_file.relative_to(root).as_posix()
        text = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as exc:
            errors.append(f"Syntax error in {rel}: {exc}")
            continue
        trees.append((rel, tree))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = {
                    item.name
                    for item in node.body
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                }
                class_methods.setdefault(node.name, set()).update(methods)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "tests" or alias.name.startswith("tests."):
                        msg = f"Forbidden test import '{alias.name}' in {rel}:{node.lineno}"
                        errors.append(msg)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "tests" or mod.startswith("tests."):
                    errors.append(f"Forbidden test import '{mod}' in {rel}:{node.lineno}")

        for idx, line in enumerate(text.splitlines(), start=1):
            if _BARE_NOQA_RE.search(line):
                errors.append(f"Bare '# noqa' without rule code in {rel}:{idx}")
            if _BARE_TYPE_IGNORE_RE.search(line):
                errors.append(f"Bare '# type: ignore' without error code in {rel}:{idx}")

    attr_to_class = {
        "registry": "UpstreamRegistry",
        "catalog": "ToolCatalog",
        "dispatcher": "HostDispatcher",
        "validator": "OidcTokenValidator",
        "exchanger": "DelegatedTokenExchanger",
        "policy": "PolicyEvaluator",
        "admin_sessions": "AdminSessionManager",
        "alert_evaluator": "AlertEvaluator",
        "telemetry": "TelemetryRecorder",
    }
    for rel, tree in trees:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
            ):
                owner_attr = node.func.value.attr
                method_name = node.func.attr
                target_cls = attr_to_class.get(owner_attr)
                if (
                    target_cls
                    and target_cls in class_methods
                    and not method_name.startswith("_")
                    and method_name not in class_methods[target_cls]
                ):
                    errors.append(
                        f"Undefined method call '{owner_attr}.{method_name}()' on {target_cls} "
                        f"in {rel}:{node.lineno}"
                    )


def _check_web_and_secrets(root: Path, errors: list[str]) -> None:
    web_app = root / "web" / "app"
    forbidden_ui_tokens = (
        ("selectedRun.pii_redactions", "Use selectedRun.redactions_count instead"),
        ("inspection.upstream", "Use inspection.name instead"),
    )
    if web_app.is_dir():
        for ui_file in sorted(web_app.rglob("*")):
            if ui_file.suffix in {".vue", ".ts", ".js", ".css"} and ui_file.is_file():
                rel = ui_file.relative_to(root).as_posix()
                text = ui_file.read_text(encoding="utf-8")
                for idx, line in enumerate(text.splitlines(), start=1):
                    if _ESLINT_DISABLE_RE.search(line):
                        errors.append(f"Unjustified eslint-disable in {rel}:{idx}")
                    for bad_tok, hint in forbidden_ui_tokens:
                        if bad_tok in line:
                            errors.append(
                                f"Mismatched backend property '{bad_tok}' in {rel}:{idx} ({hint})"
                            )

    for scan_dir in ("src", "web/app", "deploy", "scripts", "benchmarks"):
        base = root / scan_dir
        if not base.is_dir():
            continue
        for item in sorted(base.rglob("*")):
            if not item.is_file() or item.suffix not in {
                ".py",
                ".ts",
                ".js",
                ".vue",
                ".sh",
                ".yml",
                ".yaml",
                ".conf",
                ".json",
            }:
                continue
            rel = item.relative_to(root).as_posix()
            if rel == "scripts/agent_preflight.py":
                continue
            text = item.read_text(encoding="utf-8", errors="replace")
            match = _SECRET_RE.search(text)
            if match:
                errors.append(f"Potential hardcoded secret/private-key material in {rel}")


def _check_qa_contracts(root: Path, errors: list[str]) -> None:
    golden_path = root / "tests" / "golden" / "flows.json"
    if not golden_path.is_file():
        errors.append("Missing golden dataset: tests/golden/flows.json")
    else:
        try:
            golden = json.loads(golden_path.read_text(encoding="utf-8"))
            if not isinstance(golden.get("flows"), list) or not golden["flows"]:
                errors.append("tests/golden/flows.json must define a non-empty 'flows' list")
        except Exception as exc:
            errors.append(f"Invalid tests/golden/flows.json: {exc}")

    thresh_path = root / "benchmarks" / "thresholds.json"
    if not thresh_path.is_file():
        errors.append("Missing benchmark threshold lockfile: benchmarks/thresholds.json")
    else:
        try:
            thresh = json.loads(thresh_path.read_text(encoding="utf-8"))
            if not thresh.get("scaling") or not thresh.get("scenarios"):
                errors.append("benchmarks/thresholds.json must define 'scaling' and 'scenarios'")
        except Exception as exc:
            errors.append(f"Invalid benchmarks/thresholds.json: {exc}")

    dockerfile = root / "Dockerfile"
    if not dockerfile.is_file():
        errors.append("Missing Dockerfile")
    else:
        df_text = dockerfile.read_text(encoding="utf-8")
        if "FROM base-runtime AS gateway" not in df_text:
            errors.append("Dockerfile missing barebones 'gateway' target")
        else:
            gw_block = df_text.split("FROM base-runtime AS gateway", 1)[1].split("FROM ", 1)[0]
            for forbidden in ("web-builder", "nodejs", "examples"):
                if forbidden in gw_block:
                    errors.append(
                        f"Barebones Dockerfile 'gateway' target must not include '{forbidden}'"
                    )


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    errors: list[str] = []
    errors.extend(check_comments.check(root))
    _check_required_files(root, errors)
    _check_python_src(root, errors)
    _check_web_and_secrets(root, errors)
    _check_qa_contracts(root, errors)

    if errors:
        print("agent_preflight FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(
        "agent_preflight OK: governance, comment ratchet, suppression, secrets, QA, Docker passed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
