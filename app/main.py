import csv
import io
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field, model_validator

from app import db
from app.settings import api_key
from app.twogis import collect

executor = ThreadPoolExecutor(max_workers=1)
launch_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app):
    db.init()
    yield
    executor.shutdown(wait=True)


app = FastAPI(title='Eknot — база домов Астаны', version='0.3.0', lifespan=lifespan,
              description='Сбор данных через официальный API 2ГИС. '
              'Завершение задания означает завершение выбранной выдачи, а не сбор всего города. '
              'Сервис предназначен для локального запуска.')


class CollectionRequest(BaseModel):
    kind: Literal['buildings', 'organizations'] = 'buildings'
    query: str = Field(default='жилой комплекс', min_length=1, max_length=500)
    building_id: str | None = Field(default=None, pattern=r'^\d+$')
    max_pages: int = Field(default=2, ge=1, le=100)
    max_requests: int = Field(default=5, ge=1, le=200)

    @model_validator(mode='after')
    def check_building(self):
        if self.kind == 'organizations' and not self.building_id:
            raise ValueError('Для сбора организаций укажите building_id здания 2ГИС.')
        return self


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


@app.get('/api/status')
def status():
    with db.connect() as con:
        count = con.execute("SELECT count(*) FROM source_objects WHERE source='2gis'").fetchone()[0]
    return {'service': 'ok', 'api_key_configured': bool(api_key()), 'objects': count,
            'scope': 'Поисковая выборка Астаны; полный обход города ещё не реализован.',
            'reviews': 'Тексты отзывов не подключены.'}


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
def jobs(limit: int = Query(50, ge=1, le=200)):
    with db.connect() as con:
        ids = con.execute("SELECT id FROM jobs WHERE source='2gis' ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [db.job(row[0]) for row in ids]


@app.get('/api/jobs/{job_id}')
def get_job(job_id: str):
    result = db.job(job_id)
    if result is None or result['source'] != '2gis':
        raise HTTPException(404, 'Задание не найдено')
    return result


@app.post('/api/jobs/{job_id}/resume', status_code=202)
def resume(job_id: str):
    with launch_lock:
        result = get_job(job_id)
        if result['status'] not in ('limited', 'failed', 'interrupted'):
            raise HTTPException(409, 'Это задание не требует продолжения.')
        check_launch()
        db.update_job(job_id, status='queued', error=None)
        executor.submit(collect, job_id)
    return {'job_id': job_id, 'message': 'Продолжение с сохранённой страницы. Лимит запросов выделен заново.'}


@app.get('/api/objects')
def objects(object_type: str | None = None, building_id: str | None = None,
            source: Literal['2gis'] = '2gis',
            search: str | None = None, limit: int = Query(50, ge=1, le=500),
            offset: int = Query(0, ge=0)):
    conditions, values = ["source='2gis'"], []
    for column, value in [('object_type', object_type), ('building_id', building_id)]:
        if value:
            conditions.append(column+'=?')
            values.append(value)
    if search:
        conditions.append('(name LIKE ? OR address LIKE ?)')
        values.extend(['%'+search+'%']*2)
    with db.connect() as con:
        rows = con.execute('SELECT * FROM source_objects WHERE '+' AND '.join(conditions)+
                           ' ORDER BY source_id LIMIT ? OFFSET ?', [*values, limit, offset]).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['attributes'] = json.loads(item.pop('raw_json'))
        result.append(item)
    return result


@app.get('/api/objects/{source_id}')
def object_detail(source_id: str, source: Literal['2gis'] = '2gis'):
    with db.connect() as con:
        row = con.execute('SELECT raw_json,collected_at FROM source_objects WHERE source=? AND source_id=?',
                          (source, source_id)).fetchone()
    if row is None:
        raise HTTPException(404, 'Объект не найден')
    return {'source': source, 'collected_at': row['collected_at'], 'data': json.loads(row['raw_json'])}


@app.get('/api/export')
def export(format: Literal['json', 'csv'] = 'json', source: Literal['2gis'] = '2gis'):
    with db.connect() as con:
        rows = [dict(row) for row in con.execute("SELECT * FROM source_objects WHERE source='2gis' ORDER BY source_id")]
    if format == 'json':
        for row in rows:
            row['attributes'] = json.loads(row.pop('raw_json'))
        content, media = json.dumps(rows, ensure_ascii=False, indent=2), 'application/json'
    else:
        output = io.StringIO(newline='')
        columns = ['source', 'source_id', 'object_type', 'name', 'address', 'building_id', 'lon', 'lat', 'collected_at']
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            # Prevent spreadsheet formula interpretation when opening exported data.
            writer.writerow({k: "'"+v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v
                             for k, v in row.items()})
        content, media = '\ufeff'+output.getvalue(), 'text/csv'
    return Response(content, media_type=media,
                    headers={'Content-Disposition': f'attachment; filename="eknot-2gis.{format}"'})
