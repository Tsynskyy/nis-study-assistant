from __future__ import annotations

import hashlib
import os
import smtplib
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

from .corpus import Chunk


def upcoming(chunks: list[Chunk], today: date, days: int | None = None) -> list[Chunk]:
    out = []
    for c in chunks:
        if "due" not in c.meta:
            continue
        d = datetime.fromisoformat(c.meta["due"]).date()
        if d >= today and (days is None or d < today + timedelta(days=days)):
            out.append(c)
    return sorted(out, key=lambda c: datetime.fromisoformat(c.meta["due"]))


def event_uid(c: Chunk) -> str:
    return hashlib.sha1(f"{c.meta['course']}|{c.meta['event']}".encode()).hexdigest()


def ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line: str) -> str:
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 74:
            out.append(cur)
            cur = " "
        cur += ch
    return "\r\n".join(out + [cur])


def to_ics(deadlines: list[Chunk], now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//nis-study-assistant//RU",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:Учебные дедлайны"]
    for c in deadlines:
        due = datetime.fromisoformat(c.meta["due"]).astimezone(timezone.utc)
        desc = c.meta["course"] + ("\n" + c.meta["url"] if c.meta.get("url") else "")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{event_uid(c)}@nis-study-assistant",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{(due - timedelta(minutes=30)).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{due.strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{ics_escape(c.meta['event'])}",
            f"DESCRIPTION:{ics_escape(desc)}",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{ics_escape(c.meta['event'])}",
            "TRIGGER:-P1D",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(line) for line in lines) + "\r\n"


def digest(chunks: list[Chunk], today: date, alerts: list[str], days: int = 7) -> tuple[str, str]:
    soon = upcoming(chunks, today, days)
    subject = f"Учебный дайджест на {today:%d.%m}: дедлайнов {len(soon)}"
    lines = [f"Дедлайны на {days} дней с {today:%d.%m.%Y}:"]
    lines += [f"- {datetime.fromisoformat(c.meta['due']):%d.%m %H:%M} {c.meta['event']} ({c.meta['course']})"
              for c in soon] or ["- нет"]
    if alerts:
        lines += ["", "Алерты мониторинга:"] + [f"- {a}" for a in alerts]
    return subject, "\n".join(lines) + "\n"


def send_mail(subject: str, body: str) -> str:
    user = os.environ["SMTP_USER"]
    to = os.environ.get("DIGEST_TO", user)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(body)
    host = os.environ.get("SMTP_HOST", "smtp.yandex.ru")
    port = int(os.environ.get("SMTP_PORT", "465"))
    cls = smtplib.SMTP_SSL if os.environ.get("SMTP_SSL", "1") != "0" else smtplib.SMTP
    with cls(host, port, timeout=30) as smtp:
        smtp.login(user, os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)
    return to
