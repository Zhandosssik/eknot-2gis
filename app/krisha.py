"""Bounded collection of public Krisha complex pages, with durable checkpoints."""
import json
import re
import time
from html.parser import HTMLParser
from typing import Literal

import httpx

from app import db

BASE = 'https://krisha.kz'
KrishaCity = Literal['astana', 'almaty']
DEFAULT_CITY = 'astana'
CITY_LABELS = {'astana': 'Астана', 'almaty': 'Алматы'}
COMPLEX_PATH = re.compile(r'/complex/show/(?:[a-zA-Z0-9_-]+/){1,2}')
DISTRICT_MARKER = r'(?:район|р\s*[-‐‑–—]\s*н)\.?'
DISTRICT_PREFIX = re.compile(rf'{DISTRICT_MARKER}\s+(.+)', re.IGNORECASE)
DISTRICT_SUFFIX = re.compile(rf'(.+?)\s+{DISTRICT_MARKER}', re.IGNORECASE)
DISTRICT_ORDER = "CASE WHEN ? = 'astana' THEN CASE district WHEN 'Нура' THEN 0 WHEN 'Есильский' THEN 1 ELSE 2 END ELSE 0 END, district IS NULL, district"


def district_from_address(address):
    for part in (address or '').split(',', 2)[:2]:
        part = part.strip()
        match = DISTRICT_PREFIX.fullmatch(part) or DISTRICT_SUFFIX.fullmatch(part)
        if match:
            return ' '.join(match[1].split()).capitalize()
    return None


class CollectionError(Exception):
    pass


class BudgetReached(CollectionError):
    pass


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self):
        return ' '.join(' '.join(c.text() if isinstance(c, Node) else c
                                 for c in self.children).split())

    def find(self, tag=None, cls=None):
        for child in self.children:
            if isinstance(child, Node):
                if (tag is None or child.tag == tag) and (cls is None or cls in child.attrs.get('class', '').split()):
                    yield child
                yield from child.find(tag, cls)


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def page_data(html):
    match = re.search(r'window\.data\s*=\s*', html)
    if not match:
        raise CollectionError('Нет данных карточки: возможна проверка доступа или изменение страницы Krisha.')
    try:
        data = json.JSONDecoder().raw_decode(html[match.end():])[0]
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except ValueError:
        raise CollectionError('Изменился формат данных Krisha.') from None


def parse_catalog(html, page, *, index_districts=False):
    search = page_data(html).get('search', {})
    ids, total = search.get('ids'), search.get('nb')
    if not isinstance(ids, list) or not isinstance(total, int):
        raise CollectionError('В каталоге отсутствуют список ЖК или количество результатов.')
    root = Document(html).root
    links = {}
    addresses_found = False
    for node in root.find(cls='complex-card'):
        identifier = node.attrs.get('data-complex-id')
        if not identifier:
            continue
        path = catalog_card_path(node)
        if path is None:
            continue
        item = {'id': identifier, 'path': path}
        if index_districts:
            address = first_text(node, 'complex-card__address')
            addresses_found |= bool(address)
            item['district'] = district_from_address(address)
        links[identifier] = item
    ordered = [links[str(i)] for i in ids if str(i) in links]
    if len(ordered) != len(ids) or (not ids and total):
        raise CollectionError(f'Страница {page} каталога Krisha: найдено ссылок на ЖК {len(ordered)} из {len(ids)}. '
                              'Полнота списка не подтверждена.')
    if index_districts and ordered and not addresses_found:
        raise CollectionError('В каталоге не найдены адреса ЖК. Разделение по районам приостановлено.')
    has_next = any(int(n.attrs.get('data-page') or 0) > page
                   for n in root.find(cls='paginator__btn'))
    return {'items': ordered, 'total': total, 'has_next': has_next}


def catalog_card_path(node):
    # The main card link is authoritative and may omit the city segment.
    for anchor in node.find('a', 'complex-card__main-block'):
        href = anchor.attrs.get('href')
        if COMPLEX_PATH.fullmatch(href or ''):
            return href

    alias = node.attrs.get('data-complex-alias') or ''
    path = '/complex/show/' + alias + '/'
    if COMPLEX_PATH.fullmatch(path) and (
        alias.startswith(('astana/', 'nur-sultan/', 'almaty/'))
        or any(anchor.attrs.get('href') == path for anchor in node.find('a'))
    ):
        return path
    return None


def first_text(node, cls):
    return next((n.text() for n in node.find(cls=cls)), None)


def parse_complex(html, expected_id, path):
    raw = page_data(html).get('complex')
    if not isinstance(raw, dict) or str(raw.get('id')) != str(expected_id) or not raw.get('name'):
        raise CollectionError('Карточка ЖК не соответствует ожидаемому ID.')
    root = Document(html).root
    parameters, labels = {}, {}
    for block in root.find('dl', 'complex-parameters__block'):
        title = next(block.find('dt'), None)
        value = next(block.find('dd'), None)
        if title and value:
            key = title.attrs.get('data-name') or title.text()
            parameters[key], labels[key] = value.text(), title.text()
    sidebar = {}
    for block in root.find(cls='complex__sidebar-info'):
        title = first_text(block, 'complex__sidebar-info-title')
        if title:
            sidebar[title] = first_text(block, 'complex__sidebar-info-text')
    point, rating = raw.get('map') or {}, raw.get('rating') or {}
    apartments = parameters.get('count.of.apartments', '').replace(' ', '')
    # Preserve only complex data; window.data also contains unrelated session/config values.
    keys = ('photos', 'priceSquareHistory', 'constructionProgress', 'layouts', 'layoutsPhotos',
            'premisesLayouts', 'parkingLayouts', 'storeroomLayouts', 'infrastructure')
    return {
        'source_id': str(expected_id), 'name': raw['name'], 'prefix': raw.get('prefix'),
        'address': raw.get('address'), 'city': raw.get('region'),
        'source_url': BASE + path, 'collected_at': db.now(),
        'lat': point.get('lat'), 'lon': point.get('lon'),
        'status': raw.get('stateText'), 'developer': sidebar.get('Застройщик'),
        'completion': sidebar.get('Срок сдачи'), 'sidebar': sidebar,
        'parameters': parameters, 'parameter_labels': labels,
        'apartments': int(apartments) if apartments.isdigit() else None,
        'price_m2_from': raw.get('priceSquareFrom'), 'price_m2_to': raw.get('priceSquareTo'),
        'price_from': raw.get('priceFrom'), 'offers_count': raw.get('nbTotal'),
        'rating': rating.get('value'), 'votes': rating.get('nbVotes'),
        'sales_completed': raw.get('isSalesCompleted'),
        'description': first_text(root, 'complex-text'),
        'buying_methods': next((n.text() for n in root.find() if n.attrs.get('id') == 'buying-methods'), None),
        'secondary_counts': {k: v.get('nbTotal') for k, v in (raw.get('secondary') or {}).items() if isinstance(v, dict)},
        **{k: raw.get(k) for k in keys},
    }


def save_complex(job_id, page, record, city=DEFAULT_CITY, *, source='krisha'):
    record = {**record, 'city': record.get('city') or CITY_LABELS[city]}
    with db.connect() as con:
        con.execute('''INSERT INTO source_objects
            (source,source_id,object_type,name,address,lon,lat,raw_json,collected_at,krisha_city)
            VALUES(?,?,'complex',?,?,?,?,?,?,?)
            ON CONFLICT(source,source_id) DO UPDATE SET name=excluded.name,address=excluded.address,
            lon=excluded.lon,lat=excluded.lat,raw_json=excluded.raw_json,collected_at=excluded.collected_at,
            krisha_city=excluded.krisha_city''',
            (source, record['source_id'], record['name'], record['address'], record['lon'], record['lat'],
             json.dumps(record, ensure_ascii=False), record['collected_at'], city))
        con.execute('INSERT INTO snapshots(job_id,page,payload,collected_at) VALUES(?,?,?,?)',
                    (job_id, page, json.dumps({'complex': record}, ensure_ascii=False), db.now()))
        finish_card(con, job_id, record['source_id'])


def finish_card(con, job_id, source_id):
    changed = con.execute('UPDATE krisha_queue SET done=1 WHERE job_id=? AND source_id=? AND done=0',
                          (job_id, source_id)).rowcount
    con.execute('UPDATE jobs SET processed=processed+?,updated_at=? WHERE id=?', (changed, db.now(), job_id))


def saved_catalog(con, job_id, page):
    row = con.execute('SELECT payload FROM snapshots WHERE job_id=? AND page=? ORDER BY id LIMIT 1',
                      (job_id, page)).fetchone()
    return json.loads(row['payload']).get('catalog') if row else None


def load_catalog(get, city, page, index_districts):
    # Krisha redirects ?page=1 to the catalogue URL without a query.
    path = f'/complex/search/{city}/' + (f'?page={page}' if page > 1 else '')
    return parse_catalog(get(path), page, index_districts=index_districts)


def catalog_for_page(job_id, page, get, *, city, index_districts, load_catalog, label):
    with db.connect() as con:
        catalog = saved_catalog(con, job_id, page)
    if catalog is not None:
        return catalog

    catalog = load_catalog(get, city, page, index_districts)
    ids = {item['id'] for item in catalog['items']}
    with db.connect() as con:
        previous = con.execute('''SELECT payload FROM snapshots WHERE id IN (
                                  SELECT MIN(id) FROM snapshots WHERE job_id=? AND page<? GROUP BY page)''',
                               (job_id, page)).fetchall()
        for row in previous:
            old = json.loads(row['payload']).get('catalog')
            if ids and old and ids == {item['id'] for item in old['items']}:
                raise CollectionError(f'{label} повторяет страницу каталога. Полнота сбора не подтверждена.')
        stamp = db.now()
        con.execute('INSERT INTO snapshots(job_id,page,payload,collected_at) VALUES(?,?,?,?)',
                    (job_id, page, json.dumps({'catalog': catalog}, ensure_ascii=False), stamp))
        con.executemany('INSERT OR IGNORE INTO krisha_queue(job_id,source_id,page,path,district) VALUES(?,?,?,?,?)',
                        [(job_id, item['id'], page, item['path'], item.get('district')) for item in catalog['items']])
        if index_districts and not catalog['has_next']:
            count = con.execute('SELECT count(*) FROM krisha_queue WHERE job_id=?', (job_id,)).fetchone()[0]
            if count < catalog['total']:
                raise CollectionError(f'В каталоге найдено меньше ЖК, чем заявлено {label}. Список районов неполон.')
        next_page = page + 1 if index_districts else page
        con.execute('UPDATE jobs SET total=?,next_page=?,updated_at=? WHERE id=?',
                    (catalog['total'], next_page, stamp, job_id))
    return catalog


def first_pending_district(con, job_id, city):
    return con.execute(f'''SELECT district FROM krisha_queue WHERE job_id=? AND done=0
                           ORDER BY {DISTRICT_ORDER} LIMIT 1''', (job_id, city)).fetchone()


def district_progress(job):
    with db.connect() as con:
        last_catalog = saved_catalog(con, job['id'], job['next_page'] - 1)
        if last_catalog is None or last_catalog['has_next']:
            return {'stage': 'catalog', 'current_district': None}
        pending = first_pending_district(con, job['id'], job['config'].get('city', DEFAULT_CITY))
        if pending is None:
            return {'stage': 'districts', 'current_district': None}
        counts = con.execute('SELECT count(*) AS total, sum(done) AS done FROM krisha_queue WHERE job_id=? AND district IS ?',
                             (job['id'], pending['district'])).fetchone()
    return {'stage': 'districts', 'current_district': pending['district'] or 'Район не указан',
            'district_total': counts['total'], 'district_processed': counts['done']}


def collect_by_district(job, get, city, source, catalog_page, parse_card):
    job_id = job['id']
    if district_progress(job)['stage'] == 'catalog':
        for page in range(job['next_page'], job['next_page'] + job['config']['max_pages']):
            catalog = catalog_page(page, index_districts=True)
            if not catalog['has_next']:
                break
        else:
            db.update_job(job_id, status='limited', error='Достигнут лимит страниц каталога. Можно продолжить.')
            return

    with db.connect() as con:
        district = first_pending_district(con, job_id, city)
        district_field = ('krisha_district(stored.address)' if source == 'krisha'
                          else "json_extract(stored.raw_json, '$.district')")
        con.create_function('krisha_district', 1, district_from_address, deterministic=True)
        pending = []
        if district is not None:
            pending = con.execute(f'''SELECT q.*, {district_field} AS saved_district, stored.source_id AS saved_id
                                     FROM krisha_queue q LEFT JOIN source_objects stored
                                       ON stored.source=? AND stored.source_id=q.source_id
                                       AND coalesce(stored.krisha_city, 'astana')=?
                                       AND json_extract(stored.raw_json, '$.source_id')=q.source_id
                                     WHERE q.job_id=? AND q.done=0 AND q.district IS ?
                                     ORDER BY q.page, q.rowid''', (source, city, job_id, district['district'])).fetchall()
    for item in pending:
        if item['saved_id'] and (not item['district'] or item['saved_district'] == item['district']):
            with db.connect() as con:
                finish_card(con, job_id, item['source_id'])
        else:
            record = parse_card(get(item['path']), item['source_id'], item['path'])
            save_complex(job_id, item['page'], record, city, source=source)
    with db.connect() as con:
        remaining = con.execute('SELECT 1 FROM krisha_queue WHERE job_id=? AND done=0 LIMIT 1', (job_id,)).fetchone()
    db.update_job(job_id, status='district_completed' if remaining else 'completed')


def collect(job_id, transport=None, delay=1.5):
    collect_complexes(job_id, 'krisha', 'Krisha', BASE, load_catalog, parse_complex, transport, delay)


def collect_complexes(job_id, source, label, base_url, load_catalog, parse_card, transport, delay):
    current = db.job(job_id)
    config = current['config']
    city = config.get('city', DEFAULT_CITY)
    budget = current['requests'] + config['max_requests']
    db.update_job(job_id, status='running', error=None)
    with httpx.Client(base_url=base_url, timeout=30, follow_redirects=False, transport=transport) as client:
        def get(path):
            count = db.job(job_id)['requests']
            if count >= budget:
                raise BudgetReached('Достигнут лимит запросов. Продолжите задание с сохранённого места.')
            db.update_job(job_id, requests=count+1)
            time.sleep(delay)
            try:
                response = client.get(path)
            except httpx.RequestError:
                raise CollectionError(f'Ошибка сети при обращении к {label}. Прогресс сохранён.') from None
            if response.status_code != 200:
                raise CollectionError(f'{label}: HTTP {response.status_code}. Сбор остановлен, прогресс сохранён.')
            return response.text

        def catalog_page(page, *, index_districts=False):
            return catalog_for_page(job_id, page, get, city=city, index_districts=index_districts,
                                    load_catalog=load_catalog, label=label)

        try:
            if config.get('mode') == 'districts':
                collect_by_district(current, get, city, source, catalog_page, parse_card)
                return
            for page in range(current['next_page'], current['next_page'] + config['max_pages']):
                catalog = catalog_page(page)
                with db.connect() as con:
                    pending = con.execute('SELECT * FROM krisha_queue WHERE job_id=? AND page=? AND done=0 ORDER BY rowid', (job_id, page)).fetchall()
                for item in pending:
                    record = parse_card(get(item['path']), item['source_id'], item['path'])
                    save_complex(job_id, page, record, city, source=source)
                if not catalog['has_next']:
                    if db.job(job_id)['processed'] < catalog['total']:
                        raise CollectionError('Каталог закончился раньше заявленного количества ЖК. Полнота не подтверждена.')
                    db.update_job(job_id, next_page=page+1, status='completed')
                    return
                db.update_job(job_id, next_page=page+1)
            db.update_job(job_id, status='limited', error='Достигнут лимит страниц. Можно продолжить.')
        except BudgetReached as exc:
            db.update_job(job_id, status='limited', error=str(exc))
        except CollectionError as exc:
            db.update_job(job_id, status='failed', error=str(exc))
        except Exception:
            db.update_job(job_id, status='failed', error=f'Ошибка обработки {label}. Прогресс сохранён; проверьте формат страницы.')
