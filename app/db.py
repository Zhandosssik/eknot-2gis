import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.settings import database_path


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init(recover_jobs=True):
    with connect() as con:
        con.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL,
          config TEXT NOT NULL, city_id TEXT, next_page INTEGER NOT NULL DEFAULT 1,
          requests INTEGER NOT NULL DEFAULT 0, processed INTEGER NOT NULL DEFAULT 0,
          total INTEGER, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_objects (
          source TEXT NOT NULL, source_id TEXT NOT NULL, object_type TEXT NOT NULL,
          name TEXT, address TEXT, building_id TEXT, lon REAL, lat REAL,
          raw_json TEXT NOT NULL, collected_at TEXT NOT NULL,
          PRIMARY KEY(source, source_id)
        );
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, page INTEGER NOT NULL,
          payload TEXT NOT NULL, collected_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_objects_building
          ON source_objects(source, building_id);
        ''')
        if recover_jobs:
            con.execute("UPDATE jobs SET status='interrupted', error='Сервис остановился. Можно продолжить сбор.', updated_at=? WHERE source='2gis' AND status IN ('running','queued')", (now(),))


def job(job_id):
    with connect() as con:
        row = con.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['config'] = json.loads(result['config'])
    return result


def update_job(job_id, **values):
    allowed = {'status', 'city_id', 'next_page', 'requests', 'processed', 'total', 'error'}
    if not set(values) <= allowed:
        raise ValueError('Invalid job field')
    values['updated_at'] = now()
    with connect() as con:
        con.execute(f"UPDATE jobs SET {', '.join(k+'=?' for k in values)} WHERE id=?",
                    [*values.values(), job_id])


def save_page(job_id, page, payload, requested_building_id=None):
    items = payload['result'].get('items', [])
    timestamp = now()
    with connect() as con:
        con.execute('INSERT INTO snapshots(job_id,page,payload,collected_at) VALUES(?,?,?,?)',
                    (job_id, page, json.dumps(payload, ensure_ascii=False), timestamp))
        for item in items:
            point = item.get('point') or {}
            address = item.get('address') or {}
            con.execute('''INSERT INTO source_objects
              (source,source_id,object_type,name,address,building_id,lon,lat,raw_json,collected_at)
              VALUES('2gis',?,?,?,?,?,?,?,?,?)
              ON CONFLICT(source,source_id) DO UPDATE SET
              object_type=excluded.object_type, name=excluded.name, address=excluded.address,
              building_id=excluded.building_id, lon=excluded.lon,lat=excluded.lat,
              raw_json=excluded.raw_json,collected_at=excluded.collected_at''',
              (str(item['id']), item['type'], item.get('name'), item.get('address_name'),
               str(address['building_id']) if address.get('building_id') else requested_building_id,
               point.get('lon'), point.get('lat'), json.dumps(item, ensure_ascii=False), timestamp))
        con.execute('''UPDATE jobs SET next_page=?, processed=processed+?, total=?,
          updated_at=? WHERE id=?''',
          (page+1, len(items), payload['result'].get('total'), timestamp, job_id))


