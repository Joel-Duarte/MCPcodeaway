"""Safe metadata-only review for ambiguous legacy gateway secret records."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.config import GatewaySettings
from gateway.state import SQLiteAuditStore
from scripts.audit_gateway_secrets import audit


@pytest.mark.asyncio
async def test_audit_lists_ambiguous_rows_without_values_and_deletes_only_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = GatewaySettings(sqlite_path=str(tmp_path / "gateway.db"))
    store = SQLiteAuditStore(settings.sqlite_path)
    await store.init()
    await store.set_gateway_secret(
        "oauth-maybe", "ciphertext-must-not-print", updated_by="operator"
    )
    await store.close()

    assert await audit(settings) == 0
    output = capsys.readouterr().out
    rows = json.loads(output)
    assert rows[0]["name"] == "oauth-maybe"
    assert rows[0]["classification"] == "review_required_unclassified_encrypted_value"
    assert "ciphertext-must-not-print" not in output

    assert await audit(settings, delete_name="oauth-maybe") == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": True, "name": "oauth-maybe"}
    assert await audit(settings) == 0
    assert json.loads(capsys.readouterr().out) == []
