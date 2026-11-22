"""Safely review or explicitly delete legacy gateway_secrets metadata.

Values are intentionally not decrypted or displayed: the table stores arbitrary
encrypted strings and cannot reliably distinguish OAuth material from other
credentials based on ciphertext or names.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from gateway.config import GatewaySettings
from gateway.state import PostgresAuditStore, SQLiteAuditStore


async def audit(settings: GatewaySettings, *, delete_name: str | None = None) -> int:
    store = (
        PostgresAuditStore(settings.postgres_dsn)
        if settings.postgres_dsn
        else SQLiteAuditStore(settings.sqlite_path)
    )
    try:
        if delete_name is not None:
            deleted = await store.delete_gateway_secret(delete_name)
            print(json.dumps({"deleted": deleted, "name": delete_name}, sort_keys=True))
            return 0 if deleted else 1
        rows = await store.list_gateway_secrets()
        report = [
            {
                "name": row["name"],
                "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
                "classification": "review_required_unclassified_encrypted_value",
            }
            for row in rows
        ]
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    finally:
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete-name", help="delete exactly one reviewed secret record")
    parser.add_argument(
        "--confirm-delete-name",
        help="must exactly match --delete-name before deletion is allowed",
    )
    args = parser.parse_args()
    if args.delete_name is None and args.confirm_delete_name is not None:
        parser.error("--confirm-delete-name requires --delete-name")
    if args.delete_name is not None and args.confirm_delete_name != args.delete_name:
        parser.error("confirmation name must exactly match --delete-name")
    settings = GatewaySettings()
    raise SystemExit(asyncio.run(audit(settings, delete_name=args.delete_name)))


if __name__ == "__main__":
    main()
