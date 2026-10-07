from __future__ import annotations

import math
import re
from collections import Counter

from .corpus import Chunk

STOP = set(
    "и в во на по с со к ко о об от до из за для не ни что как а но или ли же бы "
    "у я мне мой моя мои ты вы это этот эта эти то там тут когда где какой какая "
    "какие каком кто чем при считается считаются the a an of to in on for and".split()
)
PREFIX = 5
ABBREVIATIONS = {
    "орг": "основы российской государственности",
}
SAME_STEM = {"итог": "итого", "оцени": "оценк"}


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-zа-яе0-9]+", text.lower().replace("\u0451", "\u0435"))
    stems = [w[:PREFIX] for w in words if w not in STOP and len(w) > 1]
    return [SAME_STEM.get(t, t) for t in stems]


def query_tokens(query: str) -> list[str]:
    words = re.findall(r"[a-zа-яе0-9]+", query.lower().replace("\u0451", "\u0435"))
    return tokenize(" ".join([query] + [ABBREVIATIONS[w] for w in words if w in ABBREVIATIONS]))


class KeywordRetriever:
    name = "keyword"

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.docs = [set(tokenize(c.text)) for c in chunks]

    def search(self, query: str, k: int = 3) -> list[tuple[Chunk, float]]:
        q = set(query_tokens(query))
        scored = [(c, float(len(q & d))) for c, d in zip(self.chunks, self.docs)]
        return _top(scored, k)


class TfidfRetriever:
    name = "tfidf"

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        tfs = [Counter(tokenize(c.text)) for c in chunks]
        df = Counter(t for tf in tfs for t in tf)
        n = len(chunks)
        self.idf = {t: math.log((1 + n) / (1 + d)) + 1 for t, d in df.items()}
        self.vecs = [self._vec(tf) for tf in tfs]

    def _vec(self, tf: Counter) -> dict[str, float]:
        v = {t: (1 + math.log(c)) * self.idf.get(t, 0.0) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def search(self, query: str, k: int = 3) -> list[tuple[Chunk, float]]:
        q = self._vec(Counter(query_tokens(query)))
        scored = [(c, sum(w * v.get(t, 0.0) for t, w in q.items())) for c, v in zip(self.chunks, self.vecs)]
        return _top(scored, k)


class BM25Retriever:
    name = "bm25"

    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks, self.k1, self.b = chunks, k1, b
        self.tfs = [Counter(tokenize(c.text)) for c in chunks]
        self.lens = [sum(tf.values()) for tf in self.tfs]
        self.avg = sum(self.lens) / max(len(self.lens), 1)
        df = Counter(t for tf in self.tfs for t in tf)
        n = len(chunks)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def search(self, query: str, k: int = 3) -> list[tuple[Chunk, float]]:
        q = query_tokens(query)
        scored = []
        for c, tf, ln in zip(self.chunks, self.tfs, self.lens):
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / self.avg))
            scored.append((c, s))
        return _top(scored, k)


RETRIEVERS = {r.name: r for r in (KeywordRetriever, TfidfRetriever, BM25Retriever)}


def _top(scored: list[tuple[Chunk, float]], k: int) -> list[tuple[Chunk, float]]:
    return [x for x in sorted(scored, key=lambda x: -x[1])[:k] if x[1] > 0]
