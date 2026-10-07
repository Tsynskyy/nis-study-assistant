from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

MARKDOWN_SOURCES = ("AUTUMN-2026.md", "SRE-2026.md", "LMS.md")
DEADLINES_CSV = "data/lms_deadlines.csv"
COURSES_CSV = "data/autumn_2026_courses.csv"
TBANK_CSV = "data/tbank_tasks.csv"

MAX_CHUNK_CHARS = 900


@dataclass
class Chunk:
    id: str
    source: str
    title: str
    text: str
    meta: dict = field(default_factory=dict)


def data_dir() -> Path:
    default = Path(__file__).resolve().parents[2] / "hse" / "student-passport"
    return Path(os.environ.get("HSE_PASSPORT_DIR", default))


def split_markdown(name: str, text: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    heads: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        buf.clear()
        if not body:
            return
        title = " / ".join(heads) or name
        for part in _pack(body.split("\n\n")):
            chunks.append(Chunk(f"{name}#{len(chunks)}", name, title, f"{title}\n{part}"))

    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            flush()
            level = len(m.group(1))
            heads[:] = heads[: level - 1] + [m.group(2).strip()]
        else:
            buf.append(line)
    flush()
    return chunks


def _pack(paragraphs: list[str]) -> list[str]:
    out, cur = [], ""
    for p in (p.strip() for p in paragraphs):
        if not p:
            continue
        if cur and len(cur) + len(p) > MAX_CHUNK_CHARS:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return out


def load_deadlines(path: Path) -> list[Chunk]:
    chunks = []
    with path.open(encoding="utf-8") as fh:
        for i, r in enumerate(csv.DictReader(fh)):
            text = (
                f"Дедлайн: {r['event_name']}\nКурс: {r['course_name']}\n"
                f"Срок: {r['due_at_moscow']}\nСтатус: {r['status']}\n{r['description']}"
            )
            chunks.append(Chunk(
                f"deadline#{i}", DEADLINES_CSV, r["course_name"], text,
                {"due": r["due_at_moscow"], "event": r["event_name"],
                 "course": r["course_name"], "url": r["source_url"]},
            ))
    return chunks


def load_tbank_tasks(path: Path) -> list[Chunk]:
    chunks = []
    with path.open(encoding="utf-8") as fh:
        for i, r in enumerate(csv.DictReader(fh)):
            due = r["hard_deadline_moscow"] or r["soft_deadline_moscow"]
            if not due:
                continue
            score = f"{r['score']}/{r['score_max']}" if r["score"] else "нет оценки"
            text = (
                f"Дедлайн: {r['task']}\nКурс: {r['course']} (Т-Образование)\n"
                f"Срок: {due}\nСтатус: {r['status']}, баллы: {score}"
            )
            chunks.append(Chunk(
                f"tbank#{i}", TBANK_CSV, r["course"], text,
                {"due": due, "event": r["task"], "course": r["course"], "url": ""},
            ))
    return chunks


def load_courses(path: Path) -> list[Chunk]:
    chunks = []
    keys = ("relation", "status", "schedule", "teacher", "immediate_action", "notes")
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            text = f"Курс: {r['course_name']}\n" + "\n".join(f"{k}: {r[k]}" for k in keys if r.get(k))
            chunks.append(Chunk(f"course#{r['record_id']}", COURSES_CSV, r["course_name"], text))
    return chunks


def load_corpus(root: Path | None = None) -> list[Chunk]:
    root = root or data_dir()
    chunks: list[Chunk] = []
    for name in MARKDOWN_SOURCES:
        p = root / name
        if p.exists():
            chunks += split_markdown(name, p.read_text(encoding="utf-8"))
    if (root / DEADLINES_CSV).exists():
        chunks += load_deadlines(root / DEADLINES_CSV)
    if (root / TBANK_CSV).exists():
        chunks += load_tbank_tasks(root / TBANK_CSV)
    if (root / COURSES_CSV).exists():
        chunks += load_courses(root / COURSES_CSV)
    if not chunks:
        raise FileNotFoundError(f"В {root} нет источников; задайте HSE_PASSPORT_DIR")
    return chunks
