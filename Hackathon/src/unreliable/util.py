"""Small shared helpers: text normalisation, token estimate, entity extraction, hashing."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

SKU_RE = re.compile(r"\bSKU-\d{3}\b")
LOT_RE = re.compile(r"\bLOT-\d{5}\b")
# tolerant (typo) forms used by the auto-repair layer
SKU_LOOSE = re.compile(r"\bsku[\s_\-]*0*(\d{1,3})\b", re.I)
LOT_LOOSE = re.compile(r"\blot[\s_\-]*0*(\d{1,5})\b", re.I)
_WS = re.compile(r"\s+")


def est_tokens(text: str) -> int:
    """Token estimate: ~4 chars/token (documented in G-09/G-10; same estimator for baseline and optimised)."""
    return max(1, (len(text) + 3) // 4)


def canon_ids(text: str) -> str:
    """Canonicalise loose SKU/LOT mentions: 'sku99'->'SKU-099', 'lot 44'->'LOT-00044'."""
    text = SKU_LOOSE.sub(lambda m: f"SKU-{int(m.group(1)):03d}", text)
    return LOT_LOOSE.sub(lambda m: f"LOT-{int(m.group(1)):05d}", text)


def norm_query(q: str) -> str:
    q = unicodedata.normalize("NFKC", q).lower()
    q = canon_ids(q).lower()
    q = re.sub(r"[^\w\s\-]", " ", q)
    return _WS.sub(" ", q).strip()


def extract_entities(text: str) -> dict[str, list[str]]:
    t = canon_ids(text)
    return {"sku": sorted(set(SKU_RE.findall(t))), "lot": sorted(set(LOT_RE.findall(t)))}


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def args_hash(tool: str, args: dict) -> str:
    return sha256(tool + ":" + canonical_json(args))[:24]


def percentile(values, p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\[])")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text.replace("\n", " ")) if s.strip()]
