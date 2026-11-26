import asyncio
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

import httpx

from app.main import app
from app.public_export import export_csv, export_xlsx


class PublicExportTests(unittest.TestCase):
    def setUp(self):
        self.results = [{'source_id': '70030076177928867', 'fields': {'floors': '10', 'material': 'Кирпич', 'entrances': None}, 'name': '=1+1', 'rating': 4.3}]

    def test_csv_unicode_missing_and_formula(self):
        content = export_csv(self.results).decode('utf-8-sig')
        rows = list(csv.reader(io.StringIO(content), delimiter=';'))
        self.assertEqual(rows[1][:4], ["'70030076177928867", '10', 'Кирпич', ''])
        self.assertEqual(rows[1][rows[0].index('Название')], "'=1+1")
        self.assertEqual(rows[1][rows[0].index('Рейтинг')], '4,3')

    def test_xlsx_types_and_missing_values(self):
        with ZipFile(io.BytesIO(export_xlsx(self.results))) as archive:
            for name in archive.namelist():
                ElementTree.fromstring(archive.read(name))
            sheet = ElementTree.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        ns = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        self.assertEqual(sheet.find('.//x:c[@r="A2"]', ns).get('t'), 'inlineStr')
        self.assertEqual(sheet.find('.//x:c[@r="B2"]/x:v', ns).text, '10')
        self.assertEqual(sheet.find('.//x:c[@r="K2"]/x:v', ns).text, '4.3')
        self.assertIsNone(sheet.find('.//x:c[@r="D2"]', ns))
        self.assertEqual(sheet.findall('.//x:f', ns), [])

    def test_download_existing_report_and_validation(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory, patch('app.main.ROOT', Path(directory)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    url = '/api/2gis/public-pages/export'
                    self.assertEqual((await client.get(url)).status_code, 404)
                    folder = Path(directory) / 'data' / '2gis-public-2026-11-23T01-00-00+00-00'
                    folder.mkdir(parents=True)
                    (folder / 'results.json').write_text(json.dumps(self.results), encoding='utf-8')
                    for fmt in ('xlsx', 'csv'):
                        response = await client.get(url, params={'format': fmt, 'report': folder.name})
                        self.assertEqual(response.status_code, 200)
                        self.assertIn('attachment;', response.headers['content-disposition'])
                        self.assertTrue(response.content.startswith(b'PK' if fmt == 'xlsx' else b'\xef\xbb\xbf'))
                    self.assertEqual((await client.get(url, params={'report': '../.env'})).status_code, 422)
                    mixed = [{**self.results[0], 'api_data': {'name': 'Old API name'}}]
                    (folder / 'results.json').write_text(json.dumps(mixed), encoding='utf-8')
                    self.assertEqual((await client.get(url)).status_code, 410)
        asyncio.run(run())
