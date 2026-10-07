from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .auth import AuthError, SERVICE_NAME, _private_json, _state_home


LEGACY_BASE_URL = "https://lms.hse.ru"


def legacy_session_path() -> Path:
    return _state_home() / SERVICE_NAME / "legacy-session.json"


class LegacyLMSAuthSession:

    def __init__(self, timeout: float = 60) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ru,en;q=0.8",
            }
        )

    @staticmethod
    def _form_payload(form) -> dict[str, str]:
        return {
            element.get("name"): element.get("value", "")
            for element in form.select("input[name]")
            if (element.get("type") or "text").lower() not in {"checkbox", "radio"}
            or element.has_attr("checked")
        }

    def load_cookies(self) -> bool:
        path = legacy_session_path()
        if not path.exists():
            return False
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            for item in document.get("cookies", []):
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                self.session.cookies.set(
                    item["name"],
                    item.get("value", ""),
                    domain=item.get("domain") or "",
                    path=item.get("path") or "/",
                    secure=bool(item.get("secure")),
                    expires=item.get("expires"),
                )
            return True
        except (OSError, ValueError, TypeError):
            return False

    def save_cookies(self) -> None:
        cookies = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
            }
            for cookie in self.session.cookies
        ]
        _private_json(legacy_session_path(), {"version": 1, "cookies": cookies})

    @staticmethod
    def _looks_authenticated(response: requests.Response) -> bool:
        if urlparse(response.url).hostname != "lms.hse.ru":
            return False
        soup = BeautifulSoup(response.text, "lxml")
        return bool(
            soup.select_one('a[href*="ctg=personal"], a[href*="ctg=lessons"]')
            and not soup.select_one('a[href*="elk_login.php"]')
        )

    def check(self) -> requests.Response | None:
        response = self.session.get(f"{LEGACY_BASE_URL}/", timeout=self.timeout)
        return response if self._looks_authenticated(response) else None

    def login(self, username: str, password: str) -> requests.Response:
        return_url = quote(f"{LEGACY_BASE_URL}/", safe="")
        response = self.session.get(
            f"{LEGACY_BASE_URL}/elk_login.php?elk_lms_url={return_url}",
            timeout=self.timeout,
        )
        soup = BeautifulSoup(response.text, "lxml")
        form = next(
            (
                candidate
                for candidate in soup.select("form")
                if candidate.select_one('input[name="username"]')
                and candidate.select_one('input[name="password"]')
            ),
            None,
        )
        if form is None:
            raise AuthError("Не найдена форма входа ЕЛК; возможно, изменился SSO")
        action = urljoin(response.url, form.get("action") or response.url)
        parsed_action = urlparse(action)
        if parsed_action.scheme != "https" or parsed_action.hostname != "saml.hse.ru":
            raise AuthError(f"Отказ отправлять учетные данные на неожиданный адрес: {action}")
        payload = self._form_payload(form)
        payload.update({"username": username, "password": password})
        submit = form.select_one('input[type="submit"][name]')
        if submit is not None:
            payload[submit.get("name")] = submit.get("value", "")
        response = self.session.post(action, data=payload, timeout=self.timeout)
        soup = BeautifulSoup(response.text, "lxml")
        if soup.select_one('input[name="otp"], input[name="totp"], input[autocomplete="one-time-code"]'):
            raise AuthError("ЕЛК запросил одноразовый код; автоматический вход временно невозможен")
        if soup.select_one('form input[name="password"]'):
            error = soup.select_one(".alert-error, .alert-danger, #input-error, .kc-feedback-text")
            detail = error.get_text(" ", strip=True) if error else "проверьте логин и пароль"
            raise AuthError(f"ЕЛК отклонил вход: {detail}")
        if not self._looks_authenticated(response):
            response = self.session.get(f"{LEGACY_BASE_URL}/", timeout=self.timeout)
        if not self._looks_authenticated(response):
            raise AuthError("ЕЛК завершил вход без авторизованной сессии lms.hse.ru")
        self.save_cookies()
        return response

    def ensure_authenticated(self, username: str, password_provider) -> tuple[requests.Response, bool]:
        self.load_cookies()
        response = self.check()
        if response is not None:
            return response, False
        self.session.cookies.clear()
        return self.login(username, password_provider()), True
