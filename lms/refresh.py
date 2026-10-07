from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime
import json
from pathlib import Path
import shutil
import tempfile
import time

from .passport import export_passport
from .sync import read_json, write_json
from .passport import MOSCOW


def _rows(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def compare_passports(old: Path, new: Path) -> dict:
    changes = {}
    for filename, key, value in (
        ("lms_courses.csv", ("course_id",), "course_name"),
        ("lms_grade_items.csv", ("course_id", "item_name"), "grade"),
        ("lms_deadlines.csv", ("course_id", "source_url", "event_type"), "due_at_moscow"),
    ):
        before = {tuple(row[k] for k in key): row for row in _rows(old / "data" / filename)}
        after = {tuple(row[k] for k in key): row for row in _rows(new / "data" / filename)}
        changes[filename] = [
            {"key": list(identity), "before": before.get(identity, {}).get(value),
             "after": after.get(identity, {}).get(value)}
            for identity in sorted(before.keys() | after.keys())
            if before.get(identity, {}).get(value) != after.get(identity, {}).get(value)
        ]
    return changes


def export_tbank_tasks(root: Path, stage: Path) -> int:
    snapshot = read_json(root / "snapshot.json", {})
    fields = ["course", "stream_id", "unit_id", "task", "status", "soft_deadline_moscow",
              "hard_deadline_moscow", "score", "score_max", "task_count", "available_task_count", "checked_at"]
    rows = []
    for course in snapshot.get("course_results", []):
        for stream in course["streams"]:
            directory = root / "courses" / course["course_id"] / "streams" / stream["stream_id"]
            for unit in read_json(directory / "units/index.json", []):
                if unit.get("type") != "exam":
                    continue
                practice = read_json(directory / "units" / unit["id"] / "practice.json", {})
                info = practice.get("info", {})
                def date_value(key):
                    value = info.get(key)
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(MOSCOW).isoformat(timespec="minutes") if value else ""
                rows.append([course["title"], stream["stream_id"], unit["id"], unit.get("title", ""),
                             info.get("status", ""), date_value("softDeadline"), date_value("hardDeadline"),
                             info.get("studentScore"), info.get("scoreMax"), info.get("taskCount"),
                             len(practice.get("tasks") or []), datetime.fromisoformat(snapshot["finished_at"]).astimezone(MOSCOW).isoformat()])
    with (stage / "data/tbank_tasks.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n"); writer.writerow(fields); writer.writerows(rows)
    return len(rows)


def _summary(report, changes, summary):
    lines = ["# Последнее обновление кабинетов", "",
             f"Завершено: {report['finished_at']}. Режим Smart LMS: **{summary['sync_mode']}**.", "",
             "| Источник | Время, с | Результат |", "| --- | ---: | --- |"]
    for name, state in report["sources"].items():
        lines.append(f"| {name} | {state['seconds']} | успешно |")
    lines += ["", f"Smart LMS: {summary['course_count']} курсов, {summary['graded_item_count']} записей с оценкой, "
              f"{summary['upcoming_deadline_count']} предстоящих событий.",
              f"Ресурсов из кэша: **{summary['cached_resource_count']}**. "
              "Их даты проверки сохранены в [реестре свежести](data/lms_freshness.csv). "
              "Время этого запуска не является датой повторной проверки архива.", "",
              "## Изменения относительно предыдущего паспорта", ""]
    for filename, records in changes.items():
        lines.append(f"- `{filename}`: {len(records)} изменений.")
    lines += ["", "Подробности: [изменения](data/refresh_changes.json), [оценки и дедлайны](LMS.md).", "",
              "Исторические документы, официальные результаты и вручную составленные сводки "
              "не переписываются автоматически. Исчезновение курса или оценки из LMS не означает отмену результата.", ""]
    return "\n".join(lines)


def refresh(lms_root: Path, output: Path, steps: dict, progress=print, cancel_event=None, tbank_root=None) -> dict:
    log = lms_root / "refresh-run.json"
    write_json(log, {"status": "running", "started_at": datetime.now(MOSCOW).isoformat()})
    try:
        return _refresh(lms_root, output, steps, progress, cancel_event, tbank_root)
    except BaseException as exc:
        report = read_json(log, {})
        if report.get("status") != "failed":
            report.update(status="cancelled" if isinstance(exc, KeyboardInterrupt) else "failed",
                          error=type(exc).__name__, finished_at=datetime.now(MOSCOW).isoformat())
            write_json(log, report)
        raise


def _refresh(lms_root: Path, output: Path, steps: dict, progress, cancel_event, tbank_root) -> dict:
    started = time.perf_counter()
    report = {"started_at": datetime.now(MOSCOW).isoformat(), "sources": {}, "status": "running"}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".passport-refresh-", dir=output.parent) as directory:
        stage = Path(directory)

        def run(name, step):
            clock = time.perf_counter()
            try:
                code = step(stage)
                return name, {"code": int(code), "seconds": round(time.perf_counter() - clock, 3)}
            except Exception as exc:
                return name, {"code": 1, "seconds": round(time.perf_counter() - clock, 3), "error": type(exc).__name__}

        executor = ThreadPoolExecutor(max_workers=len(steps))
        try:
            futures = [executor.submit(run, name, step) for name, step in steps.items()]
            for future in as_completed(futures):
                name, state = future.result()
                report["sources"][name] = state
                progress(f"{name}: {'готово' if state['code'] == 0 else 'ошибка'}, {state['seconds']} с")
        except BaseException:
            if cancel_event is not None:
                cancel_event.set()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        report["finished_at"] = datetime.now(MOSCOW).isoformat()
        report["sources"] = dict(sorted(report["sources"].items()))
        if any(state["code"] for state in report["sources"].values()):
            report["status"] = "failed"
            write_json(lms_root / "refresh-run.json", report)
            raise ValueError("Не все источники обновлены: паспорт сохранен без изменений; подробности в lms-data/refresh-run.json")

        clock = time.perf_counter()
        summary = export_passport(lms_root, stage)
        if tbank_root is not None:
            report["tbank_task_count"] = export_tbank_tasks(tbank_root, stage)
        changes = compare_passports(output, stage)
        (stage / "data" / "refresh_changes.json").write_text(json.dumps(changes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report.update(status="complete", build_seconds=round(time.perf_counter() - clock, 3),
                      seconds=round(time.perf_counter() - started, 3),
                      finished_at=datetime.now(MOSCOW).isoformat())
        (stage / "REFRESH.md").write_text(_summary(report, changes, summary), encoding="utf-8")
        (stage / "data" / "refresh_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        backup = stage / ".backup"
        targets = [path for path in stage.rglob("*") if path.is_file()]
        installed = []
        try:
            for path in targets:
                relative = path.relative_to(stage)
                target = output / relative
                if target.exists():
                    saved = backup / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, saved)
                target.parent.mkdir(parents=True, exist_ok=True)
                path.replace(target)
                installed.append(relative)
        except BaseException:
            for relative in reversed(installed):
                if (backup / relative).exists():
                    (backup / relative).replace(output / relative)
                else:
                    (output / relative).unlink(missing_ok=True)
            raise
    write_json(lms_root / "refresh-run.json", report)
    progress(f"Сводка готова: сборка {report['build_seconds']} с, всего {report['seconds']} с")
    return report
