from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
import threading
from typing import Any, Callable

import requests

from .client import SmartLMSClient


def _safe_component(value: object) -> str:
    text = str(value)
    return "".join(character if character.isalnum() or character in "-_" else "_" for character in text)


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_json(path: Path, value: Any) -> None:
    private_dir(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(path)
    path.chmod(0o600)


def write_bytes(path: Path, value: bytes) -> None:
    private_dir(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)
    temporary.replace(path)
    path.chmod(0o600)


@dataclass
class SyncOptions:
    output: Path
    workers: int = 4
    include_messages: bool = True
    course_ids: set[int] | None = None
    max_courses: int | None = None
    only_missing: bool = False
    mode: str = "full"
    archive_max_age_hours: float = 168
    watch_course_ids: set[int] | None = None
    progress: Callable[[str], None] | None = None
    cancel_event: threading.Event | None = None


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def cacheable_archive(course: dict, directory: Path, options: SyncOptions, now: datetime) -> bool:
    if options.mode != "fast" or _course_id(course) in (options.watch_course_ids or set()):
        return False
    end = int(course.get("enddate") or 0)
    if not end or end >= now.timestamp():
        return False
    year = now.year if now.month >= 9 else now.year - 1
    if f"{year}/{year + 1}" in str(course.get("fullname", "")):
        return False
    previous = read_json(directory / "course.json", {})
    if not isinstance(previous, dict) or any(previous.get(k) != course.get(k) for k in ("id", "fullname", "startdate", "enddate", "timemodified")):
        return False
    events = read_json(directory / "events.json")
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        return False
    try:
        return not any(int(event.get("timestart") or 0) > now.timestamp() for event in events)
    except (TypeError, ValueError):
        return False


def cached_resource(path: Path, previous: dict, max_age_hours: float, now: datetime) -> dict | None:
    if not path.exists() or previous.get("status") == "error":
        return None
    value = read_json(path)
    if value is None:
        return None
    if path.name in {"page.json", "grades.json"}:
        field = "sections" if path.name == "page.json" else "tables"
        if not isinstance(value, dict) or not isinstance(value.get(field), list):
            return None
    if path.name == "events.json" and (not isinstance(value, list) or any(not isinstance(event, dict) for event in value)):
        return None
    checked_at = previous.get("checked_at")
    source = previous.get("checked_at_source", "request")
    if not checked_at:
        checked_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        source = "file_mtime"
    try:
        age = (now - datetime.fromisoformat(checked_at)).total_seconds()
    except (ValueError, TypeError):
        return None
    if not 0 <= age < max_age_hours * 3600:
        return None
    return {"status": "cached", "checked_at": checked_at, "checked_at_source": source,
            "seconds": 0, "attempts": 0}


def _course_id(course: dict) -> int | None:
    value = course.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sync_one_course(client: SmartLMSClient, output: Path, course: dict,
                     options: SyncOptions, previous: dict, cancelled: threading.Event) -> dict:
    course_id = _course_id(course)
    if course_id is None:
        return {"course_id": None, "errors": ["У курса отсутствует числовой id"],
                "resources": {}, "counts": {}, "seconds": 0}
    directory = output / "courses" / str(course_id)
    private_dir(directory)
    now = datetime.now(timezone.utc)
    archive = cacheable_archive(course, directory, options, now)
    write_json(directory / "course.json", course)
    errors = []
    counts: dict[str, Any] = {}
    resources = {}
    started = time.perf_counter()
    operations = [
        ("state", "state.json", lambda: client.course_state(course_id)),
        ("page", "page.json", lambda: client.course_page(course_id)),
        ("events", "events.json", lambda: client.course_events(course_id)),
        ("grades", "grades.json", lambda: client.grades(course_id)),
    ]
    for label, filename, operation in operations:
        if cancelled.is_set():
            raise RuntimeError("Сбор отменен")
        destination = directory / filename
        cached = cached_resource(destination, previous.get("resources", {}).get(label, {}),
                                 options.archive_max_age_hours, now) if archive else None
        if options.only_missing and destination.exists():
            cached = cached_resource(destination, {}, float("inf"), now)
        if cached:
            counts[label] = "cached"
            resources[label] = cached
            continue
        clock = time.perf_counter()
        attempts = 0
        try:
            for attempt in range(3):
                if cancelled.is_set():
                    raise RuntimeError("Сбор отменен")
                attempts += 1
                try:
                    value = operation()
                    break
                except (requests.Timeout, requests.ConnectionError):
                    if attempt == 2:
                        raise
                    cancelled.wait(1 + attempt * 2)
            write_json(destination, value)
            resources[label] = {"status": "fetched", "checked_at": datetime.now(timezone.utc).isoformat(),
                                "checked_at_source": "request"}
            if isinstance(value, list):
                counts[label] = len(value)
            elif isinstance(value, dict):
                if label == "page":
                    counts[label] = sum(len(section.get("activities", [])) for section in value.get("sections", []))
                elif label == "grades":
                    counts[label] = len(value.get("tables", []))
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}")
            resources[label] = {"status": "error", "checked_at": None}
        resources[label].update(seconds=round(time.perf_counter() - clock, 3), attempts=attempts)
    return {"course_id": course_id, "counts": counts, "errors": errors,
            "resources": resources, "seconds": round(time.perf_counter() - started, 3)}


def sync_all(client: SmartLMSClient, landing_html: str, options: SyncOptions) -> dict:
    if options.mode not in {"fast", "full"} or not math.isfinite(options.archive_max_age_hours) or options.archive_max_age_hours <= 0:
        raise ValueError("Некорректный режим или срок хранения архива")
    if options.mode == "fast" and (options.course_ids or options.max_courses is not None or options.only_missing):
        raise ValueError("Быстрый режим нельзя совмещать с частичной выгрузкой/only-missing")
    started = datetime.now(timezone.utc)
    output = options.output.resolve()
    private_dir(output)
    previous_snapshot = read_json(output / "snapshot.json", {})
    previous = {row["course_id"]: row for row in previous_snapshot.get("course_results", [])}
    write_json(output / "snapshot.json", {"started_at": started.isoformat(), "status": "running",
               "errors": ["Сбор не завершен"], "course_results": list(previous.values())})
    courses = client.courses(landing_html)
    if options.course_ids:
        courses = [course for course in courses if _course_id(course) in options.course_ids]
    if options.max_courses is not None:
        courses = courses[: options.max_courses]
    write_json(output / "courses" / "index.json", courses)

    course_results = []
    workers = max(1, min(options.workers, 8))
    local = threading.local()
    clients = []
    cancelled = options.cancel_event or threading.Event()

    def run(course):
        if not hasattr(local, "client"):
            local.client = client.clone()
            clients.append(local.client)
        return _sync_one_course(local.client, output, course, options,
                                previous.get(_course_id(course), {}), cancelled)

    executor = ThreadPoolExecutor(max_workers=workers)
    remaining = iter(courses)
    pending = set()
    try:
        for course in list(courses)[:workers]:
            pending.add(executor.submit(run, next(remaining)))
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                result = future.result()
                course_results.append(result)
                if options.progress:
                    fetched = sum(r["status"] == "fetched" for r in result["resources"].values())
                    options.progress(f"Smart LMS {len(course_results)}/{len(courses)}: "
                                     f"курс {result['course_id']}, запросов ресурсов {fetched}, "
                                     f"ошибок {len(result['errors'])}")
                course = next(remaining, None)
                if course is not None:
                    pending.add(executor.submit(run, course))
    finally:
        cancelled.set()
        executor.shutdown(wait=True, cancel_futures=True)
        for worker_client in clients:
            worker_client.session.close()
    course_results.sort(key=lambda item: item.get("course_id") or -1)

    global_errors = []
    try:
        notifications = client.notifications()
        write_json(output / "notifications.json", notifications)
    except Exception as exc:
        global_errors.append(f"notifications: {type(exc).__name__}")

    conversation_count = 0
    message_count = 0
    if options.include_messages:
        try:
            conversations = client.conversations()
            conversation_count = len(conversations)
            write_json(output / "messages" / "conversations.json", conversations)
            for conversation in conversations:
                conversation_id = conversation.get("id")
                if conversation_id is None:
                    continue
                detail = client.conversation_messages(int(conversation_id))
                message_count += len(detail.get("messages", []))
                write_json(
                    output / "messages" / "conversations" / f"{_safe_component(conversation_id)}.json",
                    detail,
                )
        except Exception as exc:
            global_errors.append(f"messages: {type(exc).__name__}")

    finished = datetime.now(timezone.utc)
    report = {
        "schema_version": 1,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "output": str(output),
        "course_count": len(courses),
        "conversation_count": conversation_count,
        "message_count": message_count,
        "course_results": course_results,
        "errors": global_errors,
        "status": "complete",
        "mode": options.mode,
        "scope": "partial" if options.course_ids or options.max_courses is not None else "all",
        "workers": workers,
        "resources_fetched": sum(r["status"] == "fetched" for c in course_results for r in c["resources"].values()),
        "resources_cached": sum(r["status"] == "cached" for c in course_results for r in c["resources"].values()),
    }
    write_json(output / "snapshot.json", report)
    return report
