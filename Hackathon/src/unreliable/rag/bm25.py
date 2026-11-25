"""Stage 1 — sparse lexical: BM25Okapi + PorterStemmer, pre-tokenised corpus (k1=1.25, b=0.75)."""
from __future__ import annotations

import re
from functools import lru_cache

import numpy as np
from nltk.stem import PorterStemmer
from rank_bm25 import BM25Okapi

_ps = PorterStemmer()
STOP = set("a an the of to in on for and or is are be by with as at from that this it its what which who how when where why do does did can could should would must may "
           "please me my i we you your our their tell about into than then there these those if not no any all".split())
TOK = re.compile(r"[a-z]+-\d+|\d+(?:[.,]\d+)*|[a-z]+")


@lru_cache(maxsize=50_000)
def _stem(w: str) -> str:
    return _ps.stem(w)


def tokenize(text: str) -> list[str]:
    out = []
    for t in TOK.findall(text.lower()):
        if "-" in t and t[0].isalpha():  # SKU-017 -> keep whole + parts
            out.append(t)
            out.append(t.split("-")[1].lstrip("0") or "0")
        elif t in STOP:
            continue
        else:
            out.append(_stem(t) if t.isalpha() else t)
    return out


class BM25Index:
    def __init__(self, texts: list[str]):
        self.tokens = [tokenize(t) for t in texts]
        self._by_params: dict[tuple[float, float], BM25Okapi] = {}

    def _model(self, k1: float, b: float) -> BM25Okapi:
        key = (round(k1, 3), round(b, 3))
        if key not in self._by_params:
            self._by_params[key] = BM25Okapi(self.tokens, k1=k1, b=b)
        return self._by_params[key]

    def scores(self, query: str, k1: float = 1.25, b: float = 0.75) -> np.ndarray:
        q = tokenize(query)
        return np.asarray(self._model(k1, b).get_scores(q), dtype=np.float32) if q else np.zeros(len(self.tokens), np.float32)
