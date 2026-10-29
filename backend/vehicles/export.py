"""Small native XLSX reports; every value is a string, never an Excel formula."""
from io import BytesIO
import re
from xml.sax.saxutils import escape
from zipfile import ZipFile, ZIP_DEFLATED


def workbook(sheets):
    def column(i):
        result = ''
        while i:
            i, rem = divmod(i-1, 26)
            result = chr(65+rem) + result
        return result
    out = BytesIO()
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    with ZipFile(out, 'w', ZIP_DEFLATED) as archive:
        content = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        rels = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        book = f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
        for i, (name, rows) in enumerate(sheets, 1):
            content += f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            rels += f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
            book += f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
            xml = f'<worksheet xmlns="{ns}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="{len(rows[0])}" width="24" customWidth="1"/></cols><sheetData>'
            for number, row in enumerate(rows, 1):
                xml += f'<row r="{number}">'
                for index, value in enumerate(row, 1):
                    value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value if value is not None else ''))[:32767]
                    xml += f'<c r="{column(index)}{number}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'
                xml += '</row>'
            xml += f'</sheetData><autoFilter ref="A1:{column(len(rows[0]))}{len(rows)}"/></worksheet>'
            archive.writestr(f'xl/worksheets/sheet{i}.xml', xml)
        archive.writestr('[Content_Types].xml', content + '</Types>')
        archive.writestr('xl/workbook.xml', book + '</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', rels + '</Relationships>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    return out.getvalue()
