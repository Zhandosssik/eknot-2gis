import csv
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import httpx
from fastapi.testclient import TestClient

from app import db, korter
from app.main import app


def html(store, payload):
    return '<script>window.INITIAL_STATE = ' + json.dumps({store: payload}) + ';</script>'


class KorterTests(unittest.TestCase):
    def test_district_filter_includes_older_cards_with_explicit_district_in_address(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'DATABASE_PATH': directory + '/test.sqlite3'}):
            db.init()
            with db.connect() as con:
                addresses = ['р. Сарайшық, ул. А 431', 'р-н Сарайшык, ул. А108', 'ул. Сарайшык, 3']
                con.executemany('''INSERT INTO source_objects(source,source_id,object_type,address,raw_json,collected_at)
                                   VALUES('korter',?,'complex',?,'{}',?)''',
                                [(str(i), address, db.now()) for i, address in enumerate(addresses)])
            client = TestClient(app)
            self.assertEqual(client.get('/api/korter/districts').json(), [
                {'district': 'Сарайшық', 'count': 2}, {'district': None, 'count': 1},
            ])
            query = {'source': 'korter', 'district': 'Сарайшык'}
            response = client.get('/api/export', params=query)
            self.assertEqual([row['attributes']['district'] for row in response.json()], ['Сарайшық', 'Сарайшық'])

    def test_district_collection_resumes_and_exports_source_values(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'DATABASE_PATH': directory + '/test.sqlite3'}):
            db.init()
            client = TestClient(app)
            seen = []
            districts = {1: 'Есильский', 2: 'Нура'}

            def respond(request):
                path = request.url.path
                seen.append(path)
                if path == '/новостройки-астаны':
                    return httpx.Response(200, text=html('buildingListingStore', {
                        'filtersStore': {'totalCount': 2},
                        'pagination': {'page': {'start': 1}, 'itemsPerPage': 20},
                        'buildings': [{'buildingId': key, 'url': f'/жк-{key}', 'subLocalityNominative': district}
                                      for key, district in districts.items()],
                    }))
                identifier = int(path[-1])
                return httpx.Response(200, text=html('buildingLandingStore', {
                    'buildingId': identifier,
                    'main': {'name': 'ЖК Мечта', 'mainGeoObjectNominative': 'Астана',
                             'subLocality': {'nominative': districts[identifier]}},
                    'attributes': {'flat': [{'name': 'Количество квартир', 'value': '939 (1 очередь)'}]},
                }))

            self.assertEqual(client.get('/korter').status_code, 200)
            with patch('app.main.executor.submit') as submit:
                response = client.post('/api/korter/jobs', json={'mode': 'districts', 'max_requests': 2})
            self.assertEqual(response.status_code, 202)
            identifier = response.json()['job_id']
            submit.assert_called_once_with(korter.collect, identifier)
            transport = httpx.MockTransport(respond)
            korter.collect(identifier, transport, delay=0)
            self.assertEqual(seen, ['/новостройки-астаны', '/жк-2'])
            progress = client.get('/api/jobs?source=korter').json()[0]
            self.assertEqual((progress['status'], progress['current_district']), ('district_completed', 'Есильский'))
            self.assertEqual(client.get('/api/jobs?source=krisha').json(), [])

            with patch('app.main.executor.submit'):
                self.assertEqual(client.post(f'/api/jobs/{identifier}/resume?max_requests=1').status_code, 202)
            korter.collect(identifier, transport, delay=0)
            self.assertEqual(seen, ['/новостройки-астаны', '/жк-2', '/жк-1'])
            self.assertEqual(db.job(identifier)['status'], 'completed')
            self.assertEqual(client.get('/api/objects?source=korter&city=almaty').json(), [])
            table = client.get('/api/objects', params={'source': 'korter', 'view': 'table', 'district': 'Нура',
                                                       'search': 'мечта'}).json()
            self.assertEqual([(row['source_id'], row['apartments']) for row in table], [('2', '939 (1 очередь)')])
            csv_response = client.get('/api/export', params={'source': 'korter', 'format': 'csv', 'district': 'Нура'})
            rows = list(csv.DictReader(io.StringIO(csv_response.content.decode('utf-8-sig')), delimiter=';'))
            self.assertEqual([row['Количество квартир (по Korter)'] for row in rows], ['939 (1 очередь)'])
            excel = client.get('/api/export?source=korter&format=xlsx')
            with ZipFile(io.BytesIO(excel.content)) as book:
                self.assertIn('ЖК Korter', book.read('xl/workbook.xml').decode('utf-8'))

    def test_catalog_rejects_cross_site_card(self):
        page = html('buildingListingStore', {
            'filtersStore': {'totalCount': 1}, 'pagination': {'page': {'start': 1}, 'itemsPerPage': 20},
            'buildings': [{'buildingId': 1, 'url': '//example.org/house'}],
        })
        with self.assertRaises(korter.CollectionError):
            korter.load_catalog(lambda path: page, 'almaty', 1, False)
