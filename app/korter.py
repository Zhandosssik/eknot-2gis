"""Extract the public catalogue and complex data rendered by Korter.kz."""
import json
import re
from urllib.parse import unquote, urlsplit

from app import db
from app.krisha import CollectionError, Document, collect_complexes, district_from_address

BASE = 'https://korter.kz'
CATALOGS = {'astana': '/новостройки-астаны', 'almaty': '/новостройки-алматы'}
CONSTRUCTION = {'ready': 'Сдан', 'construction': 'Строится', 'planned': 'Запланирован'}
SALES = {'available': 'В продаже', 'all_sold': 'Всё продано'}
PARAMETER_KEYS = {'Этажность': 'count.of.floors', 'Технология строительства': 'home.type'}


def page_store(html, key):
    match = re.search(r'window\.INITIAL_STATE\s*=\s*', html)
    if match is not None:
        try:
            data = json.JSONDecoder().raw_decode(html, match.end())[0]
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get(key), dict):
            return data[key]
    raise CollectionError('В HTML Korter нет ожидаемых данных. Проверьте доступность страницы.')


def clean_text(value):
    if not isinstance(value, str):
        return None
    return ' '.join(value.replace('\u200b', '').replace('\u200d', '').split()) or None


def resolve_district(district, address):
    name = clean_text(district)
    if not name and address:
        address = re.sub(r'(^|,\s*)р\.\s+', r'\1р-н ', address, flags=re.IGNORECASE)
        name = district_from_address(address)
    if name and name.casefold() in {'сарайшық', 'сарайшык'}:
        return 'Сарайшық'
    return name


def load_catalog(get, city, page, index_districts):
    path = CATALOGS[city] + (f'?page={page}' if page > 1 else '')
    data = page_store(get(path), 'buildingListingStore')
    items = data.get('buildings')
    total = data.get('filtersStore', {}).get('totalCount')
    pagination = data.get('pagination', {})
    page_size = pagination.get('itemsPerPage')
    if (type(total) is not int or total < 0 or type(page_size) is not int or page_size < 1
            or pagination.get('page', {}).get('start') != page or not isinstance(items, list)):
        raise CollectionError(f'Не удалось проверить страницу {page} каталога Korter.')
    expected = max(0, min(page_size, total - (page - 1) * page_size))
    cards = {}
    for item in items:
        identifier = item.get('buildingId')
        link = item.get('url')
        if type(identifier) is not int or identifier < 1 or not isinstance(link, str):
            raise CollectionError('В каталоге Korter есть ЖК без ID или ссылки.')
        url = urlsplit(link)
        if (not re.fullmatch(r'/[^/\\?#]+/?', unquote(url.path)) or url.scheme or url.netloc
                or not link.startswith('/') or link.startswith('//') or url.query or url.fragment
                or unquote(url.path).strip('/') in {'.', '..'}):
            raise CollectionError('Каталог Korter вернул неожиданную ссылку на карточку ЖК.')
        district = resolve_district(item.get('subLocalityNominative'), item.get('address')) if index_districts else None
        cards[str(identifier)] = {'id': str(identifier), 'path': url.path, 'district': district}
    if len(items) != expected or len(cards) != expected:
        raise CollectionError(f'Страница {page} Korter: найдено ЖК {len(cards)} из {expected}.')
    return {'items': list(cards.values()), 'total': total, 'has_next': page * page_size < total}


def parse_complex(html, expected_id, path):
    data = page_store(html, 'buildingLandingStore')
    main = data.get('main') or {}
    name = clean_text(main.get('name') or main.get('nameOrAddress'))
    if str(data.get('buildingId')) != str(expected_id) or not name or main.get('isDeleted'):
        raise CollectionError('Карточка Korter недоступна или её ID отличается от ID в каталоге.')

    parameters, labels = {}, {}
    groups = data.get('attributes') or {}
    for field in (*(groups.get('house') or []), *(groups.get('flat') or [])):
        label = clean_text(field.get('name'))
        value = field.get('value')
        if label and isinstance(value, str):
            key = PARAMETER_KEYS.get(label, label)
            parameters[key] = clean_text(Document(value).root.text())
            labels[key] = label

    house_data = data.get('houses') or {}
    queues = {queue['queueId']: queue.get('name') for queue in house_data.get('queues') or []}
    houses, dates = [], set()
    for house in house_data.get('houses') or []:
        year, quarter = house.get('endYear'), house.get('endQuarter')
        if type(year) is int and year > 0:
            dates.add((year, quarter if quarter in (1, 2, 3, 4) else 0))
        houses.append({label: value for label, value in {
            'Название': house.get('name'), 'Адрес': clean_text(house.get('address')),
            'Очередь': queues.get(house.get('queueId')), 'Этажность': house.get('floorCount'),
            'Год сдачи': year, 'Квартал сдачи': quarter,
        }.items() if value is not None})
    completion = ', '.join(f'{quarter} кв. {year}' if quarter else str(year) for year, quarter in sorted(dates))
    district = (main.get('subLocality') or {}).get('nominative')
    point = data.get('map') or {}
    construction, sales = main.get('constructionStatus'), main.get('salesStatus')
    developers = [developer['name'] for developer in main.get('developers') or [] if developer.get('name')]
    return {
        'source_id': str(expected_id), 'source_url': BASE + path, 'collected_at': db.now(),
        'name': name, 'address': clean_text(main.get('address')), 'city': clean_text(main.get('mainGeoObjectNominative')),
        'district': resolve_district(district, main.get('address')), 'developer': ', '.join(developers) or None,
        'status': CONSTRUCTION.get(construction, construction), 'sales_status': SALES.get(sales, sales),
        'completion': completion or None, 'lat': point.get('lat'), 'lon': point.get('lng'),
        'apartments': parameters.pop('Количество квартир', None),
        'price_from': main.get('minPrice'), 'price_m2_from': main.get('minPriceSqm'),
        'parameters': parameters, 'parameter_labels': {key: labels[key] for key in parameters},
        'construction_updated_at': (data.get('constructionState') or {}).get('lastUpdateDate'),
        'houses': houses or None,
    }


def collect(job_id, transport=None, delay=1.5):
    collect_complexes(job_id, 'korter', 'Korter', BASE, load_catalog, parse_complex, transport, delay)
