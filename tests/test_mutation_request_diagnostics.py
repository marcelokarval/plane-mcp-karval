"""Request preparation is bounded; errors never expose supplied values."""
from typing import Any
import json
import pytest
from plane_api.client import PlaneClient, PlaneResponseValidationError, _validate_json_schema
from test_plane_client_failure_receipts import QueueTransport, FakeResponse, ISSUE

PARAMS = {'workspace_slug': 'workspace', 'project_id': 'project'}
UNSET = {'start_date': None, 'target_date': None, 'estimate_point': None}


def test_create_omits_unset_schedule_fields_and_preserves_caller_and_key(tmp_path):
    transport = QueueTransport([FakeResponse(201, ISSUE), FakeResponse(200, ISSUE)])
    client = PlaneClient({'base_url': 'https://plane.invalid'}, transport)
    payload = {'name': ISSUE['name'], **UNSET}
    args: dict[str, Any] = dict(path_params=PARAMS, payload=payload, attempts=1,
                idempotency_key='unset-create-request-0001', ledger_path=tmp_path / 'ledger.json')
    result = client.execute_native_mutation_action('issue__add_issue', **args)
    assert result['readback_verified'] is True
    assert [call[0] for call in transport.calls] == ['POST', 'GET']
    sent = json.loads(transport.calls[0][3])
    assert not set(UNSET).intersection(sent)
    assert payload == {'name': ISSUE['name'], **UNSET}
    client.execute_native_mutation_action('issue__add_issue', **args)
    assert len(transport.calls) == 2
    with pytest.raises(ValueError, match='different mutation'):
        client.execute_native_mutation_action('issue__add_issue', **{**args, 'payload': {'name': ISSUE['name']}})
    assert len(transport.calls) == 2


@pytest.mark.parametrize('action,extra', [
    ('issue__add_issue', {'name': None}),
    ('issue__add_issue', {'priority': None}),
    ('issue__update_issue_detail', {'start_date': None}),
])
def test_non_allowlisted_nulls_still_rejected_before_io(action, extra, tmp_path):
    transport = QueueTransport([])
    client = PlaneClient({'base_url': 'https://plane.invalid'}, transport)
    params = {**PARAMS, **({'resource_id': 'issue'} if 'update' in action else {})}
    with pytest.raises(PlaneResponseValidationError, match='request'):
        client.execute_native_mutation_action(action, path_params=params,
            payload={'name': 'example', **extra}, idempotency_key='invalid-request-00001',
            ledger_path=tmp_path / 'ledger.json')
    assert not transport.calls
    assert not (tmp_path / 'ledger.json').exists()


def test_error_identifies_nested_field_expected_actual_without_value():
    secret = 'DO_NOT_EXPOSE_THIS_VALUE'
    schema = {'type': 'object', 'properties': {'items': {'type': 'array',
        'items': {'type': 'object', 'properties': {'enabled': {'type': 'boolean'}}}}}}
    with pytest.raises(PlaneResponseValidationError) as error:
        _validate_json_schema({'items': [{'enabled': secret}]}, schema, context='mutation request')
    message = str(error.value)
    assert 'request' in message and 'response JSON' not in message
    assert '$.items[0].enabled' in message
    assert 'boolean' in message and 'string' in message
    assert secret not in message


def test_response_validation_retains_context_and_null_is_not_globally_allowed():
    with pytest.raises(PlaneResponseValidationError, match='response'):
        _validate_json_schema(None, {'type': 'string'}, context='mutation response')
    _validate_json_schema(None, {'type': ['string', 'null']}, context='mutation response')


def test_public_mcp_create_prepares_payload_and_returns_verified_receipt(monkeypatch, tmp_path):
    import asyncio
    from fastmcp import Client
    from plane_mcp_karval import server

    transport = QueueTransport([FakeResponse(201, ISSUE), FakeResponse(200, ISSUE)])
    real_client = PlaneClient({'base_url': 'https://plane.invalid'}, transport)
    monkeypatch.setattr(server, '_client', lambda: real_client)
    monkeypatch.setenv('PLANE_MUTATION_LEDGER', str(tmp_path / 'ledger.json'))
    monkeypatch.setenv('PLANE_WORKSPACE_SLUG', 'workspace')

    async def exercise():
        async with Client(server.create_server()) as client:
            args = {'operation': 'issue__add_issue', 'path_params': PARAMS,
                    'payload': {'name': ISSUE['name'], **UNSET},
                    'idempotency_key': 'public-unset-create-0001', 'attempts': 1}
            first = (await client.call_tool('plane_mutation_action', args)).data
            assert first['status'] == 'verified'
            repeated = (await client.call_tool('plane_mutation_action', args)).data
            assert repeated['duplicate'] is True
    asyncio.run(exercise())
    assert [call[0] for call in transport.calls] == ['POST', 'GET']
    assert not set(UNSET).intersection(json.loads(transport.calls[0][3]))


def test_prepare_is_create_only_and_preserves_valid_values():
    from plane_api.client import _prepare_mutation_body
    body = {'start_date': '2026-10-05', 'target_date': '2026-10-06', 'estimate_point': '3'}
    assert _prepare_mutation_body('issue__add_issue', body) == body
    assert _prepare_mutation_body('issue__update_issue_detail', UNSET) == UNSET
    assert _prepare_mutation_body('issue__add_issue', {'priority': None}) == {'priority': None}
