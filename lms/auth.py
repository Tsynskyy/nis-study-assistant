from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://edu.hse.ru"
LOGIN_URL = f"{BASE_URL}/auth/oidckc/"
COURSES_URL = f"{BASE_URL}/my/courses.php"
SERVICE_NAME = "hse-edu-parser"


class AuthError(RuntimeError):
    pass


def _state_home() -> Path:
    root = os.environ.get("XDG_STATE_HOME")
    return Path(root) if root else Path.home() / ".local" / "state"


def _config_home() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    return Path(root) if root else Path.home() / ".config"


def session_path() -> Path:
    return _state_home() / SERVICE_NAME / "session.json"


def config_path() -> Path:
    return _config_home() / SERVICE_NAME / "config.json"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _private_json(path: Path, value: object) -> None:
    _private_dir(path.parent)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def load_username() -> str | None:
    path = config_path()
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    username = value.get("username") if isinstance(value, dict) else None
    return username if isinstance(username, str) and username else None


def save_username(username: str) -> None:
    _private_json(config_path(), {"username": username})


def _credential_backend() -> tuple[str, str] | None:
    if sys.platform == "win32":
        return "wincred", "Windows Credential Manager"
    if sys.platform == "darwin":
        executable = shutil.which("security")
        if executable:
            return "security", executable
    executable = shutil.which("secret-tool")
    if executable:
        return "secret-tool", executable
    return None


def _windows_credential_target(service: str, account: str) -> str:
    return f"{service}:{account}"


def _windows_credential_types():
    import ctypes
    from ctypes import wintypes

    class CredentialAttribute(ctypes.Structure):
        _fields_ = [
            ("Keyword", wintypes.LPWSTR),
            ("Flags", wintypes.DWORD),
            ("ValueSize", wintypes.DWORD),
            ("Value", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    class Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.POINTER(CredentialAttribute)),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    return ctypes, wintypes, Credential


def _windows_load_credential(service: str, account: str) -> str | None:
    ctypes, wintypes, credential_type = _windows_credential_types()
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_read = advapi32.CredReadW
    cred_read.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(credential_type)),
    ]
    cred_read.restype = wintypes.BOOL
    cred_free = advapi32.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    cred_free.restype = None

    pointer = ctypes.POINTER(credential_type)()
    if not cred_read(
        _windows_credential_target(service, account), 1, 0, ctypes.byref(pointer),
    ):
        error = ctypes.get_last_error()
        if error == 1168:
            return None
        raise AuthError(f"Windows Credential Manager: {ctypes.WinError(error)}")
    try:
        credential = pointer.contents
        if not credential.CredentialBlob or not credential.CredentialBlobSize:
            return None
        raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        cred_free(pointer)


def _windows_store_credential(
    service: str, account: str, password: str, label: str,
) -> None:
    ctypes, wintypes, credential_type = _windows_credential_types()
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_write = advapi32.CredWriteW
    cred_write.argtypes = [ctypes.POINTER(credential_type), wintypes.DWORD]
    cred_write.restype = wintypes.BOOL

    encoded = password.encode("utf-16-le")
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    credential = credential_type()
    credential.Type = 1
    credential.TargetName = _windows_credential_target(service, account)
    credential.Comment = label
    credential.CredentialBlobSize = len(encoded)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = 2
    credential.UserName = account
    if not cred_write(ctypes.byref(credential), 0):
        error = ctypes.get_last_error()
        raise AuthError(f"Windows Credential Manager: {ctypes.WinError(error)}")


def _windows_clear_credential(service: str, account: str) -> bool:
    ctypes, wintypes, _ = _windows_credential_types()
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_delete = advapi32.CredDeleteW
    cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    cred_delete.restype = wintypes.BOOL
    if cred_delete(_windows_credential_target(service, account), 1, 0):
        return True
    error = ctypes.get_last_error()
    if error == 1168:
        return False
    raise AuthError(f"Windows Credential Manager: {ctypes.WinError(error)}")


def load_credential(service: str, account: str) -> str | None:
    backend = _credential_backend()
    if backend is None:
        return None
    kind, executable = backend
    if kind == "wincred":
        return _windows_load_credential(service, account)
    if kind == "secret-tool":
        command = [executable, "lookup", "service", service, "username", account]
    else:
        command = [
            executable, "find-generic-password", "-a", account,
            "-s", service, "-w",
        ]
    process = subprocess.run(
        command, text=True, encoding="utf-8", capture_output=True, timeout=30, check=False,
    )
    password = process.stdout.rstrip("\n")
    return password if process.returncode == 0 and password else None


def load_password(username: str) -> str | None:
    from_env = os.environ.get("HSE_EDU_PASSWORD")
    return from_env or load_credential(SERVICE_NAME, username)


def prompt_password(username: str) -> str:
    password = getpass.getpass(f"Пароль HSE для {username}: ")
    if not password:
        raise AuthError("Пустой пароль")
    return password


def store_credential(service: str, account: str, password: str, label: str) -> None:
    backend = _credential_backend()
    if backend is None:
        raise AuthError(
            "Нет поддерживаемого хранилища паролей: Windows Credential Manager, "
            "Secret Service или macOS Keychain"
        )
    kind, executable = backend
    if kind == "wincred":
        _windows_store_credential(service, account, password, label)
        return
    if kind == "secret-tool":
        command = [
            executable, "store", f"--label={label}",
            "service", service, "username", account,
        ]
        input_text = password
    else:
        interactive = " ".join(
            [
                "add-generic-password", "-U", "-a", shlex.quote(account),
                "-s", shlex.quote(service), "-l", shlex.quote(label),
                "-X", password.encode("utf-8").hex(),
            ]
        )
        command = [executable, "-i"]
        input_text = interactive + "\n"
    process = subprocess.run(
        command, input=input_text, text=True, encoding="utf-8", capture_output=True,
        timeout=60, check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or "неизвестная ошибка защищенного хранилища"
        raise AuthError(f"Не удалось сохранить пароль: {detail}")


def store_password(username: str, password: str) -> None:
    store_credential(SERVICE_NAME, username, password, f"HSE Smart LMS ({username})")


def clear_credential(service: str, account: str) -> bool:
    backend = _credential_backend()
    if backend is None:
        return False
    kind, executable = backend
    if kind == "wincred":
        return _windows_clear_credential(service, account)
    if kind == "secret-tool":
        command = [executable, "clear", "service", service, "username", account]
    else:
        command = [
            executable, "delete-generic-password", "-a", account, "-s", service,
        ]
    process = subprocess.run(
        command, text=True, encoding="utf-8", capture_output=True, timeout=30, check=False,
    )
    return process.returncode == 0


def clear_password(username: str) -> bool:
    return clear_credential(SERVICE_NAME, username)


class HSEAuthSession:
    def __init__(self, timeout: float = 30) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "hse-edu-parser/0.1 (+private read-only exporter)",
                "Accept-Language": "ru,en;q=0.8",
            }
        )

    def load_cookies(self) -> bool:
        path = session_path()
        if not path.exists():
            return False
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            for item in document.get("cookies", []):
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                kwargs = {
                    "domain": item.get("domain") or "",
                    "path": item.get("path") or "/",
                    "secure": bool(item.get("secure")),
                    "expires": item.get("expires"),
                }
                self.session.cookies.set(item["name"], item.get("value", ""), **kwargs)
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def save_cookies(self) -> None:
        cookies = []
        for cookie in self.session.cookies:
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "secure": cookie.secure,
                    "expires": cookie.expires,
                }
            )
        _private_json(session_path(), {"version": 1, "cookies": cookies})

    @staticmethod
    def _looks_authenticated(response: requests.Response) -> bool:
        path = urlparse(response.url).path.rstrip("/")
        if path in {"/login/hselogin.php", "/login/index.php"}:
            return False
        soup = BeautifulSoup(response.text, "lxml")
        if soup.select_one('form input[name="password"]'):
            return False
        return bool(
            '"sesskey"' in response.text
            or soup.select_one('a[href*="/login/logout.php"]')
            or path.startswith("/my")
        )

    def check(self) -> requests.Response | None:
        response = self.session.get(COURSES_URL, timeout=self.timeout)
        return response if self._looks_authenticated(response) else None

    @staticmethod
    def _form_payload(form) -> dict[str, str]:
        result: dict[str, str] = {}
        for element in form.select("input[name]"):
            input_type = (element.get("type") or "text").lower()
            if input_type in {"checkbox", "radio"} and not element.has_attr("checked"):
                continue
            result[element.get("name")] = element.get("value", "")
        return result

    @staticmethod
    def _assert_safe_action(action: str, allowed_hosts: set[str]) -> None:
        parsed = urlparse(action)
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise AuthError(f"Отказ отправлять учетные данные на неожиданный адрес: {action}")

    def login(self, username: str, password: str) -> requests.Response:
        response = self.session.get(LOGIN_URL, timeout=self.timeout)
        soup = BeautifulSoup(response.text, "lxml")
        login_form = next(
            (
                form
                for form in soup.select("form")
                if form.select_one('input[name="username"]')
                and form.select_one('input[name="password"]')
            ),
            None,
        )
        if login_form is None:
            if self._looks_authenticated(response):
                return response
            raise AuthError("Не найдена форма входа HSE; возможно, изменился SSO")

        action = urljoin(response.url, login_form.get("action") or response.url)
        self._assert_safe_action(action, {"saml.hse.ru"})
        payload = self._form_payload(login_form)
        payload.update({"username": username, "password": password})
        submit = login_form.select_one('input[type="submit"][name]')
        if submit is not None:
            payload[submit.get("name")] = submit.get("value", "")
        response = self.session.post(action, data=payload, timeout=self.timeout)

        soup = BeautifulSoup(response.text, "lxml")
        if soup.select_one('input[name="otp"], input[name="totp"], input[autocomplete="one-time-code"]'):
            raise AuthError("SSO запросил одноразовый код; автоматический вход временно невозможен")
        if soup.select_one('form input[name="password"]'):
            error = soup.select_one(".alert-error, .alert-danger, #input-error, .kc-feedback-text")
            detail = error.get_text(" ", strip=True) if error else "проверьте логин и пароль"
            raise AuthError(f"HSE отклонил вход: {detail}")

        oidc_form = next(
            (
                form
                for form in soup.select("form")
                if form.select_one('input[name="code"]') and form.select_one('input[name="state"]')
            ),
            None,
        )
        if oidc_form is not None:
            callback = urljoin(response.url, oidc_form.get("action") or response.url)
            self._assert_safe_action(callback, {"edu.hse.ru"})
            response = self.session.post(
                callback,
                data=self._form_payload(oidc_form),
                timeout=self.timeout,
            )

        if not self._looks_authenticated(response):
            response = self.session.get(COURSES_URL, timeout=self.timeout)
        if not self._looks_authenticated(response):
            raise AuthError("SSO завершился без авторизованной сессии Smart LMS")
        self.save_cookies()
        return response

    def ensure_authenticated(
        self,
        username: str,
        password_provider: Callable[[], str],
    ) -> tuple[requests.Response, bool]:
        self.load_cookies()
        response = self.check()
        if response is not None:
            return response, False
        self.session.cookies.clear()
        response = self.login(username, password_provider())
        return response, True

    def forget_session(self) -> bool:
        path = session_path()
        if path.exists():
            path.unlink()
            return True
        return False
