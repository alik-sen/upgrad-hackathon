"""PII redactor (PRE_EXECUTION, priority 10, fail-closed). Raw PII is never persisted: redaction happens before cache/audit/DB/LLM."""
from __future__ import annotations

import re
import unicodedata

from .base import hookimpl

_ZW = dict.fromkeys(map(ord, "​‌‍‎‏⁠﻿­"), None)
# Cyrillic/Greek look-alikes -> Latin
_HOMO = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "һ": "h", "ո": "n", "ɡ": "g",
                       "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C", "Х": "X", "Т": "T", "М": "M", "Н": "H", "К": "K", "В": "B",
                       "α": "a", "ο": "o", "ρ": "p", "ν": "v", "τ": "t", "ι": "i", "κ": "k", "ε": "e"})
NUMW = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)"
TLD = r"(?:com|org|net|io|edu|gov|co|info|biz|us|uk|de|fr|ca|au|in|app|dev|ai)"
AT = r"(?:\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*|\s+at\s+|\s*@\s*)"
DOT = r"(?:\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*|\s+dot\s+|\s*\.\s*)"
PATTERNS = [
    ("EMAIL", re.compile(rf"[A-Za-z0-9._%+\-]+{AT}[A-Za-z0-9\-]+(?:{DOT}[A-Za-z0-9\-]+)*{DOT}{TLD}\b", re.I)),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+\-]+\s*@\s*[A-Za-z0-9\-]+(?:\s*\.\s*[A-Za-z0-9\-]+)+", re.I)),
    ("SSN", re.compile(rf"(?<!\d)\d{{3}}[-.\s]\d{{2}}[-.\s]\d{{4}}(?!\d)")),
    ("SSN", re.compile(rf"(?<![\w-])(?:\d\s){{8}}\d(?!\w)")),
    ("SSN", re.compile(rf"\b(?:{NUMW}[\s,\-]*){{9}}\b", re.I)),
    ("SSN", re.compile(r"(?<![\w-])\d{9}(?!\w)")),
    ("PHONE", re.compile(r"(?<![\w-])(?:\+?\d{1,2}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}(?![\w-])")),
    ("PHONE", re.compile(r"(?<![\w-])\d{10}(?![\w-])")),
    ("CARD", re.compile(r"(?<!\d)(?:\d[ \-]?){13,16}(?!\d)")),
]


def _luhn(num: str) -> bool:
    d = [int(c) for c in num if c.isdigit()]
    if not 13 <= len(d) <= 16:
        return False
    s = 0
    for i, x in enumerate(reversed(d)):
        if i % 2:
            x *= 2
            x -= 9 if x > 9 else 0
        s += x
    return s % 10 == 0


def fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_ZW).translate(_HOMO)


def redact(text: str) -> tuple[str, dict]:
    t = fold(text)
    found: dict[str, int] = {}
    for kind, rx in PATTERNS:
        def sub(m, kind=kind):
            if kind == "CARD" and not _luhn(m.group(0)):
                return m.group(0)
            found[kind] = found.get(kind, 0) + 1
            return f"[{kind}]"
        t = rx.sub(sub, t)
    return t, found


class PIIRedactor:
    @hookimpl
    def pre_execute(self, state, session_risk):
        state.query, state.pii_found = redact(state.query)
