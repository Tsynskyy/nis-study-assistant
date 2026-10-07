from __future__ import annotations

from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup


def _text(element) -> str:
    return " ".join(element.get_text(" ", strip=True).split())


def parse_course_links(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    found: dict[int, dict] = {}
    for link in soup.select('a[href*="/course/view.php"]'):
        href = urljoin(base_url, link.get("href", ""))
        values = parse_qs(urlparse(href).query).get("id", [])
        if not values or not values[0].isdigit():
            continue
        course_id = int(values[0])
        name = _text(link)
        if name and course_id not in found:
            found[course_id] = {"id": course_id, "fullname": name, "viewurl": href}
    return list(found.values())


def parse_course_page(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title_element = soup.select_one("h1") or soup.select_one("title")
    title = _text(title_element) if title_element else ""
    breadcrumbs = [_text(item) for item in soup.select(".breadcrumb-item") if _text(item)]
    sections = []
    section_selectors = "li.section, .course-section, [data-for='section']"
    for position, section in enumerate(soup.select(section_selectors)):
        heading = section.select_one(".sectionname, .section-title, h2, h3, h4")
        activities = []
        seen = set()
        for link in section.select('a[href*="/mod/"][href*="view.php"]'):
            href = urljoin(page_url, link.get("href", ""))
            if href in seen:
                continue
            seen.add(href)
            path_parts = urlparse(href).path.split("/")
            module = path_parts[2] if len(path_parts) > 3 else None
            cmid_values = parse_qs(urlparse(href).query).get("id", [])
            activities.append(
                {
                    "name": _text(link),
                    "url": href,
                    "module": module,
                    "cmid": int(cmid_values[0]) if cmid_values and cmid_values[0].isdigit() else None,
                }
            )
        sections.append(
            {
                "position": position,
                "name": _text(heading) if heading else "",
                "activities": activities,
            }
        )
    return {"title": title, "breadcrumbs": breadcrumbs, "sections": sections}


def parse_tables(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    result = []
    for table_index, table in enumerate(soup.select("table")):
        caption = table.select_one("caption")
        rows = []
        for row in table.select("tr"):
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
                        "links": links,
                    }
                )
            if cells:
                rows.append(cells)
        if rows:
            result.append(
                {
                    "index": table_index,
                    "caption": _text(caption) if caption else "",
                    "rows": rows,
                }
            )
    return result


def parse_notification_page(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    selectors = [
        "[data-region='notification-content-item-container']",
        ".notification-area .content-item-container",
        ".notification",
    ]
    items = []
    seen = set()
    for selector in selectors:
        for item in soup.select(selector):
            text = _text(item)
            if not text or text in seen:
                continue
            seen.add(text)
            link = item.select_one("a[href]")
            time = item.select_one("time, .timestamp, .time")
            items.append(
                {
                    "text": text,
                    "url": urljoin(page_url, link.get("href")) if link else None,
                    "time": _text(time) if time else None,
                }
            )
    return {
        "title": _text(soup.select_one("h1") or soup.select_one("title")),
        "items": items,
        "tables": parse_tables(html, page_url),
    }
