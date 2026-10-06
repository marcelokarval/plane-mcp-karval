import json

from plane_api import resilience
from test_resilient_mutations import ACTION, Transport, setup, persisted


def test_recovery_history_ring_never_erases_uncertain_write_proof(tmp_path):
    transport = Transport(lost=True)
    client, kwargs = setup(tmp_path, transport)
    client.execute_native_mutation_action(ACTION, **kwargs)
    original = persisted(kwargs)["uncertain_write_attempts"]
    assert original[0]["phase"] == "write:" + ACTION
    assert original[0]["delivery"] == "uncertain"
    assert original[0]["active_authorization_id"] is None
    transport.pages = [
        {"results": [], "next_page_results": True, "next_cursor": str(index + 1)}
        for index in range(33)
    ] + [{"results": [], "next_page_results": False}]
    client.reconcile_native_mutation(ACTION, **kwargs)
    receipt = persisted(kwargs)
    assert receipt["recovery_evidence"]["complete"] is True
    assert receipt["recovery_evidence"]["pages"] == 34
    assert len(receipt["attempt_history"]) == 32
    assert all(entry["phase"] == "recovery:correlation" for entry in receipt["attempt_history"])
    assert receipt["uncertain_write_attempts"] == original
    auth = resilience.authorize_reattempt(client, ACTION, **kwargs,
        operator_acknowledged_duplicate_risk=True, reason="operator accepts duplicate risk")
    receipt = persisted(kwargs)
    assert receipt["uncertain_attempts"][0]["uncertain_write_attempts"] == original
    client.execute_native_mutation_action(ACTION, **kwargs)
    receipt = persisted(kwargs)
    assert receipt["uncertain_write_attempts"][:1] == original
    assert receipt["uncertain_write_attempts"][1]["active_authorization_id"] == auth["authorization_id"]
    assert receipt["uncertain_write_attempts"][1]["delivery"] == "uncertain"
    assert "SECRET" not in json.dumps(receipt)
    assert transport.calls.count("POST") == 2
