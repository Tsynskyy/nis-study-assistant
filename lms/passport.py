from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup


MOSCOW = ZoneInfo("Europe/Moscow")
YEAR_RE = re.compile(r"(20\d{2}/20\d{2})")
SHORT_YEAR_RE = re.compile(r"(?<!\d)(\d{2})-(\d{2})(?=\s*уч)", re.IGNORECASE)
MODULE_RE = re.compile(r"модули?:\s*([^)]*)", re.IGNORECASE)
NUMBER_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")


COURSE_FIELDS = [
    "course_id", "course_name", "academic_year", "modules", "course_kind",
    "start_date", "end_date", "lifecycle_status", "visible", "hidden",
    "progress", "activity_count", "gradebook_available", "graded_item_count",
    "source_url",
]
ACTIVITY_FIELDS = [
    "course_id", "course_name", "academic_year", "modules", "section_position",
    "section_name", "module", "cmid", "activity_name", "url",
]
GRADE_FIELDS = [
    "course_id", "course_name", "academic_year", "modules", "item_type",
    "item_name", "weight", "grade", "grade_numeric", "range", "percentage",
    "feedback", "contribution", "has_grade", "source_url", "item_url",
]
DEADLINE_FIELDS = [
    "course_id", "course_name", "academic_year", "modules", "activity_name",
    "event_name", "event_type", "due_at_moscow", "status", "overdue",
    "description", "source_url",
]
MESSAGE_FIELDS = [
    "conversation_id", "message_id", "created_at_moscow", "sender_id",
    "sender_name", "conversation_with", "is_read", "text", "urls",
]
NOTIFICATION_FIELDS = [
    "notification_id", "created_at_moscow", "course_id", "course_name",
    "subject", "text", "is_read", "url",
]
FINDING_FIELDS = [
    "finding_id", "priority", "category", "title", "details", "status", "source",
]


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o666)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if private:
        temporary.chmod(0o600)
    temporary.replace(path)


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="raise", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    temporary.replace(path)


def _clean_html(value: object) -> str:
    if value is None:
        return ""
    soup = BeautifulSoup(str(value), "lxml")
    return " ".join(soup.get_text(" ", strip=True).split())


def _html_urls(value: object) -> list[str]:
    if not value:
        return []
    soup = BeautifulSoup(str(value), "lxml")
    return list(dict.fromkeys(link.get("href", "") for link in soup.select("a[href]") if link.get("href")))


def _academic_year(name: str) -> str:
    match = YEAR_RE.search(name)
    if match:
        return match.group(1)
    short = SHORT_YEAR_RE.search(name)
    if short:
        return f"20{short.group(1)}/20{short.group(2)}"
    return ""


def _modules(name: str) -> str:
    match = MODULE_RE.search(name)
    return " ".join(match.group(1).split()) if match else ""


def _course_kind(name: str) -> str:
    lower = name.lower()
    if "курсовой проект" in lower or "подготовка к защите кр" in lower:
        return "курсовой проект"
    if "практик" in lower:
        return "практика"
    if "пересдача" in lower:
        return "пересдача"
    if "независимый экзамен" in lower or "внутренний экзамен" in lower:
        return "экзамен"
    if "подготовка" in lower:
        return "подготовительный курс"
    if "выбор" in lower:
        return "выбор дисциплин"
    if lower.startswith("уо "):
        return "страница образовательной программы"
    if _academic_year(name):
        return "дисциплина"
    return "прочее"


def _date_from_timestamp(value: object) -> str:
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone(MOSCOW).date().isoformat()


def _datetime_from_timestamp(value: object) -> str:
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone(MOSCOW).isoformat(timespec="minutes")


def _current_academic_year(as_of: datetime) -> str:
    local = as_of.astimezone(MOSCOW)
    first = local.year if local.month >= 9 else local.year - 1
    return f"{first}/{first + 1}"


def _lifecycle(course: dict, as_of: datetime, academic_year: str) -> str:
    now = int(as_of.timestamp())
    start = int(course.get("startdate") or 0)
    end = int(course.get("enddate") or 0)
    if end and end < now:
        return "завершен"
    if start and start > now:
        return "запланирован"
    if start and (not end or now <= end):
        if not end:
            if academic_year == _current_academic_year(as_of):
                return "активен"
            return "долгоживущий"
        return "активен"
    current_year = _current_academic_year(as_of)
    if academic_year:
        if academic_year == current_year:
            return "активен без дат"
        if academic_year > current_year:
            return "запланирован без дат"
        return "завершен без дат"
    return "долгоживущий"


def _grade_cells(row: list[dict]) -> dict[str, str] | None:
    values = [" ".join(str(cell.get("text", "")).split()) for cell in row]
    if not values or values[0] in {"Элемент оценивания", "Grade item"} or len(values) == 1:
        return None
    if len(values) >= 7:
        weight, grade, grade_range, percentage, feedback, contribution = values[1:7]
    elif len(values) == 5:
        weight, feedback = "", ""
        grade, grade_range, percentage, contribution = values[1:5]
    elif len(values) == 4:
        weight, feedback, contribution = "", "", ""
        grade, grade_range, percentage = values[1:4]
    else:
        return None
    grade = re.sub(r"(?:\s+|^)(?:Анализ оценок|Grade analysis)$", "", grade, flags=re.IGNORECASE).strip()
    return {
        "item_name": values[0], "weight": weight, "grade": grade,
        "range": grade_range, "percentage": percentage, "feedback": feedback,
        "contribution": contribution,
    }


def _item_type(name: str) -> str:
    lower = name.lower()
    if "итоговая оценка за курс" in lower or "course total" in lower:
        return "итог курса"
    if "итого в категории" in lower or "category total" in lower:
        return "итог категории"
    prefixes = {
        "задание ": "задание", "тест ": "тест", "h5p ": "h5p",
        "субкурс ": "субкурс", "игра ": "игра",
        "взаимное оценивание ": "взаимное оценивание",
        "заполняемый вручную элемент ": "ручная оценка",
    }
    for prefix, value in prefixes.items():
        if lower.startswith(prefix):
            return value
    return "элемент оценивания"


def _numeric_grade(value: str) -> float | str:
    compact = value.strip().replace(" ", "")
    if not NUMBER_RE.match(compact):
        return ""
    return float(compact.replace(",", "."))


def _snapshot_time(snapshot: dict) -> datetime:
    raw = snapshot.get("finished_at") or snapshot.get("started_at")
    if raw:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _legacy_facts(root: Path) -> dict[str, Any]:
    legacy = root / "legacy-lms"
    snapshot = _read_json(legacy / "snapshot.json", {})
    profile = _read_json(legacy / "profile.json", {})
    reports = _read_json(legacy / "reports.json", {})
    fields = {}
    for field in profile.get("fields", []):
        name = str(field.get("name") or "")
        if name in {"surname", "name", "second_name", "email", "languages_NAME", "timezone"}:
            fields[name] = field.get("display") or field.get("value") or ""
    report_values = {}
    for table in reports.get("tables", []):
        for row in table.get("rows", []):
            values = [" ".join(str(cell.get("text", "")).split()) for cell in row]
            if len(values) >= 2 and values[0].rstrip(":") in {"Общее время в системе", "Активен", "Присоединился"}:
                report_values[values[0].rstrip(":")] = values[1]
    return {
        "gradebook_row_count": snapshot.get("gradebook_row_count", 0),
        "course_count": snapshot.get("course_count", 0),
        "errors": snapshot.get("errors", []),
        "profile": fields,
        "report_facts": report_values,
    }


def _md(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _summary_markdown(summary: dict, courses: list[dict], grades: list[dict],
                      deadlines: list[dict], findings: list[dict]) -> str:
    current = [row for row in courses if row["lifecycle_status"] in {"активен", "активен без дат", "запланирован", "запланирован без дат"}]
    upcoming = [row for row in deadlines if row["status"] == "предстоит"]
    recent_grades = [
        row for row in grades
        if row["has_grade"] == "да" and row["academic_year"] in {"2025/2026", "2026/2027"}
    ]
    lines = [
        "# Данные Smart LMS и legacy LMS",
        "",
        f"> Снимок: **{_md(summary['as_of'])}**. Это оперативные данные LMS; при расхождении официальный протокол или выписка из зачетки имеют приоритет.",
        f"> Режим: **{summary.get('sync_mode', 'full')}**; ресурсов из кэша: **{summary.get('cached_resource_count', 0)}**. Даты проверки каждого ресурса — [lms_freshness.csv](data/lms_freshness.csv). Время снимка не означает повторную проверку кэшированных данных.",
        "",
        "## Кратко",
        "",
        f"- Доступно курсов: **{summary['course_count']}**; grade report доступен для **{summary['gradebook_course_count']}**.",
        f"- Записей с фактической оценкой: **{summary['graded_item_count']}** в **{summary['graded_course_count']}** курсах.",
        f"- Дедлайнов/action events: **{summary['deadline_count']}**, из них предстоящих **{summary['upcoming_deadline_count']}**, просроченных по флагу LMS **{summary['overdue_deadline_count']}**.",
        f"- Уведомления: **{summary['notification_count']}**, непрочитанных **{summary['unread_notification_count']}**. Сообщения: **{summary['message_count']}**.",
        f"- Legacy LMS: строк зачетки **{summary['legacy']['gradebook_row_count']}**, курсов **{summary['legacy']['course_count']}**, ошибок **{len(summary['legacy']['errors'])}**.",
        "",
        "## Требует внимания",
        "",
        "| Приоритет | Факт | Детали | Статус |",
        "| --- | --- | --- | --- |",
    ]
    for finding in findings:
        lines.append(f"| {_md(finding['priority'])} | {_md(finding['title'])} | {_md(finding['details'])} | {_md(finding['status'])} |")
    if not findings:
        lines.append("| — | Явных сигналов нет | — | — |")
    lines.extend(["", "## Актуальные и предстоящие курсы", "", "| ID | Курс | Учебный год | Статус |", "| ---: | --- | --- | --- |"])
    for row in current:
        lines.append(f"| {row['course_id']} | {_md(row['course_name'])} | {_md(row['academic_year'])} | {_md(row['lifecycle_status'])} |")
    lines.extend(["", "## Оценки 2025/2026–2026/2027, найденные в LMS", "", "| Курс | Элемент | Оценка | Диапазон | Отзыв |", "| --- | --- | ---: | --- | --- |"])
    for row in recent_grades:
        lines.append(f"| {_md(row['course_name'])} | {_md(row['item_name'])} | {_md(row['grade'])} | {_md(row['range'])} | {_md(row['feedback'])} |")
    if not recent_grades:
        lines.append("| — | Оценок не найдено | — | — | — |")
    lines.extend(["", "## Предстоящие дедлайны", "", "| Дата по Москве | Курс | Задание |", "| --- | --- | --- |"])
    for row in upcoming:
        lines.append(f"| {_md(row['due_at_moscow'])} | {_md(row['course_name'])} | {_md(row['event_name'])} |")
    if not upcoming:
        lines.append("| — | Предстоящих action events нет | — |")
    lines.extend([
        "", "## Полные реестры", "",
        "- [Курсы](data/lms_courses.csv)",
        "- [Активности курсов](data/lms_activities.csv)",
        "- [Все элементы оценивания](data/lms_grade_items.csv)",
        "- [Дедлайны](data/lms_deadlines.csv)",
        "- [Уведомления](data/lms_notifications.csv)",
        "- [Сообщения](data/lms_messages.csv)",
        "- [Выводы, требующие внимания](data/lms_findings.csv)",
        "- [Метаданные снимка](data/lms_snapshot.json)",
        "",
        "`Просрочено` означает флаг action event, который вернула Smart LMS. Сам по себе он не доказывает неявку на экзамен или отсутствие отправленной работы.",
        "",
    ])
    return "\n".join(lines)


def _activity_sections(page: dict, state: Any) -> list[dict]:
    sections = []
    seen = set()

    def append(section: dict, activities: list[dict]) -> None:
        unique = []
        for activity in activities:
            cmid = activity.get("cmid")
            key = ("id", str(cmid)) if cmid is not None else ("url", activity.get("url"))
            if key[1] and key in seen:
                continue
            if key[1]:
                seen.add(key)
            unique.append(activity)
        sections.append({**section, "activities": unique})

    if isinstance(state, dict):
        modules = state.get("cm", [])
        state_sections = state.get("section", [])
        if isinstance(modules, list) and isinstance(state_sections, list):
            modules = {str(cm["id"]): cm for cm in modules if isinstance(cm, dict) and cm.get("id") is not None}
            for section in state_sections:
                if not isinstance(section, dict):
                    continue
                ids = section.get("cmlist", [])
                ids = list(ids) if isinstance(ids, list) else []
                ids.extend(key for key, cm in modules.items() if str(cm.get("sectionid")) == str(section.get("id")))
                activities = []
                for key in ids:
                    cm = modules.get(str(key))
                    if cm is None:
                        continue
                    activities.append({"cmid": cm["id"], "name": cm.get("name", ""),
                                       "module": cm.get("module", ""), "url": cm.get("url", "")})
                append({"position": section.get("number", section.get("section", "")),
                        "name": section.get("title", "")}, activities)
    for section in page.get("sections", []):
        append(section, section.get("activities", []))
    return sections


def export_passport(input_root: Path, output_root: Path, workbook_data: Path | None = None) -> dict[str, Any]:
    input_root = input_root.resolve()
    output_root = output_root.resolve()
    snapshot = _read_json(input_root / "snapshot.json")
    if not isinstance(snapshot, dict):
        raise ValueError(f"Не найден корректный Smart LMS snapshot: {input_root / 'snapshot.json'}")
    if snapshot.get("errors"):
        raise ValueError(f"Smart LMS snapshot содержит ошибки: {snapshot['errors']}")
    if snapshot.get("scope") == "partial":
        raise ValueError("Частичный снимок нельзя выдавать за полный учебный паспорт")
    if any(row.get("errors") for row in snapshot.get("course_results", [])):
        raise ValueError("Smart LMS snapshot содержит частичные ошибки курсов")
    as_of = _snapshot_time(snapshot)
    course_source = _read_json(input_root / "courses" / "index.json", [])
    if not isinstance(course_source, list):
        raise ValueError("courses/index.json должен содержать список")

    courses: list[dict[str, Any]] = []
    activities: list[dict[str, Any]] = []
    grades: list[dict[str, Any]] = []
    deadlines: list[dict[str, Any]] = []
    course_names: dict[int, str] = {}

    for course in sorted(course_source, key=lambda row: (str(row.get("fullname", "")).casefold(), int(row.get("id") or 0))):
        course_id = int(course.get("id") or 0)
        name = " ".join(str(course.get("fullname") or course.get("fullnamedisplay") or "").split())
        year = _academic_year(name)
        modules = _modules(name)
        course_names[course_id] = name
        directory = input_root / "courses" / str(course_id)
        page = _read_json(directory / "page.json", {})
        gradebook = _read_json(directory / "grades.json", {})
        event_rows = _read_json(directory / "events.json", [])
        activity_count = 0
        for section in _activity_sections(page, _read_json(directory / "state.json", {})):
            for activity in section.get("activities", []):
                activity_count += 1
                activities.append({
                    "course_id": course_id, "course_name": name, "academic_year": year,
                    "modules": modules, "section_position": section.get("position", ""),
                    "section_name": section.get("name", ""), "module": activity.get("module", ""),
                    "cmid": activity.get("cmid", ""), "activity_name": activity.get("name", ""),
                    "url": activity.get("url", ""),
                })
        course_grade_count = 0
        for table in gradebook.get("tables", []):
            for row in table.get("rows", []):
                parsed = _grade_cells(row)
                if parsed is None:
                    continue
                grade_present = parsed["grade"] not in {"", "-"}
                if grade_present:
                    course_grade_count += 1
                item_links = row[0].get("links", []) if row else []
                grades.append({
                    "course_id": course_id, "course_name": name, "academic_year": year,
                    "modules": modules, "item_type": _item_type(parsed["item_name"]),
                    **parsed, "grade_numeric": _numeric_grade(parsed["grade"]),
                    "has_grade": "да" if grade_present else "нет",
                    "source_url": gradebook.get("url", ""),
                    "item_url": item_links[0].get("url", "") if item_links else "",
                })
        for event in event_rows if isinstance(event_rows, list) else []:
            event_course = event.get("course") or {}
            event_course_id = int(event_course.get("id") or course_id)
            due_timestamp = int(event.get("timestart") or 0)
            if due_timestamp > int(as_of.timestamp()):
                status = "предстоит"
            elif event.get("overdue"):
                status = "просрочено по Smart LMS"
            else:
                status = "прошло"
            deadlines.append({
                "course_id": event_course_id,
                "course_name": course_names.get(event_course_id) or event_course.get("fullname") or name,
                "academic_year": _academic_year(course_names.get(event_course_id) or event_course.get("fullname") or name),
                "modules": _modules(course_names.get(event_course_id) or event_course.get("fullname") or name),
                "activity_name": event.get("activityname", ""), "event_name": event.get("name", ""),
                "event_type": event.get("eventtype", ""),
                "due_at_moscow": _datetime_from_timestamp(due_timestamp), "status": status,
                "overdue": "да" if event.get("overdue") else "нет",
                "description": _clean_html(event.get("description", "")),
                "source_url": event.get("url") or event_course.get("viewurl") or course.get("viewurl", ""),
            })
        courses.append({
            "course_id": course_id, "course_name": name, "academic_year": year,
            "modules": modules, "course_kind": _course_kind(name),
            "start_date": _date_from_timestamp(course.get("startdate")),
            "end_date": _date_from_timestamp(course.get("enddate")),
            "lifecycle_status": _lifecycle(course, as_of, year),
            "visible": "да" if course.get("visible", True) else "нет",
            "hidden": "да" if course.get("hidden") else "нет",
            "progress": course.get("progress", ""), "activity_count": activity_count,
            "gradebook_available": "да" if gradebook.get("available") else "нет",
            "graded_item_count": course_grade_count, "source_url": course.get("viewurl", ""),
        })

    deadlines.sort(key=lambda row: (row["due_at_moscow"], row["course_name"], row["event_name"]))
    notifications_source = _read_json(input_root / "notifications.json", {})
    notification_data = notifications_source.get("data", notifications_source) if isinstance(notifications_source, dict) else {}
    notification_items = notification_data.get("notifications") or notification_data.get("items") or []
    notifications = []
    for item in notification_items:
        course = item.get("course") or {}
        notifications.append({
            "notification_id": item.get("id", ""),
            "created_at_moscow": _datetime_from_timestamp(item.get("timecreated") or item.get("timecreatedpretty")),
            "course_id": item.get("courseid") or course.get("id", ""),
            "course_name": course.get("fullname", ""), "subject": item.get("subject", ""),
            "text": _clean_html(item.get("fullmessagehtml") or item.get("fullmessage") or item.get("text", "")),
            "is_read": "нет" if item.get("read") is False else "да",
            "url": item.get("contexturl") or item.get("url", ""),
        })

    conversation_index = _read_json(input_root / "messages" / "conversations.json", [])
    conversation_meta = {int(row["id"]): row for row in conversation_index if row.get("id") is not None}
    messages = []
    for path in sorted((input_root / "messages" / "conversations").glob("*.json")):
        detail = _read_json(path, {})
        conversation_id = int(detail.get("id") or path.stem)
        meta = conversation_meta.get(conversation_id, {})
        members = {int(member["id"]): member.get("fullname", "") for member in detail.get("members", []) if member.get("id") is not None}
        member_names = [str(member.get("fullname", "")) for member in meta.get("members", []) if member.get("fullname")]
        for message in detail.get("messages", []):
            sender_id = int(message.get("useridfrom") or 0)
            raw_text = message.get("text", "")
            messages.append({
                "conversation_id": conversation_id, "message_id": message.get("id", ""),
                "created_at_moscow": _datetime_from_timestamp(message.get("timecreated")),
                "sender_id": sender_id, "sender_name": members.get(sender_id, "Текущий пользователь"),
                "conversation_with": "; ".join(member_names),
                "is_read": "да" if meta.get("isread", True) else "нет",
                "text": _clean_html(raw_text), "urls": " | ".join(_html_urls(raw_text)),
            })
    messages.sort(key=lambda row: (row["created_at_moscow"], row["message_id"]))

    unread = int(notification_data.get("unreadcount") or 0)
    legacy = _legacy_facts(input_root)
    graded = [row for row in grades if row["has_grade"] == "да"]
    findings: list[dict[str, Any]] = []
    finding_counter = 1

    for row in graded:
        grade_lower = str(row["grade"]).lower()
        feedback_lower = str(row["feedback"]).lower()
        if "незач" in grade_lower or "нет документ" in feedback_lower:
            findings.append({
                "finding_id": f"LMS-F{finding_counter:03d}", "priority": "высокий",
                "category": "оценивание", "title": row["item_name"],
                "details": f"{row['course_name']}: оценка «{row['grade']}»" + (f", отзыв «{row['feedback']}»" if row["feedback"] else ""),
                "status": "требует проверки/действия", "source": f"Smart LMS course {row['course_id']}",
            })
            finding_counter += 1

    upcoming = [row for row in deadlines if row["status"] == "предстоит"]
    if upcoming:
        first = upcoming[0]
        findings.append({
            "finding_id": f"LMS-F{finding_counter:03d}", "priority": "высокий",
            "category": "дедлайны", "title": f"Предстоящих дедлайнов: {len(upcoming)}",
            "details": f"Ближайший: {first['due_at_moscow']} — {first['event_name']}",
            "status": "предстоит", "source": f"Smart LMS course {first['course_id']}",
        })
        finding_counter += 1

    for row in graded:
        if row["academic_year"] == "2025/2026" and row["item_type"] in {"итог курса", "итог категории"} and row["grade_numeric"] != "":
            findings.append({
                "finding_id": f"LMS-F{finding_counter:03d}", "priority": "информационный",
                "category": "новый результат", "title": row["course_name"],
                "details": f"{row['item_name']}: {row['grade']} при диапазоне {row['range']}",
                "status": "найдено в LMS; сверить с официальной зачеткой", "source": f"Smart LMS course {row['course_id']}",
            })
            finding_counter += 1

    if unread:
        findings.append({
            "finding_id": f"LMS-F{finding_counter:03d}", "priority": "средний",
            "category": "уведомления", "title": f"Непрочитанных уведомлений: {unread}",
            "details": "Откройте реестр уведомлений", "status": "не прочитано", "source": "Smart LMS notifications",
        })

    summary = {
        "schema_version": 1, "as_of": as_of.astimezone(MOSCOW).isoformat(timespec="seconds"),
        "smart_lms_errors": snapshot.get("errors", []), "course_count": len(courses),
        "gradebook_course_count": sum(row["gradebook_available"] == "да" for row in courses),
        "grade_item_count": len(grades), "graded_item_count": len(graded),
        "graded_course_count": len({row["course_id"] for row in graded}),
        "activity_count": len(activities), "deadline_count": len(deadlines),
        "upcoming_deadline_count": len(upcoming),
        "overdue_deadline_count": sum(row["status"] == "просрочено по Smart LMS" for row in deadlines),
        "notification_count": len(notifications), "unread_notification_count": unread,
        "conversation_count": len(conversation_index), "message_count": len(messages),
        "legacy": legacy,
        "sync_mode": snapshot.get("mode", "full"),
        "cached_resource_count": snapshot.get("resources_cached", 0),
    }

    freshness = []
    for course_result in snapshot.get("course_results", []):
        for resource, state in course_result.get("resources", {}).items():
            checked = state.get("checked_at")
            checked = datetime.fromisoformat(checked).astimezone(MOSCOW).isoformat() if checked else ""
            freshness.append({"course_id": course_result["course_id"], "resource": resource,
                              "status": state["status"], "checked_at": checked,
                              "checked_at_source": state.get("checked_at_source", "")})

    data = output_root / "data"
    _write_csv(data / "lms_freshness.csv", ["course_id", "resource", "status", "checked_at", "checked_at_source"], freshness)
    _write_csv(data / "lms_courses.csv", COURSE_FIELDS, courses)
    _write_csv(data / "lms_activities.csv", ACTIVITY_FIELDS, activities)
    _write_csv(data / "lms_grade_items.csv", GRADE_FIELDS, grades)
    _write_csv(data / "lms_deadlines.csv", DEADLINE_FIELDS, deadlines)
    _write_csv(data / "lms_notifications.csv", NOTIFICATION_FIELDS, notifications)
    _write_csv(data / "lms_messages.csv", MESSAGE_FIELDS, messages)
    _write_csv(data / "lms_findings.csv", FINDING_FIELDS, findings)
    _write_json(data / "lms_snapshot.json", summary)
    (output_root / "LMS.md").write_text(
        _summary_markdown(summary, courses, grades, deadlines, findings), encoding="utf-8"
    )
    if workbook_data is not None:
        _write_json(workbook_data, {
            "summary": summary, "findings": findings, "courses": courses,
            "activities": activities, "grades": grades, "deadlines": deadlines,
            "messages": messages, "notifications": notifications,
        }, private=True)
    return summary
