"""New execution evidence survives legacy merge into the canonical ledger."""
import json

from plane_api.client import migrate_legacy_ledgers


def test_existing_native_recovery_and_cooldown_survive_legacy_merge(tmp_path):
    target = tmp_path / "canonical.json"
    legacy = tmp_path / "legacy.json"
    recoveries = {"recovered-key": {"status": "verified", "operation_id": "fixture-operation"}}
    cooldowns = {"fixture-provider": 1234567890.0}
    destination = {"receipts": {}, "native_recoveries": recoveries, "provider_cooldowns": cooldowns}
    source_receipts = {"old-key": {"status": "attempt_started", "fingerprint": "fixture-hash"}}
    target.write_text(json.dumps(destination))
    legacy.write_text(json.dumps({"receipts": source_receipts}))
    assert migrate_legacy_ledgers(target, [legacy]) == target
    actual = json.loads(target.read_text())
    assert actual["native_recoveries"] == recoveries
    assert actual["provider_cooldowns"] == cooldowns
    assert actual["receipts"] == source_receipts
    before = actual
    migrate_legacy_ledgers(target, [legacy])
    assert json.loads(target.read_text()) == before
