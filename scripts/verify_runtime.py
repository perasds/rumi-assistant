"""Verify public resources and anonymous gates without attempting game login."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import httpx


def verify(base_url: str) -> dict:
    project = Path(__file__).resolve().parents[1]
    checks: dict[str, bool] = {}
    with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as client:
        health = client.get('/api/health').json()
        capabilities = client.get('/api/auth/capabilities').json()
        checks['service_live'] = health.get('service') == 'rumi-market'
        checks['auth_capabilities_consistent'] = (
            (health.get('game_integration_ready') is False
             and capabilities.get('integration_ready') is False
             and capabilities.get('methods') == [])
            or (health.get('game_integration_ready') is True
                and capabilities.get('integration_ready') is True
                and capabilities.get('methods') == ['account'])
        )
        session = client.get('/api/session')
        checks['anonymous_session'] = session.json().get('authenticated') is False
        # Inspect flags without printing or storing cookie values.
        checks['cookie_httponly'] = 'httponly' in session.headers.get('set-cookie', '').lower()
        catalog = client.get('/api/pets').json()
        checks['public_catalog'] = (
            catalog.get('total') == 306
            and catalog.get('named_count') == 246
            and catalog.get('image_count') == 297
            and len(catalog.get('items', [])) == 306
        )
        named = client.get('/api/pets', params={'query': '蜜桃'}).json()['items']
        checks['verified_name_query'] = (
            len(named) == 6
            and {item['pet_id']: item['name'] for item in named}
            == {'1000101': '蜜桃', '1000601': '粉龙蜜桃', '1000801': '小学生蜜桃',
                '1000902': '超人蜜桃', '1003401': '小蜜蜂蜜桃', '1003501': '小红帽蜜桃'}
            and all(item['name_verified'] is True for item in named)
        )
        avatar = client.get('/api/pets/1000101/image')
        checks['real_avatar_bytes'] = (
            avatar.status_code == 200
            and avatar.headers.get('content-type', '').startswith('image/png')
            and hashlib.sha256(avatar.content).digest()
            == hashlib.sha256((project / 'data/pets/images/1000101.png').read_bytes()).digest()
        )
        items = [item for item in json.loads((project / 'data/market/items.json').read_text(encoding='utf-8'))['items']
                 if item.get('image_path')]
        item_checks = []
        for item in items:
            icon = client.get(f"/api/items/{item['item_id']}/image")
            expected = hashlib.sha256((project / 'data/market' / item['image_path']).read_bytes()).hexdigest()
            item_checks.append(
                icon.status_code == 200
                and icon.headers.get('content-type', '').startswith('image/png')
                and hashlib.sha256(icon.content).hexdigest() == expected == item['image_sha256']
            )
        checks['verified_item_icon_bytes'] = (
            len(items) == 5
            and {item['item_id'] for item in items} == {str(value) for value in range(10031, 10036)}
            and all(item_checks)
        )
        checks['no_unauthenticated_history'] = client.get('/api/market/listings').status_code == 401
        refresh = client.post('/api/market/refresh', json={},
                              headers={'Origin': base_url.rstrip('/')})
        checks['same_origin_proxy_and_auth_gate'] = (
            refresh.status_code == 401
            and refresh.json().get('error', {}).get('code') == 'AUTH_REQUIRED'
        )
        rejected = client.post('/api/market/refresh', json={},
                               headers={'Origin': 'https://example.invalid'})
        checks['cross_origin_rejected'] = rejected.status_code == 403
    return {
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'base_url': base_url,
        'scope': 'public_resources_and_anonymous_access_gates_only',
        'performed_game_login': False,
        'checks': checks,
        'all_checks_passed': all(checks.values()),
        'game_integration_ready': health.get('game_integration_ready'),
        'pet_count': catalog.get('total'),
        'named_count': catalog.get('named_count'),
        'image_count': catalog.get('image_count'),
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:5173')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = verify(args.base_url)
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)
    raise SystemExit(0 if report['all_checks_passed'] else 1)
