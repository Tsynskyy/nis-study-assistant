from __future__ import annotations

import hashlib
from urllib.parse import urlparse

import requests

from .auth import AuthError
from .legacy_auth import LEGACY_BASE_URL, LegacyLMSAuthSession
from .legacy_parsers import parse_gradebook, parse_legacy_courses, parse_legacy_page, parse_xls


class LegacyLMSClient:
    def __init__(self, auth: LegacyLMSAuthSession) -> None:
        self.auth = auth
        self.session = auth.session
        self.timeout = auth.timeout

    def get(self, path: str) -> requests.Response:
        response = self.session.get(f"{LEGACY_BASE_URL}{path}", timeout=self.timeout)
        response.raise_for_status()
        if urlparse(response.url).hostname != "lms.hse.ru" or not self.auth._looks_authenticated(response):
            raise AuthError(f"Сессия lms.hse.ru истекла при чтении {path}")
        return response

    def page(self, path: str) -> dict:
        response = self.get(path)
        return parse_legacy_page(response.text, response.url)

    def gradebook(self) -> tuple[dict, bytes]:
        html_response = self.get("/student.php?ctg=personal&op=gradebook")
        parsed = parse_gradebook(html_response.text, html_response.url)
        xls_response = self.session.get(
            f"{LEGACY_BASE_URL}/student.php?ctg=personal&op=excelgradebook",
            timeout=self.timeout,
        )
        xls_response.raise_for_status()
        content = xls_response.content
        if not content.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            raise ValueError("Экспорт зачетки не является бинарным XLS")
        xls_data = parse_xls(content)
        parsed["xls"] = xls_data
        parsed["xls_sha256"] = hashlib.sha256(content).hexdigest()
        parsed["xls_size"] = len(content)
        parsed["xls_data_row_count"] = sum(max(0, sheet["row_count"] - 1) for sheet in xls_data["sheets"])
        return parsed, content

    def courses(self) -> tuple[list[dict], str]:
        response = self.get("/student.php?ctg=lessons")
        return parse_legacy_courses(response.text, response.url), response.text
