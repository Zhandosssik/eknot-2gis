"""Bounded live check: key validity, apartment fields and city search."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from app.settings import ROOT, api_key
from app.db import now


def main():
    key = api_key()
    if not key:
        print('API key is not configured')
        return 1
    report = {'checked_at': now(), 'checks': []}
    fields = 'items.address,items.point,items.adm_div,items.floors,items.structure_info.apartments_count,items.structure_info.porch_count,items.structure_info.elevators_count,items.structure_info.year_of_construction,items.has_apartments_info'
    checks = [
        ('apartments', 'items/byid', {'id': '70030076177928867,70030076180207546,70030076191027893', 'fields': fields}),
        ('city_buildings', 'items', {'city_id': '9570771978420226', 'type': 'building', 'page_size': 10, 'page': 1, 'fields': fields}),
        ('city_buildings_page2', 'items', {'city_id': '9570771978420226', 'type': 'building', 'page_size': 10, 'page': 2, 'fields': fields}),
    ]
    with httpx.Client(timeout=25) as client:
        for label, endpoint, params in checks:
            try:
                response = client.get('https://catalog.api.2gis.com/3.0/' + endpoint, params={**params, 'key': key, 'locale': 'ru_KZ'})
                payload = json.loads(json.dumps(response.json(), ensure_ascii=False).replace(key, '[REDACTED]'))
                entry = {'check': label, 'http': response.status_code, 'payload': payload}
            except (httpx.RequestError, ValueError):
                entry = {'check': label, 'error': 'Network error or invalid JSON'}
            report['checks'].append(entry)
            payload = entry.get('payload', {})
            items = payload.get('result', {}).get('items', [])
            print(json.dumps({'check': label, 'http': entry.get('http'), 'error': entry.get('error'), 'meta': payload.get('meta'), 'total': payload.get('result', {}).get('total'), 'items': [{k: item[k] for k in ('id', 'name', 'floors', 'structure_info', 'has_apartments_info') if k in item} for item in items]}, ensure_ascii=True))
            if entry.get('error') or entry.get('http') in (401, 403, 429) or payload.get('meta', {}).get('code') in (401, 403, 429):
                break
    path = ROOT / 'data' / ('2gis-city-probe-' + report['checked_at'].replace(':', '-') + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Report: ' + str(path))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
