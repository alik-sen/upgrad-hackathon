"""Claim-to-chunk verifier (NLI proxy, G-08) used by the Verification subagent, the RAGAS auditor and the offline evals.

A claim (answer sentence) is SUPPORTED iff (a) every number / identifier in it occurs in the evidence and (b) >= 0.6 of its content-word
stems occur in a single evidence passage (best passage; sentence-level windows allowed).
"""
from __future__ import annotations

import re

from .rag.bm25 import tokenize
from .util import split_sentences

NUM = re.compile(r"\$?\d[\d,.]*(?:\s?(?:°C|%))?|[A-Z]{2,}-\d+(?:-\d+)?")


def _norm(s: str) -> str:
    return s.replace("–", "-").replace(" ", " ").lower()


def claim_supported(claim: str, evidence: list[str], min_overlap: float = 0.6, question: str = "") -> bool:
    c = _norm(claim)
    ev = [_norm(e) for e in evidence]
    joined = " ".join(ev)
    qn = _norm(question)   # numbers/IDs the user typed may be restated; the claim's *content* must still be supported by evidence
    for n in NUM.findall(claim):
        if _norm(n).strip(" .,") not in joined and _norm(n).strip(" .,") not in qn:
            return False
    ct = set(tokenize(c))
    if not ct:
        return True
    best = 0.0
    for e in ev:
        et = set(tokenize(e))
        best = max(best, len(ct & et) / len(ct))
    return best >= min_overlap


def verify_answer(answer: str, evidence: list[str], question: str = "") -> tuple[float, list[str]]:
    claims = [re.sub(r"\s*\[\d+\]\s*", " ", s).strip() for s in split_sentences(answer)]
    claims = [c for c in claims if len(c) > 3]
    if not claims:
        return 1.0, []
    bad = [c for c in claims if not claim_supported(c, evidence, question=question)]
    return 1.0 - len(bad) / len(claims), bad
