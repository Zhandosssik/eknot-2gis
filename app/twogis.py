import os
import time
import json

import httpx

from app import db
from app.settings import api_key


class CollectionError(Exception):
    pass


class BudgetReached(CollectionError):
    pass


class PlacesClient:
    def __init__(self, job_id, max_requests, transport=None):
        self.job_id = job_id
        self.max_requests = max_requests
        self.client = httpx.Client(base_url='https://catalog.api.2gis.com/3.0/',
                                   timeout=30, transport=transport, follow_redirects=False)

    def close(self):
        self.client.close()

    def get(self, **params):
        if not api_key():
            raise CollectionError('Не задан TWOGIS_API_KEY в файле .env.')
        for attempt in range(3):
            count = db.job(self.job_id)['requests']
            if count >= self.max_requests:
                raise BudgetReached('Достигнут лимит запросов задания.')
            db.update_job(self.job_id, requests=count+1)
            time.sleep(max(0, float(os.getenv('TWOGIS_REQUEST_DELAY', '1'))))
            try:
                response = self.client.get('items', params={**params, 'key': api_key(), 'locale': 'ru_KZ'})
            except httpx.RequestError:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise CollectionError('Ошибка сети при обращении к 2ГИС.') from None
            # Never expose response URLs: they contain the API key.
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** (attempt+1))
                continue
            if response.status_code != 200:
                raise CollectionError(f'2ГИС вернул HTTP {response.status_code}. Проверьте ключ, права и лимиты.')
            try:
                payload = response.json()
                code = int(payload['meta']['code'])
            except (ValueError, KeyError, TypeError):
                raise CollectionError('Неожиданный формат ответа 2ГИС.') from None
            if code == 404:
                return {'meta': payload['meta'], 'result': {'items': [], 'total': 0}}
            if code != 200:
                raise CollectionError(f'Ошибка API 2ГИС: код {code}. Проверьте ключ и доступные поля.')
            result = payload.get('result')
            if not isinstance(result, dict) or not isinstance(result.get('items', []), list):
                raise CollectionError('В ответе 2ГИС отсутствует список объектов.')
            if any(not isinstance(i, dict) or not i.get('id') or not i.get('type')
                   for i in result.get('items', [])):
                raise CollectionError('Изменилась структура объектов 2ГИС.')
            return payload


def resolve_city(client):
    configured = os.getenv('TWOGIS_CITY_ID', '').strip()
    if configured:
        return configured
    payload = client.get(q='Астана Казахстан', type='adm_div.city', page_size=10,
                         fields='items.adm_div,items.point')
    candidates = [i for i in payload['result'].get('items', [])
                  if (i['type'] == 'adm_div.city' or
                      (i['type'] == 'adm_div' and i.get('subtype') == 'city')) and
                  i.get('name', '').strip().casefold() in {'астана', 'astana'}]
    if len(candidates) != 1:
        raise CollectionError('Не удалось однозначно определить Астану. Укажите проверенный TWOGIS_CITY_ID в .env.')
    return str(candidates[0]['id'])


def collect(job_id, transport=None):
    current = db.job(job_id)
    config = current['config']
    client = PlacesClient(job_id, current['requests'] + config['max_requests'], transport)
    db.update_job(job_id, status='running', error=None)
    try:
        city_id = current['city_id'] or resolve_city(client)
        db.update_job(job_id, city_id=city_id)
        fields = ['items.point', 'items.address', 'items.adm_div', 'items.reviews']
        if config['kind'] == 'organizations':
            fields += ['items.rubrics', 'items.schedule']
        fields += [f.strip() for f in os.getenv('TWOGIS_EXTRA_FIELDS', '').split(',') if f.strip()]
        start_page = current['next_page']
        for page in range(start_page, start_page + config['max_pages']):
            params = dict(page=page, page_size=10, fields=','.join(fields))
            if config['kind'] == 'buildings':
                params.update(type='building', q=config['query'], city_id=city_id)
            else:
                # The building ID already restricts the search geographically.
                # Combining city_id + building_id can return an empty result.
                params.update(type='branch', building_id=config['building_id'])
            payload = client.get(**params)
            items = payload['result'].get('items', [])
            state = db.job(job_id)
            total = payload['result'].get('total')
            if not items and isinstance(total, int) and state['processed'] < total:
                raise CollectionError('Выдача прервалась до получения всех результатов. Сбор неполный.')
            if items and page > 1:
                with db.connect() as con:
                    previous = con.execute('SELECT payload FROM snapshots WHERE job_id=? AND page=? ORDER BY id DESC LIMIT 1',
                                           (job_id, page-1)).fetchone()
                if previous:
                    previous_ids = {str(i['id']) for i in json.loads(previous[0])['result'].get('items', [])}
                    if {str(i['id']) for i in items} == previous_ids:
                        raise CollectionError('API повторяет предыдущую страницу. Сбор остановлен как неполный.')
            db.save_page(job_id, page, payload, config.get('building_id') if config['kind'] == 'organizations' else None)
            if not items or (isinstance(total, int) and db.job(job_id)['processed'] >= total):
                db.update_job(job_id, status='completed')
                return
        db.update_job(job_id, status='limited', error='Достигнут лимит страниц. Можно продолжить.')
    except BudgetReached as exc:
        db.update_job(job_id, status='limited', error=str(exc))
    except CollectionError as exc:
        db.update_job(job_id, status='failed', error=str(exc))
    except Exception:
        db.update_job(job_id, status='failed', error='Внутренняя ошибка обработки. Данные предыдущих страниц сохранены.')
    finally:
        client.close()
