from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from .corpus import DEADLINES_CSV, data_dir
from .pipeline import LOG_PATH

TARGETS = {"p95_ms": 2000, "fallback_rate": 0.2, "data_age_days": 7}


def read_log(path: Path = LOG_PATH) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def data_age_days(root: Path | None = None) -> float | None:
    root = root or data_dir()
    refresh = root / "REFRESH.md"
    if refresh.exists():
        m = re.search(r"Завершено: (\S+)\.", refresh.read_text(encoding="utf-8"))
        if m:
            ts = datetime.fromisoformat(m.group(1))
            return (datetime.now(ts.tzinfo) - ts).total_seconds() / 86400
    p = root / DEADLINES_CSV
    return (datetime.now().timestamp() - p.stat().st_mtime) / 86400 if p.exists() else None


def report(records: list[dict] | None = None) -> dict:
    records = read_log() if records is None else records
    lat = sorted(r["latency_ms"] for r in records)
    routes = Counter(r["route"] for r in records)
    n = len(records)
    age = data_age_days()
    rep = {
        "queries": n,
        "by_day": dict(sorted(Counter(r["ts"][:10] for r in records).items())),
        "routes": dict(routes),
        "fallback_rate": round(routes.get("fallback", 0) / n, 3) if n else None,
        "llm_share": round(sum(r["generator"] != "extractive" for r in records) / n, 3) if n else None,
        "p50_ms": lat[n // 2] if n else None,
        "p95_ms": lat[max(int(n * 0.95) - 1, 0)] if n else None,
        "top_sources": Counter(s.split("#")[0] for r in records for s in r["sources"]).most_common(5),
        "unanswered": [r["question"] for r in records if r["route"] == "fallback"][-10:],
        "data_age_days": round(age, 1) if age is not None else None,
    }
    rep["alerts"] = [
        f"{key} = {rep[key]} > {limit}"
        for key, limit in TARGETS.items()
        if rep.get(key) is not None and rep[key] > limit
    ]
    return rep
