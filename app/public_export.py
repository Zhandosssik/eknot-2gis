"""Portable CSV and XLSX exports for the local service (no desktop Excel needed)."""
import csv
import io
import re
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


def column_name(index):
    name = ''
    while index:
        index, digit = divmod(index - 1, 26)
        name = chr(65 + digit) + name
    return name


def rows_for_export(results):
    rows = [['ID объекта', 'Этажей', 'Материал стен', 'Подъездов', 'Название',
             'Адрес', 'Район', 'Долгота', 'Широта', 'Назначение здания',
             'Рейтинг', 'Отзывы', 'Квартир', 'Лифтов', 'Год постройки']]
    for item in results:
        fields = item.get('fields', {})
        rows.append([item.get('source_id'), fields.get('floors'), fields.get('material'),
                     fields.get('entrances'), item.get('name'), item.get('address'),
                     (item.get('district_city') or '').split(',')[0] or None,
                     item.get('lon'), item.get('lat'), item.get('purpose'),
                     item.get('rating'), item.get('reviews'), item.get('apartments'),
                     item.get('elevators'), item.get('year')])
    return rows


def export_csv(results):
    return export_csv_rows(rows_for_export(results))


def export_csv_rows(rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output, delimiter=';')
    for index, row in enumerate(rows):
        values = []
        for column, value in enumerate(row):
            if index and column == 0 and value is not None:
                value = "'" + str(value)
            elif isinstance(value, float):
                value = str(value).replace('.', ',')
            elif isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return ('\ufeff' + output.getvalue()).encode('utf-8')


def export_xlsx(results):
    return export_xlsx_rows(rows_for_export(results))


def export_xlsx_rows(rows, sheet_name='Дома 2ГИС', *, wrap_last_column=False):
    # Inline string cells preserve long building IDs and cannot execute formulas.
    last_column = len(rows[0])
    xml_rows = []
    for index, row in enumerate(rows, 1):
        cells = []
        for column, value in enumerate(row):
            address = column_name(column + 1) + str(index)
            if value is None:
                continue
            if index > 1 and ((column in (1, 3) and str(value).isdigit()) or (isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) < 10**15)):
                cells.append(f'<c r="{address}"><v>{value}</v></c>')
            else:
                text = escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value)))
                style = ' s="1"' if wrap_last_column and column + 1 == last_column else ''
                cells.append(f'<c r="{address}" t="inlineStr"{style}><is><t xml:space="preserve">{text}</t></is></c>')
        xml_rows.append(f'<row r="{index}">{"".join(cells)}</row>')
    regular_last_column = last_column - int(wrap_last_column)
    final_column_width = (
        f'<col min="{last_column}" max="{last_column}" width="80" customWidth="1"/>'
        if wrap_last_column else ''
    )
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
             '<cols><col min="1" max="1" width="24" customWidth="1"/><col min="2" max="4" width="20" customWidth="1"/>'
             f'<col min="5" max="{regular_last_column}" width="32" customWidth="1"/>{final_column_width}</cols>'
             f'<sheetData>{"".join(xml_rows)}</sheetData><autoFilter ref="A1:{column_name(last_column)}{len(rows)}"/></worksheet>')
    files = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="'+escape(sheet_name, {'"': '&quot;'})+'" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': sheet,
    }
    if wrap_last_column:
        files['[Content_Types].xml'] = files['[Content_Types].xml'].replace(
            '</Types>', '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
        files['xl/_rels/workbook.xml.rels'] = files['xl/_rels/workbook.xml.rels'].replace(
            '</Relationships>', '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        files['xl/styles.xml'] = (
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
            '<alignment vertical="top" wrapText="1"/></xf></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>'
        )
    output = io.BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + content)
    return output.getvalue()
