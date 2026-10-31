"""How well a short text answers a question (2026-10-25): BM25 over Chinese single characters and character pairs.

Adapted from HKUDS/Vibe-Trading (MIT, commit 41f333b6): the CJK unigram + bigram tokens of
`agent/src/memory/search_index.py` (`_prepare_cjk` / `_cjk_query_tokens`) and `compute_idf` /
`compute_bm25_score` of `agent/src/memory/semantic_links.py`, kept to the standard library (the packaged
desktop Runtime ships without numpy). Changed here: one tokenizer for query and document, and common function
characters dropped as single characters (their pairs stay) — in a few short memories one shared 的 or 看 would
otherwise rank an unrelated line first.
"""
from __future__ import annotations

import math
import re
from collections import Counter

K1 = 1.5
B = 0.75
_TOKEN = re.compile(r"[a-zA-Z0-9]{2,}|[一-鿿㐀-䶿]")
# Single characters that say nothing about a topic; a product's one-character name (铜 铝 锌 金 银) is not here.
_FUNCTION_CHARS = frozenset("的了我你他她它是在吗呢吧啊呀哦么怎什看说想做一下这那个和就都也要不有没把被给让对从到还又再很太")


def tokens(text: str) -> list[str]:
    """Latin words (2+ characters, lower case) and, per run of Chinese characters, its characters and
    adjacent pairs — "螺纹钢" → 螺 纹 钢 螺纹 纹钢. Duplicates are kept: BM25 counts term frequency."""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        out.extend(char for char in run if char not in _FUNCTION_CHARS)
        out.extend(run[index] + run[index + 1] for index in range(len(run) - 1))
        run.clear()

    for token in _TOKEN.findall(text.lower()):
        if len(token) == 1 and not token.isascii():
            run.append(token)
        else:
            flush()
            out.append(token)
    flush()
    return out


def idf(corpus: list[list[str]]) -> dict[str, float]:
    """IDF(t) = log((N − n + 0.5) / (n + 0.5) + 1): never negative, even when every document holds the term."""
    frequency: Counter[str] = Counter()
    for document in corpus:
        frequency.update(set(document))
    count = len(corpus)
    return {term: math.log((count - seen + 0.5) / (seen + 0.5) + 1) for term, seen in frequency.items()}


def bm25(query: list[str], document: list[str], weights: dict[str, float], average_length: float) -> float:
    if not document or average_length <= 0:
        return 0.0
    frequency = Counter(document)
    scale = K1 * (1 - B + B * len(document) / average_length)
    return sum(weights.get(term, 0.0) * frequency[term] * (K1 + 1) / (frequency[term] + scale)
               for term in set(query) if frequency[term])


def rank(query: str, documents: list[str]) -> list[float]:
    """Each document's BM25 score against the query (0 = nothing in common), in the documents' order."""
    corpus = [tokens(document) for document in documents]
    if not corpus:
        return []
    weights = idf(corpus)
    average = sum(len(document) for document in corpus) / len(corpus)
    asked = tokens(query)
    return [bm25(asked, document, weights, average) for document in corpus]
