"""Tabular exports of complex fields and dynamic characteristics."""
import json

from app.public_export import export_csv_rows, export_xlsx_rows

COLUMNS = {
    'source_id': 'ID ЖК', 'name': 'Название', 'address': 'Адрес', 'city': 'Город', 'district': 'Район',
    'developer': 'Застройщик', 'status': 'Статус', 'completion': 'Срок сдачи',
    'apartments': 'Всего квартир в ЖК', 'price_m2_from': 'Цена м² от, ₸',
    'price_m2_to': 'Цена м² до, ₸', 'price_from': 'Цена квартиры от, ₸',
    'offers_count': 'Предложений в карточке', 'rating': 'Рейтинг', 'votes': 'Оценок',
    'lat': 'Широта', 'lon': 'Долгота', 'source_url': 'Источник', 'collected_at': 'Дата сбора',
    'description': 'Описание', 'buying_methods': 'Способы покупки',
}

EXCLUDED_COLUMNS = {
    'layouts', 'layoutsPhotos', 'parkingLayouts', 'photos',
    'prefix', 'premisesLayouts', 'priceSquareHistory', 'sales_completed',
    'secondary_counts', 'sidebar', 'storeroomLayouts', 'parameters', 'parameter_labels', 'infrastructure',
}


def format_infrastructure(groups):
    if not isinstance(groups, list):
        return None
    sections = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('places'), list):
            continue
        places = []
        for place in group['places']:
            if not isinstance(place, dict):
                continue
            name = place.get('name') or place.get('title')
            distance = place.get('distance')
            if name:
                places.append(f'{name} ({distance})' if distance else name)
        if places:
            category = group.get('name') or 'Другие места'
            sections.append(f'{category}: {", ".join(places)}')
    return '\n'.join(sections) or None


def export(records, format, *, source='krisha'):
    columns = COLUMNS
    if source == 'korter':
        columns = {key: label for key, label in COLUMNS.items() if key not in {
            'offers_count', 'rating', 'votes', 'buying_methods', 'description', 'price_m2_to',
        }}
        columns['apartments'] = 'Количество квартир (по Korter)'
        columns['sales_status'] = 'Статус продаж'
        columns['construction_updated_at'] = 'Обновление хода строительства'
        columns['houses'] = 'Дома проекта'
    parameter_labels = {}
    for r in records:
        parameter_labels.update(r.get('parameter_labels', {}))
    extras = sorted({k for r in records for k in r} - columns.keys() - EXCLUDED_COLUMNS)
    extra_labels = {'constructionProgress': 'Ход строительства'}
    keys = list(columns)
    headers = list(columns.values()) + ['Характеристика: '+v for v in parameter_labels.values()]
    headers.extend(extra_labels.get(key, key) for key in extras)
    if source == 'krisha':
        headers.append('Инфраструктура рядом')
    rows = [headers]
    for record in records:
        values = [record.get(k) for k in keys] + [record.get('parameters', {}).get(k) for k in parameter_labels] + [record.get(k) for k in extras]
        if source == 'krisha':
            values.append(format_infrastructure(record.get('infrastructure')))
        rows.append([json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in values])
    if format == 'xlsx':
        return export_xlsx_rows(rows, 'ЖК Krisha' if source == 'krisha' else 'ЖК Korter', wrap_last_column=source == 'krisha')
    return export_csv_rows(rows)
