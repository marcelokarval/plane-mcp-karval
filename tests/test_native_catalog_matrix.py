"""Full registry denominator: contract/preflight, NOT live-provider coverage."""
import re
from typing import Any
import pytest
from plane_api.client import PlaneClient, _request_body_available
from plane_api.registry import OPERATIONS, get_mutation_contract
from test_plane_client_failure_receipts import QueueTransport, FakeResponse, ISSUE

MUTATIONS = tuple(op for op in OPERATIONS if op.mutation)


def sample(schema):
    if schema.get('enum'):
        return schema['enum'][0]
    kind = schema.get('type', 'string')
    if isinstance(kind, list):
        kind = next(k for k in kind if k != 'null')
    return {'array': ['fixture-id'], 'object': {}, 'integer': 1,
            'number': 1, 'boolean': True}.get(kind, 'fixture-id')


def request_for(op):
    schema = (op.request_schema or {}).get('json_schema', {})
    params = {key: 'fixture-id' for key in re.findall(r'\{([^}]+)\}', op.path)}
    body = {field['name']: sample(schema.get('properties', {}).get(field['name'], {}))
            for field in op.request_schema.get('body', {}).get('parameters', [])
            if field.get('required')}
    selector = get_mutation_contract(op.action)['postcondition'].get('targets', {}).get('selector')
    if selector and selector.get('source') == 'payload':
        key = selector['field']
        body.setdefault(key, sample(schema.get('properties', {}).get(key, {})))
    return params, body


@pytest.mark.parametrize('op', OPERATIONS, ids=lambda op: op.action)
def test_every_operation_has_offline_descriptor(op):
    transport = QueueTransport([])
    client = PlaneClient({'base_url': 'https://plane.invalid'}, transport)
    params = {key: 'fixture-id' for key in re.findall(r'\{([^}]+)\}', op.path)}
    descriptor = client.operation_descriptor(op.action, path_params=params)
    assert descriptor['action'] == op.action
    assert descriptor['method'] == op.method
    assert not transport.calls


@pytest.mark.parametrize('op', MUTATIONS, ids=lambda op: op.action)
def test_every_mutation_accepts_registry_valid_preflight_and_rejects_retries(op):
    transport = QueueTransport([])
    client = PlaneClient({'base_url': 'https://plane.invalid'}, transport)
    params, body = request_for(op)
    args: dict[str, Any] = dict(path_params=params, payload=body, idempotency_key='matrix-key-00000001')
    proof = client.preflight_native_mutation(op.action, attempts=1, **args)
    assert proof['method'] == op.method
    for attempts in (0, 2):
        with pytest.raises(ValueError, match='exactly one'):
            client.preflight_native_mutation(op.action, attempts=attempts, **args)
    assert not transport.calls


def test_provider_binding_rejects_cross_instance_replay(tmp_path):
    ledger = tmp_path / 'ledger.json'
    transport = QueueTransport([FakeResponse(201, ISSUE), FakeResponse(200, ISSUE)])
    args: dict[str, Any] = dict(path_params={'workspace_slug': 'workspace', 'project_id': 'project'},
                payload={'name': ISSUE['name']}, idempotency_key='cross-instance-00001', ledger_path=ledger)
    PlaneClient({'base_url': 'https://first.invalid'}, transport).execute_native_mutation_action('issue__add_issue', **args)
    other = QueueTransport([FakeResponse(200, ISSUE)])
    with pytest.raises((PermissionError, ValueError), match='instance|provider'):
        PlaneClient({'base_url': 'https://second.invalid'}, other).execute_native_mutation_action('issue__add_issue', **args)
    assert not other.calls


@pytest.mark.parametrize('payload', [{'project_id': 'other-project'}, {'unregistered_field': 'x'}])
def test_payload_cannot_override_target_or_add_unregistered_fields(payload):
    transport = QueueTransport([])
    client = PlaneClient({'base_url': 'https://plane.invalid'}, transport)
    with pytest.raises(ValueError, match='payload'):
        client.preflight_native_mutation(
            'issue__update_issue_detail',
            path_params={'workspace_slug': 'workspace', 'project_id': 'project', 'resource_id': 'issue'},
            payload=payload, attempts=1, idempotency_key='payload-binding-00001')
    assert not transport.calls


def test_state_patch_requires_exact_fresh_get_readback(tmp_path):
    issue = {**ISSUE, 'state': 'done'}
    transport = QueueTransport([FakeResponse(200, issue), FakeResponse(200, issue)])
    receipt = PlaneClient({'base_url': 'https://plane.invalid'}, transport).execute_native_mutation_action(
        'issue__update_issue_detail',
        path_params={'workspace_slug': 'workspace', 'project_id': 'project', 'resource_id': ISSUE['id']},
        payload={'state': 'done'}, idempotency_key='state-exact-get-00001', ledger_path=tmp_path / 'ledger.json')
    assert [c[0] for c in transport.calls] == ['PATCH', 'GET']
    assert receipt['readback_verified'] is True


def test_state_patch_wrong_readback_is_not_verified(tmp_path):
    transport = QueueTransport([FakeResponse(200, {**ISSUE, 'state': 'done'}), FakeResponse(200, {**ISSUE, 'state': 'open'})])
    receipt = PlaneClient({'base_url': 'https://plane.invalid'}, transport).execute_native_mutation_action(
        'issue__update_issue_detail',
        path_params={'workspace_slug': 'workspace', 'project_id': 'project', 'resource_id': ISSUE['id']},
        payload={'state': 'done'}, idempotency_key='state-wrong-get-00001', ledger_path=tmp_path / 'ledger.json')
    assert receipt['readback_verified'] is False
    assert receipt['mutation_applied'] is True
