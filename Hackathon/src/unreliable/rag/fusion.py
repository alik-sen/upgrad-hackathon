"""Reciprocal Rank Fusion (k=60), dedup by chunk_hash."""
from __future__ import annotations


def rrf(rank_lists: list[list[int]], k: int = 60, hashes: list[str] | None = None, pool: int = 40) -> list[tuple[int, float]]:
    """rank_lists: list of ranked chunk indices (best first). Returns [(idx, score)] best first, deduped by hash, truncated to `pool`."""
    score: dict[int, float] = {}
    for rl in rank_lists:
        for r, idx in enumerate(rl, start=1):
            score[idx] = score.get(idx, 0.0) + 1.0 / (k + r)
    out, seen = [], set()
    for idx, s in sorted(score.items(), key=lambda kv: -kv[1]):
        if hashes is not None:
            h = hashes[idx]
            if h in seen:
                continue
            seen.add(h)
        out.append((idx, s))
        if len(out) >= pool:
            break
    return out
