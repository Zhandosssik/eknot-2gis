"""Read three public building pages; extract only visible HTML text."""
import json
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []
        self.in_heading = False
        self.heading = []
        self.address = None
        self.heading_end = None
        self.map_center = None

    def handle_starttag(self, tag, attrs):
        if tag == 'h1':
            self.in_heading = True
        attributes = dict(attrs)
        if tag == 'meta' and attributes.get('property') == 'og:image':
            # Read metadata already present in the page; do not call the image service.
            try:
                value = parse_qs(urlsplit(attributes.get('content', '')).query).get('center', [''])[0]
                lon, lat = map(float, value.split(','))
                if -180 <= lon <= 180 and -90 <= lat <= 90:
                    self.map_center = (lon, lat)
            except ValueError:
                pass
        if tag == 'meta' and attributes.get('itemprop') == 'streetAddress':
            self.address = attributes.get('content')
        if tag in ('script', 'style'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag == 'h1':
            self.in_heading = False
            self.heading_end = len(self.parts)
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())
            if self.in_heading:
                self.heading.append(data.strip())


def check_pages(identifiers, *, transport=None, output_root=None):
    identifiers = list(dict.fromkeys(identifiers))
    if not 1 <= len(identifiers) <= 3 or any(not re.fullmatch(r'[0-9]{1,30}', value) for value in identifiers):
        raise ValueError('Укажите от одного до трёх числовых ID зданий.')
    root = Path(output_root) if output_root else Path(__file__).resolve().parents[1] / 'data'
    stamp = datetime.now(timezone.utc).isoformat().replace(':', '-')
    output = root / ('2gis-public-' + stamp)
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with httpx.Client(timeout=15, follow_redirects=False, transport=transport) as client:
        for identifier in identifiers:
            url = 'https://2gis.kz/astana/geo/' + identifier
            entry = {'source_id': identifier, 'url': url, 'checked_at': datetime.now(timezone.utc).isoformat()}
            try:
                response = client.get(url)
                entry['http'] = response.status_code
                parser = VisibleText()
                parser.feed(response.text)
                entry['name'] = ' '.join(parser.heading) or None
                entry['address'] = parser.address
                if parser.heading_end is not None:
                    following = [re.sub(r'\s+', ' ', part.replace('\u200b', '')).strip()
                                 for part in parser.parts[parser.heading_end:]]
                    following = [part for part in following if part]
                    if following and re.search(r',\s*\d', following[0]):
                        entry['address'] = following[0]
                    if len(following) > 1 and 'район' in following[1]:
                        entry['district_city'] = following[1]
                text = re.sub(r'\s+', ' ', ' '.join(parser.parts)).replace('\u200b', '')
                (output / (identifier + '.txt')).write_text(text, encoding='utf-8')
                # Evidence snippets are retained; absent values remain null.
                patterns = {'floors': r'Жилой дом\s*(\d+)\s*этаж', 'entrances': r'В доме\s*(\d+)\s*подъезд', 'material': r'Материал стен\s*(Кирпич|Монолит|Панель|Дерево|Блоки)'}
                entry['fields'] = {}
                entry['evidence'] = {}
                if response.status_code == 200:
                    entry['evidence'].update(name=entry['name'], address=entry['address'])
                    for field, pattern in patterns.items():
                        match = re.search(pattern, text, re.I)
                        entry['fields'][field] = match.group(1) if match else None
                        entry['evidence'][field] = match.group(0) if match else None
                    extra_patterns = {
                        'purpose': r'\b(Жилой дом)\b',
                        'rating': r'\b([0-5](?:[.,]\d)?)\s+\d+\s+оцен',
                        'reviews': r'Отзывы\s+(\d+)',
                        'apartments': r'Количество квартир\s*:?\s*(\d+)',
                        'elevators': r'Количество лифтов\s*:?\s*(\d+)',
                        'year': r'Год постройки\s*:?\s*((?:18|19|20)\d{2})',
                    }
                    for field, pattern in extra_patterns.items():
                        match = re.search(pattern, text, re.I)
                        value = match.group(1) if match else None
                        if value is not None and field != 'purpose':
                            value = float(value.replace(',', '.')) if field == 'rating' else int(value)
                        entry[field] = value
                        entry['evidence'][field] = match.group(0) if match else None
                    if parser.map_center:
                        entry['lon'], entry['lat'] = parser.map_center
                        entry['evidence']['coordinates'] = 'Параметр center метатега og:image: центр карты карточки, не кадастровые координаты.'
                    entry['status'] = 'found' if any(value is not None for value in entry['fields'].values()) else 'not_found'
                else:
                    entry.update(status='failed', error='Страница вернула HTTP ' + str(response.status_code))
                entry['text_length'] = len(text)
            except httpx.RequestError:
                entry.update(status='failed', error='Не удалось загрузить страницу: ошибка сети или тайм-аут.')
            results.append(entry)
            if entry.get('http') in (401, 403, 429):
                for skipped in identifiers[len(results):]:
                    results.append({'source_id': skipped, 'status': 'skipped', 'error': 'Проверка остановлена после ограничения доступа со стороны 2ГИС.'})
                break
            time.sleep(1)
    (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'results': results, 'report_path': str(output / 'results.json'),
            'downloads': {fmt: '/api/2gis/public-pages/export?format=' + fmt + '&report=' + output.name.replace('+', '%2B') for fmt in ('xlsx', 'csv')},
            'note': 'Все значения извлечены с публичной страницы. null означает, что поле не найдено.'}
