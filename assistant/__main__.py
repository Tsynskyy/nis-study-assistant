from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

from .caldav import sync
from .corpus import load_corpus
from .evaluate import run_all
from .integrations import digest, send_mail
from .mcp import serve
from .monitor import report
from .pipeline import Assistant
from .retrieval import RETRIEVERS
from .schedule import ROOT, load_env, run


def main() -> None:
    if sys.stdout is None:
        (ROOT / "logs").mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(ROOT / "logs" / "schedule.log", "a", encoding="utf-8")
        print(f"--- {datetime.now():%Y-%m-%d %H:%M} {' '.join(sys.argv[1:])}")
    load_env()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="assistant", description="Учебный ассистент на RAG")
    sub = p.add_subparsers(dest="cmd", required=True)
    ask = sub.add_parser("ask", help="задать один вопрос")
    ask.add_argument("question")
    sub.add_parser("chat", help="диалог в терминале")
    for s in (ask, sub.choices["chat"]):
        s.add_argument("--retriever", choices=list(RETRIEVERS), default="bm25")
        s.add_argument("--no-router", action="store_true", help="без инструмента дедлайнов")
        s.add_argument("--no-llm", action="store_true", help="ответ без локальной модели")
        s.add_argument("--model", help="модель Ollama (по умолчанию gemma4:e4b)")
    sub.add_parser("eval", help="сравнить подходы на eval/questions.jsonl")
    sub.add_parser("report", help="отчет мониторинга по журналу запросов")
    sub.add_parser("mcp", help="MCP-сервер для подключения к любой LLM")
    dig = sub.add_parser("digest", help="письмо с дедлайнами на почту")
    dig.add_argument("--days", type=int, default=7)
    dig.add_argument("--dry-run", action="store_true", help="показать письмо, не отправляя")
    sub.add_parser("sync-calendar", help="новые и измененные дедлайны в Яндекс Календарь")
    sched = sub.add_parser("schedule", help="дайджест в 09:00 и календарь каждые 30 минут")
    sched.add_argument("action", choices=["install", "remove"])
    args = p.parse_args()

    if args.cmd == "eval":
        for r in run_all():
            misses = r.pop("misses")
            print(json.dumps(r, ensure_ascii=False))
            for m in misses:
                print(f"    промах: {m}")
    elif args.cmd == "report":
        print(json.dumps(report(), ensure_ascii=False, indent=2))
    elif args.cmd == "mcp":
        serve()
    elif args.cmd == "schedule":
        print(run(args.action))
    elif args.cmd in ("digest", "sync-calendar"):
        chunks = load_corpus()
        today = date.fromisoformat(os.environ.get("ASSISTANT_TODAY", date.today().isoformat()))
        if args.cmd == "sync-calendar":
            if not (os.environ.get("SMTP_USER") and os.environ.get("CALDAV_PASSWORD")):
                sys.exit("Задайте SMTP_USER и CALDAV_PASSWORD в .env")
            added, removed = sync(chunks, today)
            print(f"Календарь: добавлено или обновлено {added}, удалено {removed}")
        else:
            subject, body = digest(chunks, today, report()["alerts"], args.days)
            if args.dry_run:
                print(f"Тема: {subject}\n\n{body}")
            elif not (os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD")):
                sys.exit("Задайте SMTP_USER и SMTP_PASSWORD в .env")
            else:
                print(f"Отправлено на {send_mail(subject, body)}")
    else:
        if args.no_llm:
            os.environ["ASSISTANT_LLM"] = "off"
        if args.model:
            os.environ["ASSISTANT_MODEL"] = args.model
        bot = Assistant(args.retriever, not args.no_router)
        questions = [args.question] if args.cmd == "ask" else iter(lambda: input("> ").strip(), "")
        for q in questions:
            ans = bot.ask(q)
            print(ans.text)
            print(f"  [{ans.route}, {ans.generator}; источники: {', '.join(c.id for c in ans.sources) or '-'}]")


if __name__ == "__main__":
    main()
