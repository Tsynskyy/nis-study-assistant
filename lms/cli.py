from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import threading
import math

import requests

from .auth import (
    AuthError,
    HSEAuthSession,
    clear_password,
    load_password,
    load_username,
    prompt_password,
    save_username,
    session_path,
    store_password,
)
from .client import SmartLMSClient
from .legacy_auth import LegacyLMSAuthSession, legacy_session_path
from .legacy_client import LegacyLMSClient
from .legacy_sync import sync_legacy
from .passport import export_passport
from .sync import SyncOptions, sync_all
from .locking import output_lock
from .tbank import (
    TBankAuthSession,
    TBankClient,
    clear_password as clear_tbank_password,
    load_email as load_tbank_email,
    load_password as load_tbank_password,
    prompt_password as prompt_tbank_password,
    store_password as store_tbank_password,
    sync_tbank,
    validate_email as validate_tbank_email,
)
from .runtime import configure_output
from assistant.schedule import load_env


def _username(args) -> str:
    username = args.username or os.environ.get("HSE_LOGIN") or load_username()
    if not username:
        raise AuthError("Укажите логин HSE: --username или HSE_LOGIN в .env")
    return username


def _auth(
    args, force_prompt: bool = False, allow_prompt: bool = True,
) -> tuple[HSEAuthSession, object, bool]:
    username = _username(args)
    save_username(username)
    auth = HSEAuthSession(timeout=args.timeout)

    def password_provider() -> str:
        if not force_prompt:
            password = load_password(username)
            if password:
                return password
        if not allow_prompt:
            raise AuthError("Сессия Smart LMS истекла, а пароль не найден в защищенном хранилище")
        return prompt_password(username)

    response, logged_in = auth.ensure_authenticated(username, password_provider)
    return auth, response, logged_in


def _legacy_auth(
    args, force_prompt: bool = False, allow_prompt: bool = True,
) -> tuple[LegacyLMSAuthSession, object, bool]:
    username = _username(args)
    save_username(username)
    auth = LegacyLMSAuthSession(timeout=args.timeout)

    def password_provider() -> str:
        if not force_prompt:
            password = load_password(username)
            if password:
                return password
        if not allow_prompt:
            raise AuthError("Сессия Legacy LMS истекла, а пароль не найден в защищенном хранилище")
        return prompt_password(username)

    response, logged_in = auth.ensure_authenticated(username, password_provider)
    return auth, response, logged_in


def cmd_login(args) -> int:
    username = _username(args)
    save_username(username)
    auth = HSEAuthSession(timeout=args.timeout)
    password = load_password(username) if not args.prompt else None
    if not password:
        password = prompt_password(username)
    auth.login(username, password)
    if args.store_password:
        store_password(username, password)
        print("Вход выполнен; пароль сохранен в защищенном хранилище ОС.")
    else:
        print("Вход выполнен; сессия сохранена, пароль не сохранялся.")
    print(f"Файл сессии: {session_path()}")
    return 0


def cmd_status(args) -> int:
    username = _username(args)
    auth = HSEAuthSession(timeout=args.timeout)
    loaded = auth.load_cookies()
    authenticated = bool(loaded and auth.check())
    print(f"Пользователь: {username}")
    legacy = LegacyLMSAuthSession(timeout=args.timeout)
    legacy_loaded = legacy.load_cookies()
    legacy_authenticated = bool(legacy_loaded and legacy.check())
    print(f"Smart LMS сессия: {'активна' if authenticated else 'отсутствует или истекла'}")
    print(f"Legacy LMS сессия: {'активна' if legacy_authenticated else 'отсутствует или истекла'}")
    print(f"Пароль HSE в защищенном хранилище: {'есть' if load_password(username) else 'нет'}")
    return 0 if authenticated and legacy_authenticated else 1


def cmd_sync(args) -> int:
    with output_lock(Path(args.output)):
        return _cmd_sync(args)


def _cmd_sync(args) -> int:
    auth, response, logged_in = _auth(args, allow_prompt=not getattr(args, "no_prompt", False))
    client = SmartLMSClient(auth, response.text)
    if not client.sesskey or not client.userid:
        client.bootstrap()
    course_ids = set(args.course_id) if args.course_id else None
    report = sync_all(
        client,
        response.text,
        SyncOptions(
            output=Path(args.output),
            workers=args.workers,
            include_messages=not args.skip_messages,
            course_ids=course_ids,
            max_courses=args.max_courses,
            only_missing=args.only_missing,
            mode=getattr(args, "mode", "full"),
            archive_max_age_hours=getattr(args, "archive_max_age_hours", 168),
            watch_course_ids=set(getattr(args, "watch_course_id", []) or []),
            progress=lambda message: print(message, flush=True),
            cancel_event=getattr(args, "cancel_event", None),
        ),
    )
    course_failures = sum(bool(item.get("errors")) for item in report["course_results"])
    print(
        f"Готово: курсов {report['course_count']}, диалогов {report['conversation_count']}, "
        f"сообщений {report['message_count']}, курсов с частичными ошибками {course_failures}."
    )
    print(f"Данные: {report['output']}")
    if logged_in:
        print("Истекшая сессия была автоматически обновлена.")
    if report["errors"] or course_failures:
        for error in report["errors"]:
            print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    return 0


def cmd_refresh(args) -> int:
    import json
    from .refresh import refresh
    policy = {}
    if args.policy:
        path = Path(args.policy)
        if not path.exists():
            raise ValueError(f"Файл политики не найден: {path}")
        policy = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(policy, dict) or not isinstance(policy.get("watch_course_ids", []), list):
            raise ValueError("Политика должна быть JSON-объектом со списком watch_course_ids")
    archive_age = args.archive_max_age_hours if args.archive_max_age_hours is not None else policy.get("archive_max_age_hours", 168)
    if not isinstance(archive_age, (int, float)) or not math.isfinite(archive_age) or archive_age <= 0:
        raise ValueError("Срок хранения архива должен быть положительным")
    watch = policy.get("watch_course_ids", []) + (args.watch_course_id or [])
    if any(type(course_id) is not int or course_id <= 0 for course_id in watch):
        raise ValueError("watch_course_ids должен содержать положительные ID курсов")
    cancelled = threading.Event()
    common = {"username": args.username, "timeout": args.timeout, "no_prompt": True}
    smart = argparse.Namespace(**common, output=args.lms_output, workers=args.workers,
                               skip_messages=False, course_id=None, max_courses=None,
                               only_missing=False, mode="full" if args.full else "fast",
                               archive_max_age_hours=archive_age, watch_course_id=sorted(set(watch)),
                               cancel_event=cancelled)
    legacy = argparse.Namespace(**common, output=str(Path(args.lms_output) / "legacy-lms"))
    tbank = argparse.Namespace(**common, output=args.tbank_output, stream_id=None)
    with output_lock(Path(args.lms_output)):
        refresh(Path(args.lms_output), Path(args.output), {
            "Smart LMS": lambda stage: _cmd_sync(smart),
            "Legacy LMS": lambda stage: cmd_records_sync(legacy),
            "Т-Образование": lambda stage: cmd_tbank_sync(tbank),
        }, cancel_event=cancelled, tbank_root=Path(args.tbank_output))
    return 0


def cmd_records_login(args) -> int:
    auth, _, logged_in = _legacy_auth(args, force_prompt=args.prompt)
    print(f"Legacy LMS: {'выполнен новый вход' if logged_in else 'сессия уже активна'}.")
    print(f"Файл сессии: {legacy_session_path()}")
    return 0


def cmd_records_sync(args) -> int:
    with output_lock(Path(args.output)):
        return _cmd_records_sync(args)


def _cmd_records_sync(args) -> int:
    auth, _, logged_in = _legacy_auth(args, allow_prompt=not getattr(args, "no_prompt", False))
    report = sync_legacy(LegacyLMSClient(auth), Path(args.output))
    print(
        f"Legacy LMS готов: строк в зачетке {report['gradebook_row_count']}, "
        f"курсов {report['course_count']}, ошибок {len(report['errors'])}."
    )
    print(f"Данные: {report['output']}")
    if logged_in:
        print("Истекшая Legacy LMS сессия была автоматически обновлена.")
    for error in report["errors"]:
        print(f"Ошибка: {error}", file=sys.stderr)
    return 2 if report["errors"] else 0


def cmd_sync_all(args) -> int:
    smart_args = argparse.Namespace(**vars(args))
    smart_args.output = str(Path(args.output))
    smart_code = cmd_sync(smart_args)
    records_args = argparse.Namespace(**vars(args))
    records_args.output = str(Path(args.output) / "legacy-lms")
    records_code = cmd_records_sync(records_args)
    return max(smart_code, records_code)


def cmd_passport_export(args) -> int:
    with output_lock(Path(args.input)):
        summary = export_passport(Path(args.input), Path(args.output))
    print(
        f"Паспорт обновлен: курсов {summary['course_count']}, активностей {summary['activity_count']}, "
        f"оценок {summary['graded_item_count']}, дедлайнов {summary['deadline_count']}."
    )
    print(f"Данные: {Path(args.output).resolve()}")
    return 0


def cmd_tbank_login(args) -> int:
    auth = TBankAuthSession(timeout=args.timeout)
    auth.load()
    email = args.email or auth.email or load_tbank_email()
    if not email:
        raise AuthError("Укажите email: tbank-login --email ADDRESS")
    email = validate_tbank_email(email)
    password = prompt_tbank_password(email)
    auth.login(email, password)
    if args.store_password:
        store_tbank_password(email, password)
        print("Вход в Т-Образование выполнен; пароль сохранен в защищенном хранилище ОС.")
    else:
        print("Вход в Т-Образование выполнен; пароль не сохранялся.")
    return 0


def cmd_tbank_status(args) -> int:
    auth = TBankAuthSession(timeout=args.timeout)
    loaded = auth.load()
    active = bool(loaded and auth.check())
    print(f"Т-Образование: {'сессия активна' if active else 'сессия отсутствует или истекла'}")
    stored = bool(auth.email and load_tbank_password(auth.email))
    print(f"Пароль Т-Образования в защищенном хранилище: {'есть' if stored else 'нет'}")
    return 0 if active else 1


def cmd_tbank_sync(args) -> int:
    with output_lock(Path(args.output)):
        return _cmd_tbank_sync(args)


def _cmd_tbank_sync(args) -> int:
    auth = TBankAuthSession(timeout=args.timeout)

    def password_provider(email: str) -> str:
        password = load_tbank_password(email)
        if password:
            return password
        if getattr(args, "no_prompt", False):
            raise AuthError(
                "Сессия Т-Образования истекла, а пароль не найден в защищенном хранилище"
            )
        return prompt_tbank_password(email)

    refreshed = auth.ensure_authenticated(password_provider)
    report = sync_tbank(
        TBankClient(auth), Path(args.output),
        set(args.stream_id) if args.stream_id else None,
    )
    streams = [stream for course in report["course_results"] for stream in course["streams"]]
    failures = sum(bool(course["errors"]) for course in report["course_results"])
    failures += sum(bool(stream["errors"]) for stream in streams)
    print(
        f"Т-Образование: курсов {report['course_count']}, потоков {len(streams)}, "
        f"уроков {sum(stream['units'] for stream in streams)}, "
        f"заданий {sum(stream['exams'] for stream in streams)}, "
        f"частичных ошибок {failures}."
    )
    print(f"Данные: {report['output']}")
    if refreshed:
        print("Сессия Т-Образования обновлена новым входом.")
    return 2 if failures else 0


def cmd_tbank_forget(args) -> int:
    auth = TBankAuthSession(timeout=args.timeout)
    auth.load()
    email = auth.email
    removed = auth.forget()
    removed_password = bool(email and clear_tbank_password(email))
    print(f"Сессия Т-Образования удалена: {'да' if removed else 'нет'}")
    print(f"Пароль Т-Образования удален из защищенного хранилища: {'да' if removed_password else 'нет'}")
    return 0


def cmd_forget(args) -> int:
    username = _username(args)
    removed_session = HSEAuthSession(timeout=args.timeout).forget_session()
    removed_password = clear_password(username)
    legacy_path = legacy_session_path()
    removed_legacy_session = legacy_path.exists()
    if removed_legacy_session:
        legacy_path.unlink()
    print(f"Сессия удалена: {'да' if removed_session else 'нет'}")
    print(f"Legacy LMS сессия удалена: {'да' if removed_legacy_session else 'нет'}")
    print(f"Пароль HSE удален из защищенного хранилища: {'да' if removed_password else 'нет'}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m lms", description="Read-only экспорт HSE Smart LMS и legacy LMS")
    root.add_argument("--username", help="логин HSE")
    root.add_argument("--timeout", type=float, default=60, help="HTTP timeout в секундах")
    commands = root.add_subparsers(dest="command", required=True)

    login = commands.add_parser("login", help="создать новую SSO-сессию")
    login.add_argument("--prompt", action="store_true", help="всегда запросить пароль заново")
    login.add_argument(
        "--store-password", action="store_true",
        help="сохранить пароль в защищенном хранилище ОС",
    )
    login.set_defaults(handler=cmd_login)

    status = commands.add_parser("status", help="проверить сохраненную сессию")
    status.set_defaults(handler=cmd_status)

    sync = commands.add_parser("sync", help="выгрузить курсы и личные данные")
    sync.add_argument("--output", default="lms-data", help="каталог выгрузки")
    sync.add_argument("--workers", type=int, default=4, help="число параллельных запросов, 1..8")
    sync.add_argument("--skip-messages", action="store_true", help="не выгружать личные сообщения")
    sync.add_argument("--course-id", action="append", type=int, help="выгрузить только этот курс")
    sync.add_argument("--max-courses", type=int, help="ограничить число курсов (для проверки)")
    sync.add_argument("--only-missing", action="store_true", help="докачать только отсутствующие файлы курсов")
    sync.set_defaults(handler=cmd_sync)
    sync.add_argument("--no-prompt", action="store_true", help="не запрашивать пароль интерактивно")

    refresh_parser = commands.add_parser("refresh", help="обновить все кабинеты и сводку для ассистента")
    refresh_parser.add_argument("--full", action="store_true", help="перечитать весь архив; по умолчанию быстрый режим")
    refresh_parser.add_argument("--lms-output", default="lms-data")
    refresh_parser.add_argument("--tbank-output", default="tbank-data")
    refresh_parser.add_argument("--output", default=os.environ.get("HSE_PASSPORT_DIR", "student-passport"))
    refresh_parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    refresh_parser.add_argument("--policy", help="JSON с watch_course_ids и archive_max_age_hours")
    refresh_parser.add_argument("--watch-course-id", type=int, action="append")
    refresh_parser.add_argument("--archive-max-age-hours", type=float)
    refresh_parser.set_defaults(handler=cmd_refresh)

    records_login = commands.add_parser("records-login", help="создать сессию lms.hse.ru через ЕЛК")
    records_login.add_argument("--prompt", action="store_true", help="всегда запросить пароль заново")
    records_login.set_defaults(handler=cmd_records_login)

    records_sync = commands.add_parser("records-sync", help="выгрузить зачетку и данные legacy LMS")
    records_sync.add_argument("--output", default="lms-data/legacy-lms", help="каталог выгрузки")
    records_sync.set_defaults(handler=cmd_records_sync)
    records_sync.add_argument("--no-prompt", action="store_true", help="не запрашивать пароль интерактивно")

    sync_all = commands.add_parser("sync-all", help="обновить Smart LMS и legacy LMS")
    sync_all.add_argument("--output", default="lms-data", help="корневой каталог выгрузки")
    sync_all.add_argument("--workers", type=int, default=4, help="число параллельных Smart LMS запросов, 1..8")
    sync_all.add_argument("--skip-messages", action="store_true", help="не выгружать личные Smart LMS сообщения")
    sync_all.add_argument("--course-id", action="append", type=int, help="выгрузить только этот Smart LMS курс")
    sync_all.add_argument("--max-courses", type=int, help="ограничить число Smart LMS курсов")
    sync_all.add_argument("--only-missing", action="store_true", help="докачать только отсутствующие файлы Smart LMS")
    sync_all.set_defaults(handler=cmd_sync_all)
    sync_all.add_argument("--no-prompt", action="store_true", help="не запрашивать пароль интерактивно")

    passport_export = commands.add_parser("passport-export", help="собрать важные данные LMS в паспорт")
    passport_export.add_argument("--input", default="lms-data", help="каталог сырого снимка LMS")
    passport_export.add_argument("--output", default=os.environ.get("HSE_PASSPORT_DIR", "student-passport"), help="каталог паспорта")
    passport_export.set_defaults(handler=cmd_passport_export)

    tbank_login = commands.add_parser("tbank-login", help="создать отдельную сессию Т-Образования")
    tbank_login.add_argument(
        "--email", help="почта для входа; без аргумента берется из сохраненной сессии",
    )
    tbank_login.add_argument(
        "--store-password", action="store_true",
        help="сохранить пароль в защищенном хранилище ОС",
    )
    tbank_login.set_defaults(handler=cmd_tbank_login)

    tbank_status = commands.add_parser("tbank-status", help="проверить сессию Т-Образования")
    tbank_status.set_defaults(handler=cmd_tbank_status)

    tbank_sync = commands.add_parser("tbank-sync", help="выгрузить личные курсы Т-Образования")
    tbank_sync.add_argument("--output", default="tbank-data", help="закрытый каталог выгрузки")
    tbank_sync.add_argument("--stream-id", action="append", help="выгрузить только этот поток курса")
    tbank_sync.set_defaults(handler=cmd_tbank_sync)
    tbank_sync.add_argument("--no-prompt", action="store_true", help="не запрашивать пароль интерактивно")

    tbank_forget = commands.add_parser("tbank-forget", help="удалить сессию Т-Образования")
    tbank_forget.set_defaults(handler=cmd_tbank_forget)

    forget = commands.add_parser("forget", help="удалить локальную сессию и пароль")
    forget.set_defaults(handler=cmd_forget)
    return root


def main(argv: list[str] | None = None) -> int:
    configure_output()
    load_env()
    args = parser().parse_args(argv)
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError("HTTP timeout должен быть положительным конечным числом")
        return int(args.handler(args))
    except (AuthError, requests.RequestException, ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Прервано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
