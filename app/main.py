import csv
import io
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, model_validator

from app import db
from app.settings import ROOT, api_key
from app.public_export import export_csv, export_xlsx
from app.twogis import collect
from app.public_pages import check_pages
from app.krisha import collect as collect_krisha
from app.krisha import DEFAULT_CITY, KrishaCity, district_from_address, district_progress
from app.korter import collect as collect_korter
from app.korter import resolve_district as resolve_korter_district
from app.krisha_export import export as export_krisha

executor = ThreadPoolExecutor(max_workers=1)
launch_lock = threading.Lock()
public_pages_lock = threading.Lock()
RESUMABLE_JOB_STATUSES = ('limited', 'failed', 'interrupted')
KRISHA_RESUMABLE_JOB_STATUSES = (*RESUMABLE_JOB_STATUSES, 'district_completed')
KRISHA_CITY_FILTER = "coalesce(krisha_city, 'astana')=?"
Source = Literal['2gis', 'krisha', 'korter']
COMPLEX_COLLECTORS = {'krisha': collect_krisha, 'korter': collect_korter}


@asynccontextmanager
async def lifespan(app):
    db.init()
    yield
    executor.shutdown(wait=True)


app = FastAPI(title='Eknot — каталог недвижимости', version='0.4.0', lifespan=lifespan,
              description='Таблицы и управление сбором: [Krisha.kz](/krisha), [Korter.kz](/korter). '
              'Сбор данных через официальный API 2ГИС и публичные каталоги ЖК. '
              'Завершение задания означает завершение выбранной выдачи, а не сбор всего города. '
              'Сервис предназначен для локального запуска.')


class CollectionRequest(BaseModel):
    kind: Literal['buildings', 'organizations'] = 'buildings'
    query: str = Field(default='', max_length=500, description='Пустая строка — поиск зданий города без фильтра по названию. Полнота зависит от выдачи и ограничений API.')
    building_id: str | None = Field(default=None, pattern=r'^\d+$')
    max_pages: int = Field(default=2, ge=1, le=100)
    max_requests: int = Field(default=5, ge=1, le=200)

    @model_validator(mode='after')
    def check_building(self):
        if self.kind == 'organizations' and not self.building_id:
            raise ValueError('Для сбора организаций укажите building_id здания 2ГИС.')
        return self


class PublicPagesRequest(BaseModel):
    building_ids: list[Annotated[str, Field(pattern=r'^[0-9]{1,30}$')]] = Field(
        default=['70030076177928867', '70030076180207546', '70030076191027893'],
        min_length=1, max_length=3,
        description='От одного до трёх ID зданий из ссылки 2gis.kz/astana/geo/ID. По умолчанию — три проверенных дома.')


class KrishaRequest(BaseModel):
    city: KrishaCity = DEFAULT_CITY
    max_pages: int = Field(default=1, ge=1, le=200, description='Страниц каталога выбранного города за запуск.')
    max_requests: int = Field(default=15, ge=1, le=500, description='Общий лимит запросов каталога и карточек. Продолжение сохраняет очередь.')
    mode: Literal['catalog', 'districts'] = 'catalog'


def is_current_krisha_config(config):
    if not isinstance(config, dict) or set(config) - {'mode', 'city'} != {'max_pages', 'max_requests'}:
        return False
    try:
        KrishaRequest.model_validate(config)
    except ValueError:
        return False
    return True


def check_complex_launch(source):
    with db.connect() as con:
        active = con.execute("SELECT id FROM jobs WHERE source=? AND status IN ('queued','running') LIMIT 1", (source,)).fetchone()
    if active:
        label = 'Krisha' if source == 'krisha' else 'Korter'
        raise HTTPException(409, f'Сбор {label} уже запущен. Дождитесь завершения.')


@app.post('/api/krisha/jobs', status_code=202, tags=['ЖК Krisha.kz'], summary='Запустить сбор ЖК Астаны или Алматы')
def create_krisha_job(request: KrishaRequest):
    return create_complex_job(request, 'krisha')


@app.post('/api/korter/jobs', status_code=202, tags=['ЖК Korter.kz'], summary='Запустить сбор ЖК Астаны или Алматы')
def create_korter_job(request: KrishaRequest):
    return create_complex_job(request, 'korter')


def create_complex_job(request, source):
    with launch_lock:
        check_complex_launch(source)
        identifier, stamp = uuid.uuid4().hex, db.now()
        with db.connect() as con:
            con.execute("INSERT INTO jobs(id,source,status,config,created_at,updated_at) VALUES(?,?,'queued',?,?,?)",
                        (identifier, source, json.dumps(request.model_dump()), stamp, stamp))
        executor.submit(COMPLEX_COLLECTORS[source], identifier)
    return {'job_id': identifier, 'status_url': f'/api/jobs/{identifier}',
            'results_url': f'/api/objects?source={source}&city={request.city}',
            'export_url': f'/api/export?source={source}&city={request.city}&format=xlsx'}


@app.post('/api/2gis/public-pages/check', tags=['Проверка страниц 2ГИС'],
          summary='Получить данные с публичных страниц',
          description='Нажмите Try it out → Execute. Ключ не нужен. Проверка может занять около минуты. '
                      'Ответ содержит fields (значения), evidence (фрагменты страницы) и путь к сохранённому отчёту. '
                      'found — найдены некоторые поля; not_found — поля не найдены; failed — ошибка; skipped — пропущено.')
def check_public_pages(request: PublicPagesRequest):
    if not public_pages_lock.acquire(blocking=False):
        raise HTTPException(409, 'Проверка страниц уже выполняется. Дождитесь результата.')
    try:
        return check_pages(request.building_ids)
    finally:
        public_pages_lock.release()


@app.get('/api/2gis/public-pages/export', tags=['Проверка страниц 2ГИС'],
         summary='Скачать результаты проверки в Excel или CSV')
def export_public_pages(format: Literal['xlsx', 'csv'] = 'xlsx',
                        report: str | None = Query(None, pattern=r'^2gis-public-[0-9T.+-]+$')):
    if report:
        path = ROOT / 'data' / report / 'results.json'
    else:
        paths = sorted((ROOT / 'data').glob('2gis-public-*/results.json'))
        if not paths:
            raise HTTPException(404, 'Сначала выполните проверку публичных страниц.')
        path = paths[-1]
    if not path.is_file():
        raise HTTPException(404, 'Результат проверки не найден.')
    results = json.loads(path.read_text(encoding='utf-8'))
    if any('api_data' in row for row in results):
        raise HTTPException(410, 'Это старый смешанный отчёт. Выполните новую проверку страниц.')
    content = export_xlsx(results) if format == 'xlsx' else export_csv(results)
    media = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if format == 'xlsx' else 'text/csv'
    return Response(content, media_type=media,
                    headers={'Content-Disposition': f'attachment; filename="eknot-public-pages.{format}"'})


def check_launch():
    if not api_key():
        raise HTTPException(409, 'Скопируйте .env.example в .env и заполните TWOGIS_API_KEY. Перезапустите сервис.')
    with db.connect() as con:
        active = con.execute("SELECT id FROM jobs WHERE source='2gis' AND status IN ('queued','running') LIMIT 1").fetchone()
    if active:
        raise HTTPException(409, f'Уже выполняется задание {active[0]}. Дождитесь завершения.')


@app.get('/', include_in_schema=False)
def home():
    return RedirectResponse('/docs')


@app.get('/krisha', response_class=HTMLResponse, include_in_schema=False)
@app.get('/korter', response_class=HTMLResponse, include_in_schema=False)
def krisha_page():
    return (ROOT / 'app' / 'krisha.html').read_text(encoding='utf-8')


@app.get('/api/status')
def status():
    with db.connect() as con:
        count = con.execute("SELECT count(*) FROM source_objects WHERE source='2gis'").fetchone()[0]
        krisha_count = con.execute("SELECT count(*) FROM source_objects WHERE source='krisha'").fetchone()[0]
        korter_count = con.execute("SELECT count(*) FROM source_objects WHERE source='korter'").fetchone()[0]
    return {'service': 'ok', 'api_key_configured': bool(api_key()), 'objects': count,
            'scope': 'Поисковая выборка Астаны; полный обход города ещё не реализован.',
            'reviews': 'Тексты отзывов не подключены.', 'krisha_objects': krisha_count, 'korter_objects': korter_count}


@app.post('/api/2gis/jobs', status_code=202)
def create_job(request: CollectionRequest):
    with launch_lock:
        check_launch()
        identifier = uuid.uuid4().hex
        stamp = db.now()
        with db.connect() as con:
            con.execute('''INSERT INTO jobs(id,source,status,config,created_at,updated_at)
                           VALUES(?,'2gis','queued',?,?,?)''',
                        (identifier, json.dumps(request.model_dump(), ensure_ascii=False), stamp, stamp))
        executor.submit(collect, identifier)
    return {'job_id': identifier, 'status_url': f'/api/jobs/{identifier}'}


@app.get('/api/jobs')
def jobs(limit: int = Query(50, ge=1, le=200), source: Source = '2gis',
         city: KrishaCity = DEFAULT_CITY):
    condition, params = 'source=?', [source]
    if source in COMPLEX_COLLECTORS:
        condition += " AND coalesce(json_extract(config, '$.city'), 'astana')=?"
        params.append(city)
    with db.connect() as con:
        ids = con.execute(f'SELECT id FROM jobs WHERE {condition} ORDER BY created_at DESC LIMIT ?',
                          (*params, limit)).fetchall()
    results = [db.job(row[0]) for row in ids]
    if source in COMPLEX_COLLECTORS:
        for result in results:
            result['can_resume'] = (result['status'] in KRISHA_RESUMABLE_JOB_STATUSES
                                    and is_current_krisha_config(result['config']))
            if result['config'].get('mode') == 'districts':
                result.update(district_progress(result))
    return results


@app.get('/api/jobs/{job_id}')
def get_job(job_id: str):
    result = db.job(job_id)
    if result is None or result['source'] not in ('2gis', *COMPLEX_COLLECTORS):
        raise HTTPException(404, 'Задание не найдено')
    if result['source'] in COMPLEX_COLLECTORS and result['config'].get('mode') == 'districts':
        result.update(district_progress(result))
    return result


@app.post('/api/jobs/{job_id}/resume', status_code=202)
def resume(job_id: str,
           max_pages: Annotated[int | None, Query(ge=1, le=200, description='Новый лимит страниц каталога ЖК.')] = None,
           max_requests: Annotated[int | None, Query(ge=1, le=500, description='Новый лимит запросов каталога ЖК.')] = None):
    with launch_lock:
        result = get_job(job_id)
        source = result['source']
        resumable = KRISHA_RESUMABLE_JOB_STATUSES if source in COMPLEX_COLLECTORS else RESUMABLE_JOB_STATUSES
        if result['status'] not in resumable:
            raise HTTPException(409, 'Это задание не требует продолжения.')
        updates = {'status': 'queued', 'error': None}
        if source in COMPLEX_COLLECTORS:
            if not is_current_krisha_config(result['config']):
                raise HTTPException(409, 'Старое задание Krisha несовместимо. Создайте новое.')
            check_complex_launch(source)
            limits = {key: value for key, value in [('max_pages', max_pages), ('max_requests', max_requests)]
                      if value is not None}
            if limits:
                updates['config'] = json.dumps({**result['config'], **limits})
        else:
            check_launch()
        db.update_job(job_id, **updates)
        executor.submit(COMPLEX_COLLECTORS.get(source, collect), job_id)
    return {'job_id': job_id, 'message': 'Продолжение с сохранённой страницы. Лимит запросов выделен заново.'}


def complex_district_sql(con, source):
    if source == 'korter':
        con.create_function('korter_district', 2, resolve_korter_district, deterministic=True)
        return "korter_district(json_extract(raw_json, '$.district'), address)"
    con.create_function('krisha_district', 1, district_from_address, deterministic=True)
    return 'krisha_district(address)'


def district_filter(district, source):
    if source == 'korter':
        return resolve_korter_district(district, None)
    return district.strip().capitalize() or None


@app.get('/api/objects')
def objects(object_type: str | None = None, building_id: str | None = None,
            source: Source = '2gis',
            search: str | None = None, limit: int = Query(50, ge=1, le=500),
            offset: int = Query(0, ge=0), district: str | None = None, city: KrishaCity = DEFAULT_CITY,
            view: Literal['full', 'table'] = Query('full', description='Поля таблицы ЖК, при выборе района возвращаются все его строки.')):
    complexes = source in COMPLEX_COLLECTORS
    table_view = complexes and view == 'table'
    fields = '*'
    if table_view:
        fields = '''source_id, name, address,
                    json_extract(raw_json, '$.developer') AS developer,
                    json_extract(raw_json, '$.status') AS status,
                    json_extract(raw_json, '$.completion') AS completion,
                    json_extract(raw_json, '$.parameters."count.of.floors"') AS floors,
                    json_extract(raw_json, '$.parameters."home.type"') AS home_type,
                    json_extract(raw_json, '$.apartments') AS apartments,
                    json_extract(raw_json, '$.offers_count') AS offers_count,
                    json_extract(raw_json, '$.price_m2_from') AS price_m2_from'''
        if source == 'korter':
            fields += ", json_extract(raw_json, '$.sales_status') AS sales_status"
    conditions, values = ['source=?'], [source]
    if complexes:
        conditions.append(KRISHA_CITY_FILTER)
        values.append(city)
    for column, value in [('object_type', object_type), ('building_id', building_id)]:
        if value:
            conditions.append(column+'=?')
            values.append(value)
    if search:
        if complexes:
            conditions.append('(instr(casefold(name), ?) OR instr(casefold(address), ?))')
            values.extend([search.casefold()]*2)
        else:
            conditions.append('(name LIKE ? OR address LIKE ?)')
            values.extend(['%'+search+'%']*2)
    with db.connect() as con:
        if complexes:
            district_sql = complex_district_sql(con, source)
            fields += f', {district_sql} AS district'
            if district is not None:
                conditions.append(f'{district_sql} IS ?')
                values.append(district_filter(district, source))
        if search and complexes:
            con.create_function('casefold', 1, lambda value: value.casefold() if value else '')
        query = f'SELECT {fields} FROM source_objects WHERE ' + ' AND '.join(conditions) + ' ORDER BY source_id'
        if not table_view or district is None:
            query += ' LIMIT ? OFFSET ?'
            values.extend([limit, offset])
        rows = con.execute(query, values).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        if not table_view:
            item.pop('krisha_city')
            item['attributes'] = json.loads(item.pop('raw_json'))
            if source == 'korter':
                item['attributes']['district'] = item['district']
        result.append(item)
    return result


@app.get('/api/krisha/districts', tags=['ЖК Krisha.kz'])
def krisha_districts(city: KrishaCity = DEFAULT_CITY):
    return complex_districts('krisha', city)


@app.get('/api/korter/districts', tags=['ЖК Korter.kz'])
def korter_districts(city: KrishaCity = DEFAULT_CITY):
    return complex_districts('korter', city)


def complex_districts(source, city):
    with db.connect() as con:
        district_sql = complex_district_sql(con, source)
        rows = con.execute(f'''SELECT {district_sql} AS district, count(*) AS count
                               FROM source_objects WHERE source=? AND {KRISHA_CITY_FILTER}
                               GROUP BY district ORDER BY district IS NULL, district''', (source, city)).fetchall()
    return [dict(row) for row in rows]


@app.get('/api/objects/{source_id}')
def object_detail(source_id: str, source: Source = '2gis', city: KrishaCity = DEFAULT_CITY):
    query, values = 'SELECT raw_json,address,collected_at FROM source_objects WHERE source=? AND source_id=?', [source, source_id]
    if source in COMPLEX_COLLECTORS:
        query += ' AND ' + KRISHA_CITY_FILTER
        values.append(city)
    with db.connect() as con:
        row = con.execute(query, values).fetchone()
    if row is None:
        raise HTTPException(404, 'Объект не найден')
    data = json.loads(row['raw_json'])
    if source == 'korter':
        data['district'] = resolve_korter_district(data.get('district'), row['address'])
    return {'source': source, 'collected_at': row['collected_at'], 'data': data}


@app.get('/api/export')
def export(format: Literal['json', 'csv', 'xlsx'] = 'json', source: Source = '2gis',
           district: str | None = None, city: KrishaCity = DEFAULT_CITY):
    query, values = 'SELECT * FROM source_objects WHERE source=?', [source]
    complexes = source in COMPLEX_COLLECTORS
    if complexes:
        query += ' AND ' + KRISHA_CITY_FILTER
        values.append(city)
    with db.connect() as con:
        if complexes and district is not None:
            query += f' AND {complex_district_sql(con, source)} IS ?'
            values.append(district_filter(district, source))
        rows = [dict(row) for row in con.execute(query + ' ORDER BY source_id', values)]
    if format == 'json':
        for row in rows:
            row.pop('krisha_city')
            row['attributes'] = json.loads(row.pop('raw_json'))
            if source == 'korter':
                row['attributes']['district'] = resolve_korter_district(row['attributes'].get('district'), row['address'])
        content, media = json.dumps(rows, ensure_ascii=False, indent=2), 'application/json'
    elif complexes:
        records = [json.loads(row['raw_json']) for row in rows]
        for record, row in zip(records, rows):
            record['district'] = (district_from_address(row['address']) if source == 'krisha'
                                  else resolve_korter_district(record.get('district'), row['address']))
        content = export_krisha(records, format, source=source)
        media = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if format == 'xlsx' else 'text/csv'
    else:
        records = []
        for row in rows:
            raw = json.loads(row['raw_json'])
            structure = raw.get('structure_info') or {}
            reviews = raw.get('reviews') or {}
            point = raw.get('point') or {}
            districts = [d.get('name', '') for d in raw.get('adm_div', []) if d.get('type') == 'district']
            records.append({'source_id': row['source_id'], 'name': row['name'],
                'address': raw.get('full_address_name') or row['address'],
                'district_city': ', '.join(districts),
                'fields': {'floors': raw.get('floors'), 'material': structure.get('material'), 'entrances': structure.get('porch_count')},
                'lon': point.get('lon'), 'lat': point.get('lat'), 'purpose': raw.get('purpose_name'),
                'rating': reviews.get('general_rating'), 'reviews': reviews.get('general_review_count'),
                'apartments': structure.get('apartments_count'), 'elevators': structure.get('elevators_count'),
                'year': structure.get('year_of_construction')})
        content = export_xlsx(records) if format == 'xlsx' else export_csv(records)
        media = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if format == 'xlsx' else 'text/csv'
    name = f'eknot-{source}-{city}' if complexes and city != DEFAULT_CITY else f'eknot-{source}'
    return Response(content, media_type=media,
                    headers={'Content-Disposition': f'attachment; filename="{name}.{format}"'})
