from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
import xlrd

from .parsers import _text


def _direct_rows(table) -> list:
    return table.select(":scope > thead > tr, :scope > tbody > tr, :scope > tfoot > tr, :scope > tr")


def parse_leaf_tables(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    tables = []
    for index, table in enumerate(soup.select("table")):
        if table.select_one(":scope table"):
            continue
        rows = []
        for row in _direct_rows(table):
            cells = []
            for cell in row.select(":scope > th, :scope > td"):
                links = [
                    {"text": _text(link), "url": urljoin(page_url, link.get("href", ""))}
                    for link in cell.select("a[href]")
                ]
                cells.append(
                    {
                        "kind": cell.name,
                        "text": _text(cell),
                        "colspan": int(cell.get("colspan", 1) or 1),
                        "rowspan": int(cell.get("rowspan", 1) or 1),
                        "links": links,
                    }
                )
            if cells:
                rows.append(cells)
        if rows:
            tables.append(
                {
                    "index": index,
                    "id": table.get("id"),
                    "classes": table.get("class", []),
                    "rows": rows,
                }
            )
    return tables


def parse_form_fields(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    fields = []
    for element in soup.select("input[name], select[name], textarea[name]"):
        field_type = (element.get("type") or element.name).lower()
        if field_type in {"password", "hidden", "submit", "button", "file", "reset", "image"}:
            continue
        name = element.get("name", "")
        if name in {"search_text", "current_location"}:
            continue
        label = None
        if element.get("id"):
            label_element = soup.select_one(f'label[for="{element.get("id")}"]')
            if label_element:
                label = _text(label_element)
        if not label:
            cell = element.find_parent("td")
            previous = cell.find_previous_sibling(["td", "th"]) if cell else None
            label = _text(previous) if previous else name
        if element.name == "select":
            selected = element.select_one("option[selected]") or element.select_one("option")
            value: Any = selected.get("value", "") if selected else ""
            display = _text(selected) if selected else ""
        elif field_type in {"checkbox", "radio"}:
            value = element.has_attr("checked")
            display = str(value)
        else:
            value = element.get("value", "") if element.name == "input" else element.get_text()
            display = str(value)
        fields.append({"name": name, "label": label.rstrip(":"), "value": value, "display": display})
    return fields


def parse_legacy_page(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = soup.select_one("h1") or soup.select_one(".title") or soup.select_one("title")
    headings = []
    for element in soup.select("h1,h2,h3,h4,h5,.panel-heading,.box-title"):
        value = _text(element)
        if value and value not in headings:
            headings.append(value)
    return {
        "url": page_url,
        "title": _text(title) if title else "",
        "headings": headings,
        "tables": parse_leaf_tables(html, page_url),
        "fields": parse_form_fields(html),
    }


def parse_gradebook(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    block = soup.select_one("#gradebookBlock")
    if block is None:
        return {"url": page_url, "available": False, "summary": "", "tables": []}
    summary = block.select_one("#gradebookSumHours")
    tables = []
    for table in block.select("table"):
        rows = []
        for row in _direct_rows(table):
            cells = [
                {
                    "kind": cell.name,
                    "text": _text(cell),
                    "colspan": int(cell.get("colspan", 1) or 1),
                    "rowspan": int(cell.get("rowspan", 1) or 1),
                }
                for cell in row.select(":scope > th, :scope > td")
            ]
            if cells and not (len(cells) == 1 and not cells[0]["text"]):
                rows.append(cells)
        tables.append({"rows": rows})
    data_rows = 0
    for table in block.select("table"):
        for row in table.select(":scope > tbody > tr"):
            cells = row.select(":scope > td")
            if cells and any(_text(cell) for cell in cells) and not row.select_one(".sortedTableFooter"):
                data_rows += 1
    return {
        "url": page_url,
        "available": True,
        "summary": _text(summary) if summary else "",
        "row_count": data_rows,
        "tables": tables,
    }


def parse_legacy_courses(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    courses: dict[int, dict] = {}
    for link in soup.select('a[href*="lessons_ID="]'):
        url = urljoin(page_url, link.get("href", ""))
        values = parse_qs(urlparse(url).query).get("lessons_ID", [])
        if not values or not values[0].isdigit():
            continue
        course_id = int(values[0])
        name = _text(link)
        if name and course_id not in courses:
            courses[course_id] = {"id": course_id, "name": name, "url": url}
    return list(courses.values())


def _xls_value(book, cell) -> Any:
    if cell.ctype == xlrd.XL_CELL_DATE:
        return xlrd.xldate_as_datetime(cell.value, book.datemode).isoformat()
    if cell.ctype == xlrd.XL_CELL_NUMBER and float(cell.value).is_integer():
        return int(cell.value)
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
        return None
    if isinstance(cell.value, datetime):
        return cell.value.isoformat()
    return cell.value


def parse_xls(content: bytes) -> dict:
    book = xlrd.open_workbook(file_contents=content, formatting_info=False)
    sheets = []
    for sheet in book.sheets():
        rows = [
            [_xls_value(book, sheet.cell(row, column)) for column in range(sheet.ncols)]
            for row in range(sheet.nrows)
        ]
        sheets.append({"name": sheet.name, "row_count": sheet.nrows, "column_count": sheet.ncols, "rows": rows})
    return {"format": "xls", "sheets": sheets}
