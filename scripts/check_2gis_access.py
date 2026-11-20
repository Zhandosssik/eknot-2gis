"""Bounded live check; credentials and request URLs are never printed."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app import db
from app.settings import ROOT, api_key


def main():
    key = api_key()
    if not key:
        print('TWOGIS_API_KEY is missing')
        return 1
    with db.connect() as con:
        rows = con.execute("SELECT source_id,name FROM source_objects WHERE source='2gis' AND object_type='building' ORDER BY source_id LIMIT 10").fetchall()
    if not rows:
        print('No saved buildings')
        return 1
    identifiers = ','.join(row['source_id'] for row in rows)
    checks = []
    with httpx.Client(timeout=25) as client:
        def request(label, endpoint, **params):
            try:
                response = client.get('https://catalog.api.2gis.com/3.0/'+endpoint,
                                      params={**params, 'key': key, 'locale': 'ru_KZ'})
                payload = response.json()
                # Defensive redaction also applies to error messages returned by the API.
                payload = json.loads(json.dumps(payload, ensure_ascii=False).replace(key, '[REDACTED]'))
                entry = {'check': label, 'http': response.status_code, 'payload': payload}
            except (httpx.RequestError, ValueError):
                entry = {'check': label, 'error': 'Network error or invalid JSON'}
            checks.append(entry)
            return entry.get('payload', {})

        structure_fields = [
            'material', 'apartments_count', 'porch_count', 'floor_type', 'gas_type',
            'year_of_construction', 'elevators_count', 'is_in_emergency_state', 'project_type']
        fields = ['items.floors']+['items.structure_info.'+f for f in structure_fields]
        data = request('building_characteristics', 'items/byid', id=identifiers,
                       fields=','.join(['items.address', 'items.point', 'items.adm_div']+fields))
        if data.get('meta', {}).get('code') != 200:
            request('floors_only', 'items/byid', id=identifiers, fields='items.floors')
            request('structure_only', 'items/byid', id=identifiers,
                    fields=','.join('items.structure_info.'+f for f in structure_fields))
        request('review_statistics', 'items/byid', id=identifiers, fields='items.reviews')
        organizations = request('organizations', 'items', building_id=rows[0]['source_id'],
                city_id='9570771978420226', type='branch', page_size=10, page=1,
                fields='items.address,items.point,items.rubrics,items.schedule')
        if organizations.get('meta', {}).get('code') == 404:
            request('organizations_sample', 'items', building_id=identifiers,
                    city_id='9570771978420226', type='branch', page_size=10, page=1,
                    fields='items.address,items.point,items.rubrics,items.schedule')

    report = {'checked_at': db.now(), 'buildings': [dict(row) for row in rows],
              'requests': len(checks), 'checks': checks}
    path = ROOT / 'data' / '2gis-access-check.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = []
    for check in checks:
        payload = check.get('payload', {})
        items = payload.get('result', {}).get('items', [])
        summary.append({'check': check['check'], 'http': check.get('http'),
                        'meta': payload.get('meta'), 'total': payload.get('result', {}).get('total'),
                        'items': [{k: item[k] for k in ('id', 'name', 'address', 'floors', 'structure_info',
                                                       'reviews', 'rubrics', 'schedule') if k in item}
                                  for item in items]})
    print(json.dumps({'requests': len(checks), 'checks': summary}, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
