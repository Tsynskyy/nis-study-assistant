from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .corpus import Chunk, load_corpus
from .retrieval import RETRIEVERS

DEADLINE_INTENT = re.compile(r"дедлайн|сдать|сдавать|срок|ближайш|на этой неделе|до какого", re.I)
MIN_SCORE = {"keyword": 2.0, "tfidf": 0.12, "bm25": 3.0}
LOG_PATH = Path(os.environ.get("ASSISTANT_LOG", Path(__file__).resolve().parents[1] / "logs" / "queries.jsonl"))
SYSTEM = (
    "Ты учебный ассистент студента. Отвечай кратко по-русски и только по приведенным фрагментам. "
    "Фрагменты отсортированы по релевантности, первый подходит лучше всего. "
    "Если ответа во фрагментах нет, так и скажи. В конце укажи имя файла источника в квадратных скобках, "
    "например [AUTUMN-2026.md]."
)


@dataclass
class Answer:
    text: str
    route: str
    sources: list[Chunk] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    generator: str = "extractive"


class Assistant:
    def __init__(self, retriever: str = "bm25", router: bool = True, chunks: list[Chunk] | None = None,
                 today: date | None = None, k: int = 3):
        self.chunks = chunks if chunks is not None else load_corpus()
        self.retriever = RETRIEVERS[retriever](self.chunks)
        self.router, self.k = router, k
        self.today = today or date.fromisoformat(os.environ.get("ASSISTANT_TODAY", date.today().isoformat()))
        self.deadlines = [c for c in self.chunks if "due" in c.meta]

    def retrieve(self, question: str) -> tuple[str, list[tuple[Chunk, float]]]:
        rag = self.retriever.search(question, self.k)
        if self.router and DEADLINE_INTENT.search(question):
            hits, matched = self.upcoming_deadlines(question)
            if matched or (hits and not self.confident(rag)):
                return "deadlines", hits
        return "rag", rag

    def confident(self, hits: list[tuple[Chunk, float]]) -> bool:
        return bool(hits) and hits[0][1] >= MIN_SCORE[self.retriever.name]

    def upcoming_deadlines(self, question: str) -> tuple[list[tuple[Chunk, float]], bool]:
        future = [c for c in self.deadlines if datetime.fromisoformat(c.meta["due"]).date() >= self.today]
        future.sort(key=lambda c: c.meta["due"])
        ranked = dict((c.id, s) for c, s in self.retriever.search(question, len(self.chunks)))
        matched = [c for c in future if ranked.get(c.id, 0) >= MIN_SCORE[self.retriever.name]]
        picked = matched or future
        return [(c, 1.0) for c in picked[: self.k]], bool(matched)

    def ask(self, question: str) -> Answer:
        started = time.perf_counter()
        route, hits = self.retrieve(question)
        if route == "rag" and not self.confident(hits):
            ans = Answer("Не нашел ответа в учебных материалах.", "fallback")
        else:
            chunks = [c for c, _ in hits]
            text, gen = generate(question, chunks, route)
            ans = Answer(text, route, chunks, [s for _, s in hits], gen)
        log(question, ans, self.retriever.name, time.perf_counter() - started)
        return ans


def generate(question: str, chunks: list[Chunk], route: str) -> tuple[str, str]:
    model = os.environ.get("ASSISTANT_MODEL", "gemma4:e4b")
    if route != "deadlines" and os.environ.get("ASSISTANT_LLM") != "off":
        try:
            text = call_ollama(question, chunks, model).strip()
            if not text:
                raise ValueError("пустой ответ модели")
            return text, f"ollama:{model}"
        except Exception as exc:
            print(f"Локальная модель недоступна, ответ без генерации: {exc}")
    return extractive(chunks, route), "extractive"


def extractive(chunks: list[Chunk], route: str) -> str:
    if route == "deadlines":
        lines = [f"- {c.meta['due'][:16].replace('T', ' ')} - {c.meta['event']} ({c.meta['course']})" for c in chunks]
        return "Ближайшие дедлайны:\n" + "\n".join(lines)
    best = chunks[0]
    body = best.text.split("\n", 1)[-1].strip()
    return f"{body[:700]}\n\n[{best.source}: {best.title}]"


def prompt(question: str, chunks: list[Chunk]) -> str:
    context = "\n\n".join(f"[{c.source}] {c.title}\n{c.text}" for c in chunks)
    return f"Фрагменты:\n{context}\n\nВопрос: {question}"


def call_ollama(question: str, chunks: list[Chunk], model: str) -> str:
    body = {"model": model, "stream": False,
            "options": {"temperature": 0, "num_predict": int(os.environ.get("LLM_MAX_TOKENS", "2000"))},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt(question, chunks)}]}
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/chat"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=int(os.environ.get("LLM_TIMEOUT", "120"))) as resp:
        return json.load(resp)["message"].get("content") or ""


def log(question: str, ans: Answer, retriever: str, seconds: float) -> None:
    if os.environ.get("ASSISTANT_NO_LOG"):
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "question": question,
        "route": ans.route,
        "retriever": retriever,
        "generator": ans.generator,
        "sources": [c.id for c in ans.sources],
        "top_score": round(ans.scores[0], 3) if ans.scores else None,
        "latency_ms": round(seconds * 1000, 1),
    }
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
