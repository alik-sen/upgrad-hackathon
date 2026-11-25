"""Obsidian vault loader + chunker (512-token chunks, 64-token overlap; whitespace tokens)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

FM = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)
WIKI = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")


@dataclass
class Note:
    id: str
    title: str
    domain: str
    risk_tier: str
    version: str
    tenant: str
    summary: str
    body: str
    links: list[str] = field(default_factory=list)


@dataclass
class ChunkRec:
    chunk_id: str
    note_id: str
    title: str
    text: str
    tenant: str
    chunk_hash: str


def load_vault(vault: Path) -> list[Note]:
    notes = []
    for p in sorted(vault.glob("*.md")):
        raw = p.read_text()
        m = FM.match(raw)
        meta, body = (yaml.safe_load(m.group(1)), m.group(2)) if m else ({}, raw)
        title = next((l[2:].strip() for l in body.splitlines() if l.startswith("# ")), p.stem)
        summ = next((l.split(":", 1)[1].strip() for l in body.splitlines() if l.startswith("Summary:")), "")
        notes.append(Note(id=str(meta.get("id", p.stem)), title=title, domain=str(meta.get("domain", "")), risk_tier=str(meta.get("risk_tier", "")),
                          version=str(meta.get("version", "")), tenant=str(meta.get("tenant", "all")), summary=summ, body=body.strip(),
                          links=[l.strip().lower() for l in WIKI.findall(body)]))
    return notes


def chunk_notes(notes: list[Note], size: int = 512, overlap: int = 64) -> list[ChunkRec]:
    """Chunk = the note's fact lines (the Summary line duplicates them and is dropped). Lines are packed up to `size` words with `overlap` words carried over."""
    out: list[ChunkRec] = []
    for n in notes:
        lines = [l[2:].strip() for l in n.body.splitlines() if l.startswith("- ")]
        if not lines:
            lines = [l.strip() for l in n.body.splitlines() if l.strip() and not l.startswith(("#", "Related:", "Summary:", "Facts:"))]
        cur: list[str] = []
        cur_w = 0
        pieces: list[str] = []
        for ln in lines:
            w = len(ln.split())
            if cur and cur_w + w > size:
                pieces.append("\n".join(cur))
                tail = " ".join(" ".join(cur).split()[-overlap:])
                cur, cur_w = [tail], len(tail.split())
            cur.append(ln); cur_w += w
        if cur:
            pieces.append("\n".join(cur))
        for i, piece in enumerate(pieces):
            out.append(ChunkRec(chunk_id=f"{n.id}#{i}", note_id=n.id, title=n.title, text=piece, tenant=n.tenant,
                                chunk_hash=hashlib.sha1(piece.encode()).hexdigest()))
    return out
