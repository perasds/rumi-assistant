import pytest

from rumi_market.game.version import _public_result


def response(servers):
    return {'code': 0, 'data': {'version': '1.5.3', 'hotVersion': {'version': 71}, 'serverList': servers}}


def test_public_version_keeps_per_server_chat_endpoint_without_exposing_it_in_repr():
    result = _public_result(response([
        {'serverId': 2, 'chat_gate_url': 'wss://chat.example.invalid/stream'},
        {'serverId': '3', 'chat_gate_url': 'wss://other.example.invalid/chat'},
    ]))
    assert result.chat_gate_url_for(2) == 'wss://chat.example.invalid/stream'
    assert result.chat_gate_url_for(3) == 'wss://other.example.invalid/chat'
    assert result.chat_gate_url_for(4) is None
    assert 'example.invalid' not in repr(result)


@pytest.mark.parametrize('servers', [None, {}, 'private', [{'serverId': True, 'chat_gate_url': 'wss://chat.invalid'}],
    [{'serverId': 2, 'chat_gate_url': 'https://chat.invalid'}],
    [{'serverId': 2, 'chat_gate_url': 'wss://user:secret@chat.invalid'}],
    [{'serverId': 2, 'chat_gate_url': 'wss://chat.invalid/#fragment'}],
    [{'serverId': 2, 'chat_gate_url': 'wss://chat.invalid/a'}, {'serverId': 2, 'chat_gate_url': 'wss://chat.invalid/b'}],
])
def test_invalid_or_ambiguous_chat_metadata_only_disables_optional_chat(servers):
    result = _public_result(response(servers))
    assert result.version == '1.5.3' and result.hot_version == 71
    assert result.chat_gate_url_for(2) is None


def test_missing_optional_server_list_keeps_existing_login_version_contract():
    raw = response([])
    del raw['data']['serverList']
    assert _public_result(raw).chat_gate_url_for(2) is None
