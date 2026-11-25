"""Grounded generation. Default backend is *extractive* (G-05): answer sentences are selected verbatim from evidence, with [n] citations.
Optional `anthropic` backend (strict quote-only prompt, falls back to extractive on 429/5xx — FMEA #8)."""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

import httpx

from .config import settings
from .rag.bm25 import tokenize
from .util import canon_ids, est_tokens, split_sentences

ABSTAIN_TEXT = "I can't find this in the knowledge sources available to you, so I won't guess. Please rephrase, or contact QA/Operations for help."
ID_RX = re.compile(r"\b(?:SKU-\d{3}|SUP-\d{2}|LOT-\d{5}|SOP-[A-Z]{2}-\d{2})\b")


@dataclass
class Evidence:
    id: str
    note_id: str
    title: str
    text: str
    score: float = 0.0


@dataclass
class Answer:
    text: str
    cited: list[Evidence] = field(default_factory=list)
    backend: str = "extractive"
    prompt_tokens: int = 0
    output_tokens: int = 0


def _sentences(ev: Evidence) -> list[tuple[str, str]]:
    """[(sentence, kind)]: kind 'chunk' | 'anchor' | 'hop' (graph lines after the first are query-filtered hop facts)."""
    out: list[tuple[str, str]] = []
    if ev.id == "graph":
        for i, line in enumerate(ev.text.split("\n")):
            for sent in split_sentences(re.sub(r"^\[[^\]]+\]\s*", "", line)):
                out.append((sent, "anchor" if i == 0 else "hop"))
        return out
    for l in [l for l in ev.text.split("\n") if l.strip()]:
        out += [(x, "chunk") for x in split_sentences(l)]
    return out


class ExtractiveGenerator:
    def generate(self, query: str, evidence: list[Evidence], summary: bool = False, max_sent: int = 4) -> Answer:
        cands: list[tuple[str, Evidence]] = []
        kinds: list[str] = []
        seen: dict[str, int] = {}
        rank = {"chunk": 0, "anchor": 1, "hop": 2}
        for ev in evidence:
            for s, kind in _sentences(ev):
                if s in seen:   # same sentence from a retrieved chunk and the graph: keep the stronger provenance
                    if rank[kind] > rank[kinds[seen[s]]]:
                        kinds[seen[s]] = kind
                    continue
                seen[s] = len(cands); cands.append((s, ev)); kinds.append(kind)
        hop_ids = {i for (s, _), k in zip(cands, kinds) if k == "hop" for i in ID_RX.findall(s)}
        if not cands:
            return Answer(ABSTAIN_TEXT)
        q = canon_ids(query)
        if summary:  # overview questions: take the leading facts of the best note
            first = evidence[0]
            picked = [(s, ev) for (s, ev), k in zip(cands, kinds) if ev is first and k == "chunk"][:4] or cands[:4]
        else:
            ctx = set(tokenize(q)); q_ids = set(ID_RX.findall(q))
            covered: set[str] = set()
            df: dict[str, int] = {}
            toks = [set(tokenize(s)) for s, _ in cands]
            for t in toks:
                for w in t:
                    df[w] = df.get(w, 0) + 1
            n = len(cands)
            idf = lambda w: math.log(1 + n / (1 + df.get(w, 0)))  # noqa: E731
            picked, chosen, per_ev = [], set(), {}
            asks_rule = bool(re.search(r"\b(?:what does|what must|who must|must happen|require|required|allowed|can\b)", q, re.I))
            q_units = set(re.findall(r"\b(hours?|days?|minutes?|months?|years?|°C|meter)\b|(\$)", q, re.I)) and {u.lower() for t in re.findall(r"\b(hours?|days?|minutes?|months?|years?|meter)\b", q, re.I) for u in [t.rstrip("s")]}
            denom = sum(idf(w) for w in ctx) or 1.0
            best0 = None
            for _ in range(max_sent):
                best, bs = None, 0.0
                for i, (s, ev) in enumerate(cands):
                    if i in chosen:
                        continue
                    sids = set(ID_RX.findall(s))
                    sc = sum(idf(w) for w in toks[i] & ctx) / denom
                    sc += 0.25 * len((sids & q_ids) - covered) + 0.05 * ev.score
                    if asks_rule and re.search(r"\b(?:requires?|must|may (?:not )?|shall|at least|at most|more than|within)\b", s, re.I) and re.search(r"\d|quarantin|notif|approv|cleared", s, re.I):
                        sc += 0.2                       # obligation / threshold sentence for a "what is required" question
                        if q_units and any(re.search(rf"\b{u}s?\b", s, re.I) for u in q_units):
                            sc += 0.15                  # same unit as the situation in the question (e.g. hours)
                    if kinds[i] == "hop":
                        sc += 0.35                      # pre-filtered by relevance during graph traversal
                    elif kinds[i] == "anchor" and (sids & hop_ids) - covered:
                        sc += 0.3                       # bridging sentence that links the anchor to a hop node
                    if q_ids and sids and not (sids & q_ids):
                        sc -= 0.5                      # sentence is about a different entity than the question
                    sc *= 0.75 ** per_ev.get(id(ev), 0)  # diversity: spread across evidence passages
                    if sc > bs:
                        best, bs = i, sc
                if best is None or (best0 is not None and bs < 0.45 * best0) or bs < 0.12:
                    break
                best0 = best0 or bs
                chosen.add(best); picked.append(cands[best])
                per_ev[id(cands[best][1])] = per_ev.get(id(cands[best][1]), 0) + 1
                new_ids = set(ID_RX.findall(cands[best][0]))
                covered |= new_ids & (q_ids | hop_ids)
                ctx |= {w for w in toks[best] if w.startswith(("sup-", "sku-", "tier"))} | {f"tier_{m.lower()}" for m in re.findall(r"Tier ([A-D])", cands[best][0])}
                ctx |= set(tokenize(" ".join(new_ids)))
                q_ids |= new_ids
        if not picked:
            return Answer(ABSTAIN_TEXT)
        cited, parts = [], []
        for s, ev in picked:
            if ev not in cited:
                cited.append(ev)
            parts.append(f"{s.rstrip()} [{cited.index(ev) + 1}]")
        text = " ".join(parts)
        return Answer(text, cited, "extractive", output_tokens=est_tokens(text))


LLM_INSTRUCTIONS = ("\n\n[ANSWER RULES] Answer in 1-4 short sentences using ONLY the evidence. If the question needs several facts, combine them "
                    "(e.g. the situation in the question + the rule that applies). Copy numbers, units, IDs and names exactly as written in the evidence. "
                    "Cite evidence items as [1], [2]. If the evidence does not contain the answer, reply exactly: " + ABSTAIN_TEXT)


class AnthropicGenerator:
    """Optional LLM backend (quote-only). Requires ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN; falls back to extractive on any error."""
    def __init__(self):
        self.base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        self.key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY", "")
        self.fallback = ExtractiveGenerator()

    async def generate(self, prompt: str, query: str, evidence: list[Evidence], summary: bool) -> Answer:
        try:
            async with httpx.AsyncClient(timeout=float(os.environ.get("LLM_TIMEOUT_S", 20))) as c:
                r = await c.post(f"{self.base}/v1/messages", headers={"x-api-key": self.key, "authorization": f"Bearer {self.key}", "anthropic-version": "2023-06-01"},
                                 json={"model": settings.llm_model, "max_tokens": 1200, "temperature": 0.0, "messages": [{"role": "user", "content": prompt + LLM_INSTRUCTIONS}]})
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(r.status_code)
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json()["content"]).strip()
            if not text:
                raise RuntimeError("empty completion")
            return Answer(text, evidence[:3], "anthropic", est_tokens(prompt), est_tokens(text))
        except Exception:  # noqa: BLE001  FMEA #8: deterministic fallback
            a = self.fallback.generate(query, evidence, summary)
            a.backend = "extractive(fallback)"
            return a


VERIFY_PROMPT = ("You are a strict fact-checker. EVIDENCE and QUESTION are below, followed by numbered CLAIMS from a draft answer. "
                 "A claim is SUPPORTED only if it is stated in the EVIDENCE, or is a direct logical consequence of EVIDENCE together with facts given in the QUESTION "
                 "(e.g. applying a threshold rule from the evidence to numbers from the question). Anything else (outside knowledge, invented numbers, names or rules) is UNSUPPORTED. "
                 "Reply with ONLY a JSON array of strings, one per claim, each \"SUPPORTED\" or \"UNSUPPORTED\".\n\n")


async def llm_verify_claims(gen: "AnthropicGenerator", question: str, claims: list[str], evidence: list[str]) -> list[bool] | None:
    """Independent NLI-style judge (separate call, temperature 0, sees only question+evidence+claims). None => judge unavailable (caller fails closed)."""
    import json
    body = VERIFY_PROMPT + "QUESTION: " + question + "\n\nEVIDENCE:\n" + "\n".join(f"- {e}" for e in evidence) + "\n\nCLAIMS:\n" + "\n".join(f"{i+1}. {c}" for i, c in enumerate(claims))
    try:
        async with httpx.AsyncClient(timeout=float(os.environ.get("LLM_JUDGE_TIMEOUT_S", 25))) as c:
            r = await c.post(f"{gen.base}/v1/messages", headers={"x-api-key": gen.key, "authorization": f"Bearer {gen.key}", "anthropic-version": "2023-06-01"},
                             json={"model": settings.llm_model, "max_tokens": 1200, "temperature": 0.0, "messages": [{"role": "user", "content": body}]})
        r.raise_for_status()
        txt = "".join(b.get("text", "") for b in r.json()["content"])
        arr = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
        if len(arr) != len(claims):
            return None
        return [str(a).upper().startswith("SUPPORTED") for a in arr]
    except Exception:  # noqa: BLE001
        return None
