from __future__ import annotations

import json
import os
import sys
import time
from datetime import date

from .corpus import load_corpus
from .integrations import upcoming
from .monitor import report
from .pipeline import MIN_SCORE, Answer, log
from .retrieval import RETRIEVERS

PROTOCOL = "2025-06-18"
TOOLS = [
    {"name": "search_materials",
     "description": "Поиск по учебным материалам студента (правила курсов, оценки, формулы, задания) "
                    "с источниками. Отвечай только по найденным фрагментам.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "вопрос или ключевые слова по-русски"},
         "k": {"type": "integer", "description": "сколько фрагментов вернуть", "default": 5}},
         "required": ["query"]}},
    {"name": "upcoming_deadlines",
     "description": "Будущие дедлайны со всех учебных платформ (Smart LMS, Т-Образование), по дате.",
     "inputSchema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "горизонт в днях; без него все будущие"}}}},
    {"name": "monitoring_report",
     "description": "Отчет мониторинга ассистента: свежесть данных, задержка, доля отказов, алерты.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def today() -> date:
    return date.fromisoformat(os.environ.get("ASSISTANT_TODAY", date.today().isoformat()))


def search_materials(query: str, k: int = 5) -> dict:
    started = time.perf_counter()
    retriever = RETRIEVERS["bm25"](load_corpus())
    hits = retriever.search(query, max(1, min(int(k), 20)))
    found = bool(hits) and hits[0][1] >= MIN_SCORE["bm25"]
    ans = Answer("", "rag" if found else "fallback", [c for c, _ in hits] if found else [],
                 [s for _, s in hits], "mcp")
    log(query, ans, retriever.name, time.perf_counter() - started)
    return {"found": found,
            "fragments": [{"source": c.id, "title": c.title, "score": round(s, 2), "text": c.text}
                          for c, s in hits] if found else []}


def upcoming_deadlines(days: int | None = None) -> list[dict]:
    return [{"due": c.meta["due"], "event": c.meta["event"], "course": c.meta["course"],
             "url": c.meta.get("url", ""), "source": c.id}
            for c in upcoming(load_corpus(), today(), days)]


HANDLERS = {"search_materials": search_materials, "upcoming_deadlines": upcoming_deadlines,
            "monitoring_report": lambda: report()}


def handle(msg: dict) -> dict | None:
    method, params, mid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "nis-study-assistant", "version": "1.0"},
                  "instructions": "Учебный ассистент студента НИУ ВШЭ: материалы курсов и дедлайны."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call" and params.get("name") in HANDLERS:
        try:
            out = HANDLERS[params["name"]](**(params.get("arguments") or {}))
            result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}],
                      "isError": False}
        except Exception as e:
            result = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    elif method == "tools/call":
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"нет инструмента {params.get('name')}"}}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"нет метода {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None) -> None:
    if stdin is None and hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line))
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
