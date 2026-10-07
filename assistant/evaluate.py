from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

from .corpus import load_corpus
from .pipeline import MIN_SCORE, Assistant
from .retrieval import RETRIEVERS

QUESTIONS = Path(__file__).resolve().parents[1] / "eval" / "questions.jsonl"
EVAL_TODAY = date(2026, 10, 6)


def load_questions(path: Path = QUESTIONS) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def evaluate(assistant: Assistant, questions: list[dict]) -> dict:
    hit1 = hit3 = rr = answered_ok = abstain_ok = 0
    n_ans = n_abs = 0
    latencies, misses = [], []
    for q in questions:
        start = time.perf_counter()
        route, hits = assistant.retrieve(q["q"])
        latencies.append((time.perf_counter() - start) * 1000)
        confident = route == "deadlines" or (hits and hits[0][1] >= MIN_SCORE[assistant.retriever.name])
        if q["type"] == "abstain":
            n_abs += 1
            abstain_ok += not confident
            continue
        n_ans += 1
        expect = [e.lower() for e in q["expect"]]
        ranks = [i for i, (c, _) in enumerate(hits) if any(e in c.text.lower() for e in expect)]
        if ranks:
            hit1 += ranks[0] == 0
            hit3 += ranks[0] < 3
            rr += 1 / (ranks[0] + 1)
            answered_ok += bool(confident)
        else:
            misses.append(q["q"])
    latencies.sort()
    return {
        "retriever": assistant.retriever.name,
        "router": assistant.router,
        "hit@1": round(hit1 / n_ans, 3),
        "hit@3": round(hit3 / n_ans, 3),
        "mrr": round(rr / n_ans, 3),
        "answered_correctly": round(answered_ok / n_ans, 3),
        "abstain_accuracy": round(abstain_ok / n_abs, 3) if n_abs else None,
        "p50_ms": round(latencies[len(latencies) // 2], 2),
        "p95_ms": round(latencies[int(len(latencies) * 0.95) - 1], 2),
        "misses": misses,
    }


def run_all() -> list[dict]:
    os.environ["ASSISTANT_NO_LOG"] = "1"
    chunks = load_corpus()
    questions = load_questions()
    results = []
    for name in RETRIEVERS:
        for router in (False, True):
            results.append(evaluate(Assistant(name, router, chunks, EVAL_TODAY), questions))
    return results
