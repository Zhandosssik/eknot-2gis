import csv
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile
from xml.etree import ElementTree

import httpx
from fastapi.testclient import TestClient

from app import db
from app.krisha import collect, parse_complex, parse_catalog, save_complex, CollectionError
from app.main import app


def catalog(ids=('1', '2'), total=2, next_page=None, addresses=None, city='astana'):
    data = '<script>window.data = '+json.dumps({'search': {'ids': list(ids), 'nb': total}})+';</script>'
    for i in ids:
        address = f'<p class="complex-card__address">{addresses[i]}</p>' if addresses else ''
        data += f'<div class="complex-card" data-complex-id="{i}" data-complex-alias="{city}/house-{i}">{address}</div>'
    if next_page:
        data += f'<a class="paginator__btn" data-page="{next_page}"></a>'
    return data


def detail(identifier='1', apartments='503', address=None):
    return '<script>window.data = '+json.dumps({'complex': {
        'id': identifier, 'name': '=ЖК', 'nbTotal': 8,
        'address': address or ('Астана, Нура р-н, ул. Айтматова, 19' if identifier == '1' else 'Астана'),
        'map': {'lat': 51.1, 'lon': 71.4}, 'rating': {'value': 4.3, 'nbVotes': 3},
        'infrastructure': [
            {'name': 'Детские сады', 'places': [{'name': 'Isma Kids', 'distance': '64 м'}]},
            {'name': 'Аптеки', 'places': [{'name': 'Zerde'}]},
        ] if identifier == '1' else []},
        'appToken': 'do-not-store'})+''' ;</script>
        <dl class="complex-parameters__block"><dt data-name="count.of.apartments">Количество квартир</dt>
        <dd>'''+apartments+'''</dd></dl><div class="complex__sidebar-info">
        <span class="complex__sidebar-info-title">Застройщик</span>
        <p class="complex__sidebar-info-text">Компания</p></div>'''


class KrishaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATABASE_PATH': self.temp.name+'/test.sqlite3'})
        self.env.start()
        db.init()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def job(self, max_requests=2, max_pages=2, **options):
        with db.connect() as con:
            con.execute("INSERT INTO jobs(id,source,status,config,created_at,updated_at) VALUES('k','krisha','queued',?,?,?)",
                        (json.dumps({'max_pages': max_pages, 'max_requests': max_requests, **options}), db.now(), db.now()))
        return 'k'

    def test_almaty_district_collection_is_separate_from_legacy_astana(self):
        with db.connect() as con:
            con.execute("""INSERT INTO source_objects(source,source_id,object_type,name,address,raw_json,collected_at)
                           VALUES('krisha','legacy-card','complex','Старый ЖК','Астана, Алматы р-н','{}',?)""",
                        (db.now(),))
        addresses = {'a': 'Алматы, Турксибский р-н', 'b': 'Алматы, Алатауский р-н'}
        seen = []

        def handler(request):
            path = request.url.path
            seen.append(path)
            if path == '/complex/search/almaty/':
                return httpx.Response(200, text=catalog(ids=tuple(addresses), addresses=addresses, city='almaty'))
            identifier = path.rstrip('/').rsplit('-', 1)[-1]
            return httpx.Response(200, text=detail(identifier, address=addresses[identifier]))

        client = TestClient(app)
        with patch('app.main.executor.submit'):
            response = client.post('/api/krisha/jobs', json={'city': 'almaty', 'mode': 'districts'})
        self.assertEqual(response.status_code, 202)
        job = response.json()['job_id']
        collect(job, httpx.MockTransport(handler), delay=0)
        self.assertEqual(seen, ['/complex/search/almaty/', '/complex/show/almaty/house-b/'])
        self.assertEqual(client.get(f'/api/jobs/{job}').json()['current_district'], 'Турксибский')
        self.assertEqual(client.get('/api/jobs?source=krisha').json(), [])
        self.assertEqual(client.get('/api/jobs?source=krisha&city=almaty').json()[0]['id'], job)
        self.assertEqual(client.get('/api/krisha/districts?city=almaty').json(), [{'district': 'Алатауский', 'count': 1}])
        rows = client.get('/api/objects?source=krisha&city=almaty&district=Алатауский&search=ЖК').json()
        self.assertEqual([row['source_id'] for row in rows], ['b'])
        self.assertEqual(client.get('/api/objects/b?source=krisha').status_code, 404)
        self.assertEqual(client.get('/api/objects/b?source=krisha&city=almaty').json()['data']['city'], 'Алматы')
        self.assertEqual([row['source_id'] for row in client.get('/api/objects?source=krisha').json()], ['legacy-card'])
        exported = client.get('/api/export?source=krisha&city=almaty&format=csv&district=Алатауский')
        headers, row = csv.reader(io.StringIO(exported.content.decode('utf-8-sig')), delimiter=';')
        self.assertEqual(row[headers.index('ID ЖК')], "'b")
        self.assertEqual(row[headers.index('Город')], 'Алматы')
        with patch('app.main.executor.submit'):
            response = client.post(f'/api/jobs/{job}/resume?max_requests=2')
        self.assertEqual(response.status_code, 202)
        collect(job, httpx.MockTransport(handler), delay=0)
        self.assertEqual(seen[-1], '/complex/show/almaty/house-a/')
        self.assertEqual(db.job(job)['status'], 'completed')

    def test_resume_mid_page_no_refetch_and_export(self):
        seen = []
        def handler(r):
            seen.append(str(r.url))
            if '/search/' in r.url.path:
                if r.url.query:
                    return httpx.Response(301, headers={'Location': '/complex/search/astana/'})
                return httpx.Response(200, text=catalog())
            return httpx.Response(200, text=detail(r.url.path.rstrip('/').split('-')[-1]))
        transport = httpx.MockTransport(handler)
        job = self.job()
        collect(job, transport, delay=0)
        self.assertEqual(db.job(job)['status'], 'limited')
        self.assertEqual(db.job(job)['processed'], 1)
        db.init()
        collect(job, transport, delay=0)
        self.assertEqual(db.job(job)['status'], 'completed')
        self.assertEqual(db.job(job)['processed'], 2)
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[0], 'https://krisha.kz/complex/search/astana/')
        client = TestClient(app)
        rows = client.get('/api/objects?source=krisha').json()
        self.assertEqual(rows[0]['attributes']['apartments'], 503)
        self.assertEqual(rows[0]['attributes']['offers_count'], 8)
        self.assertNotIn('do-not-store', json.dumps(rows))
        self.assertEqual(client.get('/api/objects', params={'source': 'krisha', 'search': 'жк'}).json(), rows)
        self.assertEqual(client.get('/api/objects', params={'source': 'krisha', 'search': 'аСТАНА'}).json(), rows)
        self.assertEqual(client.get('/api/objects', params={'source': 'krisha', 'search': '%'}).json(), [])
        self.assertEqual(client.get('/api/objects').json(), [])
        self.assertEqual(client.get('/api/krisha/districts').json(), [
            {'district': 'Нура', 'count': 1}, {'district': None, 'count': 1},
        ])
        filtered = client.get('/api/objects', params={'source': 'krisha', 'district': 'нУРА', 'search': 'айтматова'}).json()
        self.assertEqual(filtered, rows[:1])
        self.assertEqual(filtered[0]['district'], 'Нура')
        self.assertEqual(client.get('/api/objects?source=krisha&district=').json(), rows[1:])
        csv_text = client.get('/api/export?source=krisha&format=csv').content.decode('utf-8-sig')
        self.assertIn("'=ЖК", csv_text)
        headers, first_row, second_row = csv.reader(io.StringIO(csv_text), delimiter=';')
        self.assertEqual(len(headers), 24)
        district_index = headers.index('Район')
        self.assertEqual(first_row[district_index], 'Нура')
        self.assertEqual(second_row[district_index], '')
        filtered_csv = client.get('/api/export', params={'source': 'krisha', 'format': 'csv', 'district': 'Нура'})
        self.assertEqual(list(csv.reader(io.StringIO(filtered_csv.content.decode('utf-8-sig')), delimiter=';')),
                         [headers, first_row])
        self.assertEqual(headers[-3:], ['Характеристика: Количество квартир', 'Ход строительства', 'Инфраструктура рядом'])
        self.assertEqual(first_row[-1], 'Детские сады: Isma Kids (64 м)\nАптеки: Zerde')
        self.assertEqual(second_row[-1], '')
        with ZipFile(io.BytesIO(client.get('/api/export?source=krisha&format=xlsx').content)) as z:
            for name in z.namelist():
                ElementTree.fromstring(z.read(name))
            sheet = ElementTree.fromstring(z.read('xl/worksheets/sheet1.xml'))
            self.assertEqual([cell.text for cell in sheet.findall('.//{*}row[@r="1"]//{*}t')], headers)
            infrastructure_cell = sheet.find('.//{*}c[@r="X2"]')
            self.assertEqual(infrastructure_cell.find('.//{*}t').text, first_row[-1])
            self.assertEqual(infrastructure_cell.get('s'), '1')
        self.assertEqual(client.get('/krisha').status_code, 200)

    def test_collect_districts_in_order_after_catalog_and_resume(self):
        addresses = {'a': 'Астана, Алматы р-н', 'b': 'Астана, Сарыарка р-н',
                     'c': 'Астана, Нура р-н', 'd': 'Астана, р-н Нура',
                     'e': 'Астана, район Есильский', 'f': 'Астана, ул. Туран',
                     'g': 'Астана, Есильский р-н'}
        cached_path = '/complex/show/astana/house-e/'
        save_complex('previous', 1, parse_complex(detail('e', address=addresses['e']), 'e', cached_path))
        seen = []

        def handler(request):
            if '/search/' in request.url.path:
                page = request.url.params.get('page', '1')
                seen.append('page-' + page)
                html = (catalog('abc', 7, 2, addresses) if page == '1'
                        else catalog('defg', 7, addresses=addresses))
            else:
                identifier = request.url.path.rstrip('/').split('-')[-1]
                seen.append(identifier)
                html = detail(identifier, address=addresses[identifier])
            return httpx.Response(200, text=html)

        job_id = self.job(max_pages=1, mode='districts')
        transport = httpx.MockTransport(handler)
        client = TestClient(app)
        status_url = f'/api/jobs/{job_id}'
        collect(job_id, transport, delay=0)
        self.assertEqual(seen, ['page-1'])
        self.assertEqual(client.get(status_url).json()['stage'], 'catalog')
        collect(job_id, transport, delay=0)
        self.assertEqual(seen, ['page-1', 'page-2', 'c'])
        db.update_job(job_id, status='running')
        db.init()
        job = client.get(status_url).json()
        self.assertEqual((job['status'], job['current_district'], job['district_processed']), ('interrupted', 'Нура', 1))
        with patch('app.main.executor.submit'):
            self.assertEqual(client.post(status_url + '/resume', json={}).status_code, 202)
        collect(job_id, transport, delay=0)
        self.assertEqual(seen, ['page-1', 'page-2', 'c', 'd'])
        job = client.get('/api/jobs?source=krisha').json()[0]
        self.assertEqual((job['status'], job['current_district'], job['can_resume']),
                         ('district_completed', 'Есильский', True))
        db.init()
        with patch('app.main.executor.submit'):
            self.assertEqual(client.post(status_url + '/resume', json={}).status_code, 202)
        collect(job_id, transport, delay=0)
        self.assertEqual(seen, ['page-1', 'page-2', 'c', 'd', 'g'])
        self.assertEqual(client.get(status_url).json()['current_district'], 'Алматы')
        collect(job_id, transport, delay=0)
        self.assertEqual(seen[-1], 'a')
        self.assertEqual(client.get(status_url).json()['current_district'], 'Сарыарка')
        collect(job_id, transport, delay=0)
        self.assertEqual(seen[-1], 'b')
        self.assertEqual(client.get(status_url).json()['current_district'], 'Район не указан')
        collect(job_id, transport, delay=0)
        self.assertEqual(seen, ['page-1', 'page-2', 'c', 'd', 'g', 'a', 'b', 'f'])
        job = client.get(status_url).json()
        self.assertEqual((job['status'], job['processed'], job['requests']), ('completed', 7, 8))

    def test_table_returns_entire_district_and_keeps_city_pagination(self):
        details = {'parameters': {'count.of.floors': '12 этажей', 'home.type': 'кирпичный'},
                   'apartments': 100, 'infrastructure': [{'name': 'Аптеки'}]}
        with db.connect() as con:
            con.executemany('''INSERT INTO source_objects(source,source_id,object_type,name,address,raw_json,collected_at)
                               VALUES('krisha',?,'complex',?,?,?,?)''',
                           [(f'{i:03}', 'Искомый ЖК' if i == 500 else f'ЖК {i}',
                             'Астана, Нура р-н' if i < 501 else 'Астана, Алматы р-н',
                             json.dumps(details), db.now()) for i in range(502)])
        client = TestClient(app)
        endpoint = '/api/objects'
        params = {'source': 'krisha', 'view': 'table', 'limit': 26}
        city_page = client.get(endpoint, params=params).json()
        self.assertEqual(len(city_page), 26)
        district = client.get(endpoint, params={**params, 'district': 'Нура'}).json()
        self.assertEqual(len(district), 501)
        self.assertTrue(all(row['district'] == 'Нура' for row in district))
        self.assertEqual((district[-1]['source_id'], district[-1]['floors'], district[-1]['home_type']),
                         ('500', '12 этажей', 'кирпичный'))
        self.assertNotIn('attributes', district[-1])
        found = client.get(endpoint, params={**params, 'district': 'Нура', 'search': 'искомый'}).json()
        self.assertEqual(found, district[-1:])
        self.assertEqual(client.get(endpoint + '/500', params={'source': 'krisha'}).json()['data'], details)

    def test_failure_preserves_pending_card(self):
        job = self.job(max_requests=10)
        collect(job, httpx.MockTransport(lambda r: httpx.Response(200, text=catalog()) if '/search/' in r.url.path else httpx.Response(429)), delay=0)
        self.assertEqual(db.job(job)['status'], 'failed')
        self.assertEqual(db.job(job)['next_page'], 1)
        self.assertEqual(db.job(job)['processed'], 0)
        self.assertEqual(db.job(job)['requests'], 2)

    def test_repeated_page_not_completed(self):
        job = self.job(max_requests=10)
        collect(job, httpx.MockTransport(lambda r: httpx.Response(200, text=catalog(total=4, next_page=2) if '/search/' in r.url.path else detail(r.url.path.rstrip('/').split('-')[-1]))), delay=0)
        self.assertEqual(db.job(job)['status'], 'failed')
        self.assertIn('повторяет', db.job(job)['error'])

    def test_catalog_uses_published_link_for_nonstandard_city_slug(self):
        html = catalog(('23529216',), 1, addresses={'23529216': 'Астана, Нура р-н'})
        html = html.replace('astana/house-23529216', 'asatana/galaxymars')
        with self.assertRaisesRegex(CollectionError, 'Страница 8'):
            parse_catalog(html, 8, index_districts=True)
        html = html.replace('</div>', '<a href="/complex/show/asatana/galaxymars/">Galaxy Mars</a></div>')
        result = parse_catalog(html, 8, index_districts=True)
        self.assertEqual(result['items'], [
            {'id': '23529216', 'path': '/complex/show/asatana/galaxymars/', 'district': 'Нура'},
        ])

    def test_catalog_uses_short_main_card_link(self):
        html = catalog(('15743133',), 1, addresses={'15743133': 'Астана, Алматы р-н'})
        html = html.replace('astana/house-15743133', 'tengricityjassyl')
        link = '<a class="complex-card__main-block" href="/complex/show/tengricityjassyl/">ЖК</a>'
        html = html.replace('</div>', link + '</div>')
        self.assertEqual(parse_catalog(html, 22, index_districts=True)['items'], [
            {'id': '15743133', 'path': '/complex/show/tengricityjassyl/', 'district': 'Алматы'},
        ])
        with self.assertRaisesRegex(CollectionError, 'Страница 22'):
            parse_catalog(html.replace('href="/', 'href="//example.com/'), 22, index_districts=True)

    def test_no_invented_apartment_count_or_false_success(self):
        r = parse_complex(detail(apartments='нет данных'), '1', '/complex/show/astana/house-1/')
        self.assertIsNone(r['apartments'])
        self.assertEqual(r['offers_count'], 8)
        with self.assertRaises(CollectionError):
            parse_catalog('<html>captcha</html>', 1)
        with self.assertRaises(CollectionError):
            parse_complex(detail('2'), '1', '/complex/show/astana/house-1/')

    def test_create_validation_and_duplicate_lock(self):
        client = TestClient(app)
        self.assertEqual(client.post('/api/krisha/jobs', json={'max_requests': 0}).status_code, 422)
        with patch('app.main.executor.submit') as submit:
            result = client.post('/api/krisha/jobs', json={})
            self.assertEqual(result.status_code, 202)
            self.assertEqual(submit.call_count, 1)
            self.assertEqual(client.post('/api/krisha/jobs', json={}).status_code, 409)

    def test_resume_updates_limits_without_resetting_checkpoint(self):
        job_id = self.job(max_pages=10, max_requests=20, mode='districts')
        db.update_job(job_id, status='limited', next_page=52, requests=54, total=1263)
        endpoint = f'/api/jobs/{job_id}/resume'
        client = TestClient(app)
        self.assertEqual(client.post(endpoint, params={'max_requests': 501}).status_code, 422)
        self.assertEqual(db.job(job_id)['status'], 'limited')

        with patch('app.main.executor.submit') as submit:
            response = client.post(endpoint, params={'max_pages': 60, 'max_requests': 100})
            self.assertEqual(response.status_code, 202)
            submit.assert_called_once_with(collect, job_id)
            job = db.job(job_id)
            self.assertEqual(job['config'], {'max_pages': 60, 'max_requests': 100, 'mode': 'districts'})
            self.assertEqual((job['status'], job['next_page'], job['requests'], job['total']), ('queued', 52, 54, 1263))
            db.update_job(job_id, status='limited')
            self.assertEqual(client.post(endpoint, json={}).status_code, 202)
            self.assertEqual(db.job(job_id)['config'], job['config'])


if __name__ == '__main__':
    unittest.main()
