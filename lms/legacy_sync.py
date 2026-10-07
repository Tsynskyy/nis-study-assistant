from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .legacy_client import LegacyLMSClient
from .sync import private_dir, write_bytes, write_json


def sync_legacy(client: LegacyLMSClient, output: Path) -> dict:
    started = datetime.now(timezone.utc)
    output = output.resolve()
    private_dir(output)
    errors = []

    gradebook_rows = 0
    try:
        gradebook, xls = client.gradebook()
        gradebook_rows = int(gradebook.get("xls_data_row_count") or gradebook.get("row_count") or 0)
        write_bytes(output / "gradebook.xls", xls)
        write_json(output / "gradebook.json", gradebook)
    except Exception as exc:
        errors.append(f"gradebook: {type(exc).__name__}: {exc}")

    pages = {
        "profile.json": "/student.php?ctg=personal&op=account",
        "status.json": "/student.php?ctg=personal&op=status",
        "reports.json": "/student.php?ctg=statistics",
        "notices.json": "/student.php?ctg=notices",
        "messages.json": "/student.php?ctg=messages",
    }
    for filename, path in pages.items():
        try:
            write_json(output / filename, client.page(path))
        except Exception as exc:
            errors.append(f"{filename}: {type(exc).__name__}: {exc}")

    courses = []
    try:
        courses, _ = client.courses()
        write_json(output / "courses" / "index.json", courses)
        for course in courses:
            try:
                write_json(output / "courses" / f"{course['id']}.json", client.page(f"/student.php?lessons_ID={course['id']}"))
            except Exception as exc:
                errors.append(f"course {course['id']}: {type(exc).__name__}: {exc}")
    except Exception as exc:
        errors.append(f"courses: {type(exc).__name__}: {exc}")

    finished = datetime.now(timezone.utc)
    report = {
        "schema_version": 1,
        "source": "https://lms.hse.ru/",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "output": str(output),
        "gradebook_row_count": gradebook_rows,
        "course_count": len(courses),
        "errors": errors,
    }
    write_json(output / "snapshot.json", report)
    return report
