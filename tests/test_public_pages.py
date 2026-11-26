import asyncio
import tempfile
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from app.main import PublicPagesRequest, app
from app.public_pages import check_pages


class PublicPagesTests(unittest.TestCase):
    def check(self, handler, ids=None):
        with tempfile.TemporaryDirectory() as directory, patch('app.public_pages.time.sleep'):
            return check_pages(ids or ['123'], transport=httpx.MockTransport(handler), output_root=directory)

    def test_visible_fields_and_missing_values(self):
        report = self.check(lambda request: httpx.Response(200, text='<script>В доме 999 подъездов</script><div>Жилой дом</div><span>10 этажей</span><p>Материал стен</p><p>Кирпич</p>'))
        row = report['results'][0]
        self.assertEqual(row['fields'], {'floors': '10', 'entrances': None, 'material': 'Кирпич'})
        self.assertEqual(row['status'], 'found')

    def test_empty_page_is_not_success(self):
        row = self.check(lambda request: httpx.Response(200, text='Проверка браузера'))['results'][0]
        self.assertEqual(row['status'], 'not_found')

    def test_address_is_from_visible_page_without_other_hosts(self):
        def handler(request):
            self.assertEqual(request.url.host, '2gis.kz')
            return httpx.Response(200, text='<h1>Айналайын</h1><div>Улица Кайым Мухамедханов, 19</div><div>Нура район, Астана</div>Жилой дом 10 этажей')
        row = self.check(handler)['results'][0]
        self.assertEqual(row['name'], 'Айналайын')
        self.assertEqual(row['address'], 'Улица Кайым Мухамедханов, 19')
        self.assertEqual(row['district_city'], 'Нура район, Астана')
        self.assertNotIn('api_data', row)

    def test_rating_reviews_and_coordinates_from_page(self):
        html = '<meta property="og:image" content="https://example.com/image?center=71.384649%2C51.14113"><h1>Дом</h1><div>Улица, 19</div>Жилой дом 10 этажей 4.3 92 оценки Отзывы 37'
        row = self.check(lambda r: httpx.Response(200, text=html))['results'][0]
        self.assertEqual(row['rating'], 4.3)
        self.assertEqual(row['reviews'], 37)
        self.assertEqual(row['lon'], 71.384649)
        self.assertEqual(row['lat'], 51.14113)
        self.assertEqual(row['purpose'], 'Жилой дом')
        self.assertIsNone(row['apartments'])
        self.assertIsNone(row['elevators'])
        self.assertIsNone(row['year'])

    def test_access_limit_stops_remaining_requests(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429)
        rows = self.check(handler, ['123', '456'])['results']
        self.assertEqual(len(calls), 1)
        self.assertEqual([r['status'] for r in rows], ['failed', 'skipped'])

    def test_timeout_is_reported(self):
        def handler(request):
            raise httpx.ReadTimeout('timeout', request=request)
        self.assertEqual(self.check(handler)['results'][0]['status'], 'failed')

    def test_validation_and_schema(self):
        for ids in [[], ['../123'], ['1'] * 4]:
            with self.assertRaises(ValidationError):
                PublicPagesRequest(building_ids=ids)
        self.assertIn('/api/2gis/public-pages/check', app.openapi()['paths'])

    def test_browser_endpoint_returns_report_and_validates_ids(self):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                with patch('app.main.check_pages', return_value={'results': [{'source_id': '123', 'fields': {'floors': '10'}}]}) as check:
                    response = await client.post('/api/2gis/public-pages/check', json={'building_ids': ['123']})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['results'][0]['fields']['floors'], '10')
                    check.assert_called_once_with(['123'])
                response = await client.post('/api/2gis/public-pages/check', json={'building_ids': ['https://example.com']})
                self.assertEqual(response.status_code, 422)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
