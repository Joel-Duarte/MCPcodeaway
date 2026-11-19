"""CLI for plug-and-play MCP/OpenAPI onboarding and Classic vs. Code Mode comparison."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from gateway.auth import DEV_PERSONAS
from gateway.comparator import audit_catalog_comparison, run_side_by_side_comparison
from gateway.config import GatewaySettings, UpstreamConfig
from gateway.server import GatewayRuntime
from gateway.upstream_clients import DiscoveredTool, UpstreamClientManager, inspect_upstream


def _parse_kv_list(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Expected KEY=VAL format, got '{item}'")
        k, v = item.split("=", 1)
        out[k] = v
    return out


async def _run_add_mcp(args: argparse.Namespace) -> int:
    openapi_spec = None
    if args.openapi_spec_file:
        openapi_spec = json.loads(Path(args.openapi_spec_file).read_text(encoding="utf-8"))

    cfg = UpstreamConfig(
        name=args.name,
        type=args.type,
        url=args.url,
        openapi_spec_url=args.openapi_spec_url,
        openapi_spec=openapi_spec,
        command=args.command,
        args=args.arg or [],
        env=_parse_kv_list(args.env or []),
        auth_type=args.auth_type,
        downstream_audience=args.downstream_audience,
        downstream_scope=args.downstream_scope,
        secret_env_var=args.secret_env_var,
        allowed_roles=args.allowed_roles or [],
    )

    if args.gateway_url:
        async with httpx.AsyncClient(timeout=20.0) as client:
            endpoint = "/admin/upstreams" if args.register else "/admin/upstreams/inspect"
            resp = await client.post(
                f"{args.gateway_url.rstrip('/')}{endpoint}",
                json=cfg.model_dump(exclude_none=True),
            )
            if resp.status_code >= 400:
                print(f"Gateway returned {resp.status_code}: {resp.text}", file=sys.stderr)
                return 1
            data = resp.json()
    else:
        settings = GatewaySettings()
        runtime = GatewayRuntime(settings=settings)
        await runtime.store.init()
        try:
            client_mgr = UpstreamClientManager()
            inspection = await inspect_upstream(cfg, client_mgr)
            discovered = [DiscoveredTool.model_validate(t) for t in inspection["tools"]]
            audit = audit_catalog_comparison(
                runtime.catalog,
                runtime.registry,
                candidate_name=cfg.name,
                candidate_tools=discovered,
            )
            if args.register:
                if not settings.dynamic_upstreams_permitted:
                    print(
                        "Dynamic registration is disabled in read-only/prod mode.",
                        file=sys.stderr,
                    )
                    return 1
                await runtime.store.save_dynamic_upstream(
                    cfg.name, cfg.model_dump(exclude_none=True)
                )
            data = {"inspection": inspection, "onboarding_audit": audit}
        finally:
            await runtime.shutdown()

    if args.export_yaml:
        print(data["inspection"]["yaml_snippet"])
        return 0

    print(json.dumps(data, indent=2, default=str))
    return 0


async def _run_compare(args: argparse.Namespace) -> int:
    if args.gateway_url:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{args.gateway_url.rstrip('/')}/compare/run",
                json={"scenario": args.scenario, "persona": args.persona},
            )
            resp.raise_for_status()
            print(json.dumps(resp.json(), indent=2, default=str))
            return 0

    settings = GatewaySettings()
    runtime = GatewayRuntime(settings=settings)
    await runtime.startup()
    try:
        assert runtime.dev_keys is not None
        token = runtime.dev_keys.issue_user_token(persona=args.persona)
        identity = await runtime.validator.validate_token(token)
        result = await run_side_by_side_comparison(runtime, identity, scenario=args.scenario)
        print(json.dumps(result, indent=2, default=str))
    finally:
        await runtime.shutdown()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway", description="MCPcodeaway gateway CLI")
    sub = parser.add_subparsers(dest="command_name", required=True)

    add_p = sub.add_parser("add-mcp", help="Inspect or register a new MCP or OpenAPI upstream")
    add_p.add_argument("--name", required=True, help="Lowercase slug name (e.g. github_mcp)")
    add_p.add_argument("--type", required=True, choices=["mcp_http", "mcp_stdio", "openapi"])
    add_p.add_argument("--url", default=None)
    add_p.add_argument("--openapi-spec-url", default=None)
    add_p.add_argument("--openapi-spec-file", default=None)
    add_p.add_argument("--command", default=None)
    add_p.add_argument("--arg", action="append", default=[])
    add_p.add_argument("--env", action="append", default=[])
    add_p.add_argument(
        "--auth-type",
        default="none",
        choices=["delegated_token", "bearer_secret", "api_key", "none"],
    )
    add_p.add_argument("--downstream-audience", default=None)
    add_p.add_argument("--downstream-scope", default=None)
    add_p.add_argument("--secret-env-var", default=None)
    add_p.add_argument("--allowed-roles", action="append", default=[])
    add_p.add_argument("--gateway-url", default=None, help="Target running gateway URL")
    add_p.add_argument("--register", action="store_true", help="Persist into gateway")
    add_p.add_argument("--export-yaml", action="store_true", help="Print upstreams.yaml snippet")

    cmp_p = sub.add_parser("compare", help="Run Classic MCP vs. Code Mode side-by-side comparison")
    cmp_p.add_argument(
        "--scenario",
        default="reconcile_overdue",
        choices=["reconcile_overdue", "trial_balance_audit", "single_lookup"],
    )
    cmp_p.add_argument("--persona", default="finance_analyst", choices=sorted(DEV_PERSONAS))
    cmp_p.add_argument("--gateway-url", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command_name == "add-mcp":
        return asyncio.run(_run_add_mcp(args))
    if args.command_name == "compare":
        return asyncio.run(_run_compare(args))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
