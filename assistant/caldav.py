from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

from .corpus import Chunk
from .integrations import event_uid, to_ics, upcoming

STATE_PATH = Path(__file__).resolve().parents[1] / "logs" / "calendar-state.json"
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}


class CalDAV:
    def __init__(self, url: str, user: str, password: str):
        self.url = url
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def request(self, method: str, url: str, body: str | None = None, headers: dict | None = None) -> bytes:
        req = urllib.request.Request(url, data=body.encode("utf-8") if body is not None else None, method=method)
        req.add_header("Authorization", self.auth)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if method == "DELETE" and e.code == 404:
                return b""
            raise

    def propfind(self, url: str, props: str, depth: str = "0") -> ET.Element:
        body = f'<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>{props}</d:prop></d:propfind>'
        out = self.request("PROPFIND", url, body, {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"})
        return ET.fromstring(out)

    def href(self, root: ET.Element, path: str) -> str:
        node = root.find(f".//{path}/d:href", NS)
        if node is None or not node.text:
            raise RuntimeError(f"CalDAV: сервер не вернул {path}")
        return urljoin(self.url, node.text.strip())

    def find_calendar(self) -> str:
        principal = self.href(self.propfind(self.url, "<d:current-user-principal/>"), "d:current-user-principal")
        home = self.href(self.propfind(principal, "<c:calendar-home-set/>"), "c:calendar-home-set")
        listing = self.propfind(home, "<d:displayname/><d:resourcetype/><c:supported-calendar-component-set/>", "1")
        for resp in listing.findall("d:response", NS):
            if resp.find(".//d:resourcetype/c:calendar", NS) is None:
                continue
            comps = [c.get("name") for c in resp.findall(".//c:supported-calendar-component-set/c:comp", NS)]
            if comps and "VEVENT" not in comps:
                continue
            return urljoin(self.url, resp.findtext("d:href", "", NS).strip())
        raise RuntimeError("CalDAV: календарь для событий не найден")

    def put(self, calendar: str, uid: str, ics: str) -> None:
        self.request("PUT", urljoin(calendar, f"{uid}.ics"), ics, {"Content-Type": "text/calendar; charset=utf-8"})

    def delete(self, calendar: str, uid: str) -> None:
        self.request("DELETE", urljoin(calendar, f"{uid}.ics"))


def event_hash(ics: str) -> str:
    stable = "\r\n".join(line for line in ics.split("\r\n") if not line.startswith("DTSTAMP:"))
    return hashlib.sha1(stable.encode("utf-8")).hexdigest()


def plan(chunks: list[Chunk], today: date, state: dict) -> tuple[dict[str, str], list[str]]:
    known = {event_uid(c) for c in chunks if "due" in c.meta}
    events = {event_uid(c): to_ics([c]) for c in upcoming(chunks, today)}
    put = {uid: ics for uid, ics in events.items() if state.get(uid) != event_hash(ics)}
    gone = [uid for uid in state if uid not in known]
    return put, gone


def sync(chunks: list[Chunk], today: date, client: CalDAV | None = None,
         state_path: Path = STATE_PATH) -> tuple[int, int]:
    saved = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    events = saved.get("events", {})
    put, gone = plan(chunks, today, events)
    if not put and not gone:
        return 0, 0
    client = client or CalDAV("https://caldav.yandex.ru/", os.environ["SMTP_USER"], os.environ["CALDAV_PASSWORD"])
    calendar = saved.get("calendar") or client.find_calendar()
    for uid, ics in put.items():
        client.put(calendar, uid, ics)
        events[uid] = event_hash(ics)
    for uid in gone:
        client.delete(calendar, uid)
        events.pop(uid, None)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"calendar": calendar, "events": events}, indent=1), encoding="utf-8")
    return len(put), len(gone)
