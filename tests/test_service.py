import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.twogis import collect


def page(items, total):
    return httpx.Response(200, json={'meta': {'code': 200}, 'result': {'items': items, 'total': total}})


def building(identifier='b1'):
    return {'id': identifier, 'type': 'building', 'name': 'Дом',
            'address_name': 'Астана, улица, 1', 'point': {'lon': 71.4, 'lat': 51.1}}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATABASE_PATH': self.temp.name+'/test.sqlite3',
                              'TWOGIS_API_KEY': 'test-secret', 'TWOGIS_CITY_ID': 'city-astana',
                              'TWOGIS_EXTRA_FIELDS': '', 'TWOGIS_REQUEST_DELAY': '0'})
        self.env.start()
        db.init()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def job(self, identifier='test', **overrides):
        config = {'kind': 'buildings', 'query': 'жилой комплекс', 'building_id': None,
                  'max_pages': 1, 'max_requests': 3, **overrides}
        with db.connect() as con:
            con.execute("INSERT INTO jobs(id,source,status,config,created_at,updated_at) VALUES(?,'2gis','queued',?,?,?)",
                        (identifier, json.dumps(config), db.now(), db.now()))
        return identifier

    def test_resume_and_upsert_keep_history(self):
        job_id = self.job()
        seen = []
        def handler(request):
            seen.append(dict(request.url.params))
            return page([building('b'+request.url.params['page'])], 2)
        transport = httpx.MockTransport(handler)
        collect(job_id, transport)
        self.assertEqual(db.job(job_id)['status'], 'limited')
        self.assertEqual(db.job(job_id)['next_page'], 2)
        collect(job_id, transport)
        self.assertEqual(db.job(job_id)['status'], 'completed')
        self.assertEqual([p['page'] for p in seen], ['1', '2'])
        self.assertTrue(all(p['city_id'] == 'city-astana' for p in seen))
        collect(self.job('update'), httpx.MockTransport(lambda r: page([building('b1')], 1)))
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM source_objects').fetchone()[0], 2)
            self.assertEqual(con.execute('SELECT count(*) FROM snapshots').fetchone()[0], 3)

    def test_auth_failure_does_not_leak_key(self):
        job_id = self.job()
        collect(job_id, httpx.MockTransport(lambda r: httpx.Response(403, text='test-secret')))
        result = db.job(job_id)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('test-secret', json.dumps(result))
        self.assertEqual(result['next_page'], 1)

    def test_budget_includes_city_resolution(self):
        os.environ['TWOGIS_CITY_ID'] = ''
        job_id = self.job(max_requests=1)
        collect(job_id, httpx.MockTransport(lambda r: page([{'id': 'astana', 'type': 'adm_div.city', 'name': 'Астана'}], 1)))
        result = db.job(job_id)
        self.assertEqual(result['status'], 'limited')
        self.assertEqual(result['requests'], 1)
        self.assertEqual(result['city_id'], 'astana')
        self.assertEqual(result['next_page'], 1)

    def test_repeated_page_is_incomplete(self):
        job_id = self.job(max_pages=3)
        collect(job_id, httpx.MockTransport(lambda r: page([building()], 10)))
        self.assertEqual(db.job(job_id)['status'], 'failed')
        self.assertEqual(db.job(job_id)['next_page'], 2)

    def test_city_resolution_accepts_real_api_adm_div_subtype(self):
        os.environ['TWOGIS_CITY_ID'] = ''
        job_id = self.job()
        def handler(request):
            if request.url.params.get('type') == 'adm_div.city':
                return page([{'id': '9570771978420226', 'type': 'adm_div',
                              'subtype': 'city', 'name': 'Астана',
                              'adm_div': [{'type': 'country', 'name': 'Казахстан'}]}], 1)
            self.assertEqual(request.url.params['city_id'], '9570771978420226')
            return page([building()], 1)
        collect(job_id, httpx.MockTransport(handler))
        self.assertEqual(db.job(job_id)['status'], 'completed')
        self.assertEqual(db.job(job_id)['city_id'], '9570771978420226')

    def test_organizations_use_exact_building_filter(self):
        job_id = self.job(kind='organizations', building_id='123')
        def handler(request):
            self.assertEqual(request.url.params['building_id'], '123')
            self.assertEqual(request.url.params['type'], 'branch')
            self.assertNotIn('city_id', request.url.params)
            self.assertIn('items.reviews', request.url.params['fields'])
            return page([{'id': 'org1', 'type': 'branch', 'name': 'Аптека'}], 1)
        collect(job_id, httpx.MockTransport(handler))
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT building_id FROM source_objects').fetchone()[0], '123')

    def test_api_validation_missing_key_and_export(self):
        # Do not enter lifespan: executor must remain usable for other tests.
        client = TestClient(app)
        os.environ['TWOGIS_API_KEY'] = ''
        self.assertEqual(client.post('/api/2gis/jobs', json={}).status_code, 409)
        self.assertEqual(client.post('/api/2gis/jobs', json={'kind': 'organizations'}).status_code, 422)
        self.assertEqual(client.get('/api/jobs/unknown').status_code, 404)
        job_id = self.job()
        item = building()
        item['name'] = '=FORMULA()'
        db.save_page(job_id, 1, {'result': {'items': [item], 'total': 1}})
        self.assertEqual(client.get('/api/objects').json()[0]['attributes']['id'], 'b1')
        self.assertIn("'=FORMULA()", client.get('/api/export?format=csv').text)
        self.assertEqual(client.get('/api/export').json()[0]['source'], '2gis')

    def test_interrupted_jobs_can_be_recovered(self):
        job_id = self.job()
        db.update_job(job_id, status='running')
        db.init()
        self.assertEqual(db.job(job_id)['status'], 'interrupted')

    def test_only_twogis_is_exposed_with_legacy_source_data(self):
        job_id = self.job()
        db.save_page(job_id, 1, {'result': {'items': [building()], 'total': 1}})
        with db.connect() as con:
            con.execute("""INSERT INTO jobs(id,source,status,config,created_at,updated_at)
                           VALUES('legacy','krisha','limited','{}',?,?)""", (db.now(), db.now()))
            con.execute("""INSERT INTO source_objects(source,source_id,object_type,name,raw_json,collected_at)
                           VALUES('krisha','old','complex','Legacy complex','{}',?)""", (db.now(),))
        client = TestClient(app)
        self.assertEqual(client.get('/api/status').json()['objects'], 1)
        self.assertEqual([i['source'] for i in client.get('/api/objects').json()], ['2gis'])
        self.assertEqual([i['source'] for i in client.get('/api/export').json()], ['2gis'])
        self.assertNotIn('Legacy complex', client.get('/api/export?format=csv').text)
        self.assertEqual([j['id'] for j in client.get('/api/jobs').json()], [job_id])
        self.assertEqual(client.get('/api/jobs/legacy').status_code, 404)
        self.assertEqual(client.post('/api/jobs/legacy/resume').status_code, 404)
        self.assertEqual(client.post('/api/krisha/jobs', json={}).status_code, 404)
        self.assertEqual(client.get('/api/objects?source=krisha').status_code, 422)
        self.assertNotIn('/api/krisha/jobs', client.get('/openapi.json').json()['paths'])
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM source_objects WHERE source='krisha'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
