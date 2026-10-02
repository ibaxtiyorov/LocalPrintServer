"""Minimal, valid .docx / .xlsx files built from scratch (no Office needed).

Used by the admin "Test Word/Excel conversion" button and by the automated tests.
"""
from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
         'relationships/officeDocument" Target="{target}"/></Relationships>')


def make_docx(pages: int = 2, extra_rels: str = "", content_type_extra: str = "") -> bytes:
    """A Word document with `pages` pages separated by explicit page breaks."""
    body = []
    for i in range(pages):
        body.append(f'<w:p><w:r><w:t>{escape(f"LocalPrintServer test document - page {i + 1}")}'
                    '</w:t></w:r></w:p>')
        body.append('<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell A</w:t></w:r></w:p></w:tc>'
                    '<w:tc><w:p><w:r><w:t>Table cell B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
        if i < pages - 1:
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{"".join(body)}</w:body></w:document>')
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
              f'officedocument.wordprocessingml.document.main+xml"/>{content_type_extra}</Types>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", _RELS.format(target="word/document.xml"))
        z.writestr("word/document.xml", document)
        if extra_rels:
            z.writestr("word/_rels/document.xml.rels",
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships '
                       'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       f'{extra_rels}</Relationships>')
    return buf.getvalue()


def make_xlsx(sheets: int = 2) -> bytes:
    """An Excel workbook with `sheets` worksheets containing a small table each."""
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 '<sheetData>{rows}</sheetData></worksheet>')
    rows = "".join(f'<row r="{r}"><c r="A{r}" t="inlineStr"><is><t>Item {r}</t></is></c>'
                   f'<c r="B{r}"><v>{r * 10}</v></c></row>' for r in range(1, 11))
    wb_sheets = "".join(f'<sheet name="Sheet{i}" sheetId="{i}" r:id="rId{i}"/>'
                        for i in range(1, sheets + 1))
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets>{wb_sheets}</sheets></workbook>')
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/'
                         'officeDocument/2006/relationships/worksheet" '
                         f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, sheets + 1))
               + '</Relationships>')
    overrides = "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/'
                        'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                        for i in range(1, sheets + 1))
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
              f'officedocument.spreadsheetml.sheet.main+xml"/>{overrides}</Types>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", _RELS.format(target="xl/workbook.xml"))
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        for i in range(1, sheets + 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", sheet_xml.format(rows=rows))
    return buf.getvalue()
