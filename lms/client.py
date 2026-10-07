from __future__ import annotations

import json
import re
from typing import Any

import requests

from .auth import BASE_URL, AuthError, HSEAuthSession
from .parsers import parse_course_links, parse_course_page, parse_notification_page, parse_tables


class MoodleAPIError(RuntimeError):
    def __init__(self, method: str, detail: str, code: str | None = None) -> None:
        super().__init__(f"{method}: {detail}")
        self.method = method
        self.detail = detail
        self.code = code


class SmartLMSClient:
    def __init__(self, auth: HSEAuthSession, landing_html: str | None = None) -> None:
        self.auth = auth
        self.session = auth.session
        self.timeout = auth.timeout
        self.sesskey: str | None = None
        self.userid: int | None = None
        if landing_html:
            self._read_bootstrap(landing_html)

    def _read_bootstrap(self, html: str) -> None:
        sesskey = re.search(r'"sesskey"\s*:\s*"([^"]+)"', html)
        userid = re.search(r'"userid"\s*:\s*(\d+)', html)
        if sesskey:
            self.sesskey = sesskey.group(1)
        if userid:
            self.userid = int(userid.group(1))

    def bootstrap(self) -> requests.Response:
        response = self.session.get(f"{BASE_URL}/my/courses.php", timeout=self.timeout)
        self._read_bootstrap(response.text)
        if not self.sesskey or not self.userid:
            raise AuthError("Не удалось извлечь sesskey/userid из Smart LMS")
        return response

    def ajax(self, method: str, args: dict[str, Any]) -> Any:
        if not self.sesskey:
            self.bootstrap()
        response = self.session.post(
            f"{BASE_URL}/lib/ajax/service.php",
            params={"sesskey": self.sesskey, "info": method},
            json=[{"index": 0, "methodname": method, "args": args}],
            timeout=self.timeout,
        )
        response.raise_for_status()
        try:
            document = response.json()
        except requests.JSONDecodeError as exc:
            raise MoodleAPIError(method, "сервер вернул не JSON") from exc
        envelope = document[0] if isinstance(document, list) and document else document
        if not isinstance(envelope, dict):
            raise MoodleAPIError(method, "неожиданный формат ответа")
        if envelope.get("error") or envelope.get("exception"):
            exception = envelope.get("exception")
            if not isinstance(exception, dict):
                exception = envelope
            detail = str(exception.get("message") or exception.get("error") or "ошибка Moodle API")
            code = exception.get("errorcode")
            raise MoodleAPIError(method, detail, str(code) if code else None)
        data = envelope.get("data", envelope)
        if isinstance(data, str) and data[:1] in {"{", "["}:
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                pass
        return data

    def courses(self, landing_html: str | None = None) -> list[dict]:
        try:
            data = self.ajax(
                "core_course_get_enrolled_courses_by_timeline_classification",
                {
                    "offset": 0,
                    "limit": 0,
                    "classification": "all",
                    "sort": "fullname",
                    "customfieldname": "",
                    "customfieldvalue": "",
                },
            )
            if isinstance(data, dict) and isinstance(data.get("courses"), list):
                return data["courses"]
        except MoodleAPIError:
            pass
        if landing_html is None:
            landing_html = self.bootstrap().text
        return parse_course_links(landing_html, BASE_URL)

    def course_state(self, course_id: int) -> Any:
        return self.ajax("core_courseformat_get_state", {"courseid": course_id})

    def course_page(self, course_id: int) -> dict:
        url = f"{BASE_URL}/course/view.php?id={course_id}"
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return parse_course_page(response.text, response.url)

    def course_events(self, course_id: int, page_size: int = 50) -> list[dict]:
        events: list[dict] = []
        after_event_id = 0
        seen_last_ids: set[int] = set()
        for _ in range(100):
            data = self.ajax(
                "core_calendar_get_action_events_by_course",
                {
                    "courseid": course_id,
                    "timesortfrom": 0,
                    "limitnum": page_size,
                    "aftereventid": after_event_id,
                },
            )
            page = data.get("events", []) if isinstance(data, dict) else []
            events.extend(page)
            if len(page) < page_size:
                break
            last_id = int(data.get("lastid") or 0)
            if not last_id or last_id in seen_last_ids:
                break
            seen_last_ids.add(last_id)
            after_event_id = last_id
        return events

    def grades(self, course_id: int) -> dict:
        url = f"{BASE_URL}/grade/report/user/index.php?id={course_id}"
        response = self.session.get(url, timeout=self.timeout)
        if response.status_code == 404:
            return {"url": response.url, "available": False, "status_code": 404, "tables": []}
        response.raise_for_status()
        return {
            "url": response.url,
            "available": True,
            "status_code": response.status_code,
            "tables": parse_tables(response.text, response.url),
        }

    def notifications(self) -> dict:
        if self.userid is None:
            self.bootstrap()
        api_error = None
        try:
            notifications: list[dict] = []
            unread_count = 0
            page_size = 100
            for offset in range(0, 100_000, page_size):
                data = self.ajax(
                    "message_popup_get_popup_notifications",
                    {"useridto": self.userid, "limit": page_size, "offset": offset},
                )
                page = data.get("notifications", []) if isinstance(data, dict) else []
                if isinstance(data, dict):
                    unread_count = int(data.get("unreadcount") or 0)
                notifications.extend(page)
                if len(page) < page_size:
                    break
            return {
                "source": "api",
                "data": {"notifications": notifications, "unreadcount": unread_count},
            }
        except MoodleAPIError as exc:
            api_error = {"code": exc.code, "message": exc.detail}
        url = f"{BASE_URL}/message/output/popup/notifications.php"
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        parsed = parse_notification_page(response.text, response.url)
        parsed.update({"source": "html", "url": response.url, "api_error": api_error})
        return parsed

    def conversations(self, page_size: int = 100) -> list[dict]:
        if self.userid is None:
            self.bootstrap()
        conversations: list[dict] = []
        offset = 0
        for _ in range(100):
            data = self.ajax(
                "core_message_get_conversations",
                {
                    "userid": self.userid,
                    "limitfrom": offset,
                    "limitnum": page_size,
                    "mergeself": False,
                },
            )
            page = data.get("conversations", []) if isinstance(data, dict) else []
            conversations.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        return conversations

    def conversation_messages(self, conversation_id: int, page_size: int = 100) -> dict:
        if self.userid is None:
            self.bootstrap()
        messages: list[dict] = []
        members: list[dict] = []
        offset = 0
        for _ in range(1000):
            data = self.ajax(
                "core_message_get_conversation_messages",
                {
                    "currentuserid": self.userid,
                    "convid": conversation_id,
                    "limitfrom": offset,
                    "limitnum": page_size,
                    "newest": False,
                    "timefrom": 0,
                },
            )
            page = data.get("messages", []) if isinstance(data, dict) else []
            if not members and isinstance(data, dict):
                members = data.get("members", []) or []
            messages.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        return {"id": conversation_id, "members": members, "messages": messages}

    def clone(self) -> "SmartLMSClient":
        auth = HSEAuthSession(timeout=self.timeout)
        auth.session.headers.update(self.session.headers)
        auth.session.cookies.update(self.session.cookies)
        client = SmartLMSClient(auth)
        client.sesskey = self.sesskey
        client.userid = self.userid
        return client
