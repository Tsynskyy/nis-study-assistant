from __future__ import annotations

import getpass
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .auth import (
    AuthError,
    _config_home,
    _private_json,
    _state_home,
    clear_credential,
    load_credential,
    store_credential,
)
from .sync import private_dir, write_json


BASE_URL = "https://edu.tbank.ru"
SESSION_FILENAME = "tbank-session.json"
SERVICE_NAME = "hse-edu-parser-tbank"
UUID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)


def _uuid(value: object) -> str:
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise ValueError("Некорректный идентификатор курса или урока Т-Образования")
    return value


def session_path() -> Path:
    return _state_home() / "hse-edu-parser" / SESSION_FILENAME


def config_path() -> Path:
    return _config_home() / "hse-edu-parser" / "tbank.json"


def load_email() -> str | None:
    path = config_path()
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    email = document.get("email") if isinstance(document, dict) else None
    return email if isinstance(email, str) and email else None


def save_email(email: str) -> None:
    _private_json(config_path(), {"email": email})


def validate_email(email: str) -> str:
    email = email.strip()
    if not re.fullmatch(r"[^\s@<>]+@[^\s@<>.]+(?:\.[^\s@<>.]+)+", email):
        raise AuthError(
            "Укажите настоящий email вашего аккаунта Т-Образования после --email. "
            "ВАША_ПОЧТА и ADDRESS в примерах — заглушки; замените их своим адресом."
        )
    return email


def prompt_password(email: str) -> str:
    password = getpass.getpass(f"Пароль Т-Образования для {email}: ")
    if not password:
        raise AuthError("Пустой пароль Т-Образования")
    return password


def load_password(email: str) -> str | None:
    return os.environ.get("TBANK_EDU_PASSWORD") or load_credential(SERVICE_NAME, email)


def store_password(email: str, password: str) -> None:
    store_credential(SERVICE_NAME, email, password, f"Т-Образование ({email})")


def clear_password(email: str) -> bool:
    return clear_credential(SERVICE_NAME, email)


class TBankTLSAdapter(requests.adapters.HTTPAdapter):

    def cert_verify(self, conn, url, verify, cert):
        parsed = urlparse(url)
        if (parsed.scheme, parsed.hostname, parsed.port) != ("https", "edu.tbank.ru", None):
            raise requests.exceptions.SSLError("Unexpected T-Education TLS origin")
        bundle = Path(__file__).with_name("certs") / "russian_trusted_root_ca.pem"
        super().cert_verify(conn, url, str(bundle), cert)


class TBankAuthSession:
    def __init__(self, timeout: float = 30) -> None:
        self.timeout = timeout
        self.email: str | None = None
        self.session = requests.Session()
        self.session.mount(BASE_URL + "/", TBankTLSAdapter())
        self.session.headers.update(
            {"User-Agent": "hse-edu-parser/0.1 (+private read-only exporter)",
             "Accept": "application/json", "Accept-Language": "ru,en;q=0.8"}
        )

    def load(self) -> bool:
        self.email = load_email()
        path = session_path()
        if not path.exists():
            return False
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("version") != 1 or not isinstance(document.get("email"), str):
                return False
            self.email = document["email"]
            for item in document.get("cookies", []):
                if item.get("name") not in {"token", "refresh_token"}:
                    continue
                domain = item.get("domain") or ""
                if domain.lstrip(".") != "edu.tbank.ru":
                    continue
                self.session.cookies.set(
                    item["name"], item.get("value", ""), domain=domain,
                    path=item.get("path") or "/", secure=True,
                    expires=item.get("expires"),
                )
            return True
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            return False

    def save(self) -> None:
        if not self.email:
            raise AuthError("Не указан email Т-Образования")
        cookies = [
            {"name": cookie.name, "value": cookie.value, "domain": cookie.domain,
             "path": cookie.path, "expires": cookie.expires}
            for cookie in self.session.cookies
            if cookie.name in {"token", "refresh_token"}
            and cookie.domain.lstrip(".") == "edu.tbank.ru"
        ]
        if not any(item["name"] == "token" for item in cookies):
            raise AuthError("Т-Образование не вернуло сессионный токен")
        _private_json(session_path(), {"version": 1, "email": self.email, "cookies": cookies})

    def check(self) -> bool:
        response = self.session.get(
            f"{BASE_URL}/api/v1/lk-student-service/student/activities/passing",
            timeout=self.timeout, allow_redirects=False,
        )
        if response.status_code in {401, 403} or 300 <= response.status_code < 400:
            return False
        response.raise_for_status()
        return isinstance(response.json(), list)

    def refresh(self) -> bool:
        response = self.session.post(
            f"{BASE_URL}/api/auth/refresh/v6", json={"features": {}},
            timeout=self.timeout, allow_redirects=False,
        )
        if response.status_code in {400, 401, 403}:
            return False
        response.raise_for_status()
        if not isinstance(response.json().get("user"), dict):
            return False
        self.save()
        return self.check()

    def login(self, email: str, password: str) -> None:
        email = validate_email(email)
        self.email = email
        landing = self.session.get(f"{BASE_URL}/sign-in", timeout=self.timeout)
        landing.raise_for_status()
        action = self.session.post(
            f"{BASE_URL}/api/auth/v1/get-signin-action",
            json={"input": email, "tags": [], "enroll": [],
                  "features": {"tworkAuth": True}},
            timeout=self.timeout, allow_redirects=False,
        )
        if action.status_code == 400:
            raise AuthError(
                "Т-Образование отклонило данные начала входа (HTTP 400). "
                "Проверьте email аккаунта; HTTPS-соединение с сервером уже установлено."
            )
        action.raise_for_status()
        if action.json().get("action") != "input-password":
            raise AuthError("Т-Образование запросило другой способ входа")
        response = self.session.post(
            f"{BASE_URL}/api/auth/signin/v8",
            json={"email": email, "password": password, "features": {}, "data": {}},
            timeout=self.timeout, allow_redirects=False,
        )
        if response.status_code != 200:
            raise AuthError(f"Т-Образование отклонило вход (HTTP {response.status_code})")
        if not isinstance(response.json().get("user"), dict) or not self.check():
            raise AuthError("Вход не создал доступ к курсам Т-Образования")
        self.save()
        save_email(email)

    def ensure_authenticated(self, password_provider: Callable[[str], str]) -> bool:
        self.load()
        if self.email and self.check():
            return False
        if self.email and self.refresh():
            return False
        if not self.email:
            raise AuthError("Сначала выполните tbank-login --email ADDRESS")
        self.login(self.email, password_provider(self.email))
        return True

    def forget(self) -> bool:
        path = session_path()
        config = config_path()
        removed = False
        for target in (path, config):
            if target.exists():
                target.unlink()
                removed = True
        return removed


class TBankClient:
    def __init__(self, auth: TBankAuthSession) -> None:
        self.auth = auth

    def get_json(self, path: str) -> Any:
        parsed = urlparse(path)
        if parsed.scheme or parsed.netloc or not path.startswith("/api/") or ".." in parsed.path.split("/"):
            raise ValueError("Разрешены только внутренние read-only API Т-Образования")
        response = self.auth.session.get(
            BASE_URL + path, timeout=self.auth.timeout, allow_redirects=False,
        )
        if response.status_code in {401, 403}:
            raise AuthError("Сессия Т-Образования истекла или нет доступа к курсу")
        response.raise_for_status()
        if "json" not in response.headers.get("content-type", ""):
            raise ValueError("Т-Образование вернуло не JSON")
        return response.json()

    def passing_activities(self) -> list[dict[str, Any]]:
        return self.get_json("/api/v1/lk-student-service/student/activities/passing")

    def completed_activities(self) -> list[dict[str, Any]]:
        return self.get_json("/api/v1/lk-student-service/student/activities/completed")

    def course_streams(self, course_id: str) -> Any:
        return self.get_json(f"/api/v3/course/{_uuid(course_id)}/streams")

    def course_info(self, stream_id: str) -> dict[str, Any]:
        return self.get_json(f"/api/v4/course-stream/{_uuid(stream_id)}/info")

    def course_progress(self, stream_id: str) -> dict[str, Any]:
        return self.get_json(f"/api/v2/course-stream/{_uuid(stream_id)}/progress?last-version=0")

    def unit(self, stream_id: str, unit_stream_id: str) -> Any:
        return self.get_json(f"/api/v4/course-stream/{_uuid(stream_id)}/unit-stream/{_uuid(unit_stream_id)}")

    def practice(self, unit_stream_id: str) -> Any:
        return self.get_json(
            f"/api/v2/practice-progress/by-unit-stream-id/{_uuid(unit_stream_id)}/info"
        )

    def practice_history(self, unit_stream_id: str) -> Any:
        return self.get_json(f"/api/v3/course-exam/{_uuid(unit_stream_id)}/history")


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def sync_tbank(
    client: TBankClient, output: Path, stream_ids: set[str] | None = None,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    output = output.resolve()
    private_dir(output)
    passing = client.passing_activities()
    completed = client.completed_activities()
    write_json(output / "activities" / "passing.json", passing)
    write_json(output / "activities" / "completed.json", completed)

    courses = {
        item["activityId"]: item
        for item in passing + completed
        if item.get("entityType") == 1 and isinstance(item.get("activityId"), str)
    }
    results: list[dict[str, Any]] = []
    for course_id, activity in courses.items():
        course_result: dict[str, Any] = {
            "course_id": course_id, "title": activity.get("title"),
            "streams": [], "errors": [],
        }
        results.append(course_result)
        try:
            streams = client.course_streams(course_id)
            if not isinstance(streams, list):
                raise ValueError("Неожиданный список потоков курса")
            course_dir = output / "courses" / _uuid(course_id)
            write_json(course_dir / "activity.json", activity)
            write_json(course_dir / "streams.json", streams)
        except (requests.RequestException, ValueError, AuthError) as exc:
            course_result["errors"].append(f"streams: {_describe_error(exc)}")
            continue
        for stream in streams:
            stream_id = stream.get("streamId") if isinstance(stream, dict) else None
            if stream_ids and stream_id not in stream_ids:
                continue
            stream_result: dict[str, Any] = {"stream_id": stream_id, "units": 0, "exams": 0, "errors": []}
            course_result["streams"].append(stream_result)
            try:
                stream_id = _uuid(stream_id)
                info = client.course_info(stream_id)
                if not info.get("userCourseStreamId"):
                    raise ValueError("Поток не привязан к текущему студенту")
                progress = client.course_progress(stream_id)
                stream_dir = course_dir / "streams" / stream_id
                write_json(stream_dir / "info.json", info)
                write_json(stream_dir / "progress.json", progress)
            except (requests.RequestException, ValueError, AuthError) as exc:
                stream_result["errors"].append(f"info/progress: {_describe_error(exc)}")
                continue
            units = [
                unit for module in info.get("modules") or []
                for unit in module.get("units") or []
                if isinstance(unit, dict)
            ]
            write_json(stream_dir / "units" / "index.json", units)
            for unit in units:
                unit_id = unit.get("id")
                try:
                    unit_id = _uuid(unit_id)
                    unit_dir = stream_dir / "units" / unit_id
                    write_json(unit_dir / "detail.json", client.unit(stream_id, unit_id))
                    stream_result["units"] += 1
                    if unit.get("type") == "exam":
                        write_json(unit_dir / "practice.json", client.practice(unit_id))
                        write_json(unit_dir / "history.json", client.practice_history(unit_id))
                        stream_result["exams"] += 1
                except (requests.RequestException, ValueError, AuthError) as exc:
                    stream_result["errors"].append(f"unit {unit_id}: {_describe_error(exc)}")
    if stream_ids and not any(
        stream.get("stream_id") in stream_ids
        for course in results for stream in course["streams"]
    ):
        raise ValueError("Указанный поток не найден среди личных курсов")
    finished = datetime.now(timezone.utc)
    report = {
        "schema_version": 1, "started_at": started.isoformat(),
        "finished_at": finished.isoformat(), "output": str(output),
        "course_count": len(courses), "course_results": results,
    }
    write_json(output / "snapshot.json", report)
    return report
