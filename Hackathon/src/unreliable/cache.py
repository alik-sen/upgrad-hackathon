"""Tenant-isolated semantic cache (ADR-3).

key       = sc:{sha256(tenant)[:32]}:{sha256(tenant:user:norm_query)}
partition = scidx:{sha256(tenant)[:32]}:{sha256(user)[:16]}   (user scope)  |  ...:shared (tenant-shared, non-personal answers only)
TTL       = 300 s ± 45 s jitter.  Every read re-validates stored tenant_id (defence in depth); mismatch => purge + violation counter.
Backends: in-process (default) | Redis (REDIS_URL; tested against fakeredis).
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time

import numpy as np

from .config import settings
from .telemetry import tele
from .util import norm_query, sha256


_SIG = re.compile(r"[A-Z]{2,}-\d+(?:-\d+)?|\$?\d[\d,.]*|\btier [a-d]\b|\bclass [iv]+\b", re.I)


def signature(nq: str) -> str:
    """Entity/number signature of a query. Dense embeddings barely distinguish SKU-015 from SKU-016, so a semantic hit additionally
    requires an identical signature (otherwise a near-duplicate question about a *different* entity would receive the cached answer)."""
    return "|".join(sorted(m.lower() for m in _SIG.findall(nq)))


def tprefix(tenant_id: str) -> str:
    return f"sc:{sha256(tenant_id)[:32]}"


def entry_key(tenant_id: str, user_id: str, nq: str) -> str:
    return f"{tprefix(tenant_id)}:{sha256(f'{tenant_id}:{user_id}:{nq}')}"


def part_key(tenant_id: str, scope: str) -> str:
    return f"scidx:{sha256(tenant_id)[:32]}:{scope}"


class MemoryBackend:
    def __init__(self, max_entries: int = 200_000):
        self.kv: dict[str, tuple[float, bytes]] = {}
        self.sets: dict[str, dict[str, float]] = {}
        self.max = max_entries

    async def get(self, key: str) -> bytes | None:
        v = self.kv.get(key)
        if not v:
            return None
        if v[0] < time.time():
            self.kv.pop(key, None)
            return None
        return v[1]

    async def set(self, key: str, val: bytes, ttl: float):
        if len(self.kv) >= self.max:
            now = time.time()
            for k in [k for k, (e, _) in list(self.kv.items())[:2000] if e < now] or list(self.kv)[:500]:
                self.kv.pop(k, None)
        self.kv[key] = (time.time() + ttl, val)

    async def delete(self, key: str):
        self.kv.pop(key, None)

    async def sadd(self, skey: str, member: str, cap: int):
        s = self.sets.setdefault(skey, {})
        s[member] = time.time()
        while len(s) > cap:
            s.pop(min(s, key=s.get))

    async def smembers(self, skey: str) -> list[str]:
        return list(self.sets.get(skey, {}))

    async def srem(self, skey: str, member: str):
        self.sets.get(skey, {}).pop(member, None)

    async def flush_prefix(self, prefix: str):
        for k in [k for k in self.kv if k.startswith(prefix)]:
            self.kv.pop(k, None)


class RedisBackend:
    def __init__(self, client):
        self.r = client

    async def get(self, key):
        return await self.r.get(key)

    async def set(self, key, val, ttl):
        await self.r.set(key, val, ex=int(ttl))

    async def delete(self, key):
        await self.r.delete(key)

    async def sadd(self, skey, member, cap):
        await self.r.zadd(skey, {member: time.time()})
        n = await self.r.zcard(skey)
        if n > cap:
            await self.r.zremrangebyrank(skey, 0, n - cap - 1)

    async def smembers(self, skey):
        return [m.decode() if isinstance(m, bytes) else m for m in await self.r.zrange(skey, 0, -1)]

    async def srem(self, skey, member):
        await self.r.zrem(skey, member)

    async def flush_prefix(self, prefix):
        async for k in self.r.scan_iter(match=prefix + "*"):
            await self.r.delete(k)


def make_backend():
    if settings.redis_url:
        try:
            import redis.asyncio as aioredis
            return RedisBackend(aioredis.from_url(settings.redis_url, socket_timeout=0.02))
        except Exception:
            pass
    return MemoryBackend()


class SemanticCache:
    def __init__(self, backend=None, threshold: float | None = None):
        self.b = backend or make_backend()
        self.threshold = threshold if threshold is not None else settings.semantic_threshold
        self.enabled = True

    def _ttl(self) -> float:
        return settings.cache_ttl_s + random.uniform(-settings.cache_ttl_jitter_s, settings.cache_ttl_jitter_s)

    async def _read(self, key: str, tenant_id: str):
        try:
            raw = await asyncio.wait_for(self.b.get(key), 0.02)
        except Exception:
            tele.inc("cache.backend_error")
            return None
        if raw is None:
            return None
        e = json.loads(raw)
        if e.get("tenant_id") != tenant_id or not key.startswith(tprefix(tenant_id)):
            tele.inc("cache.isolation_violation")
            await self.b.delete(key)
            return None
        return e

    async def lookup(self, tenant_id: str, user_id: str, query: str, embed=None):
        """Returns (response_dict, kind) or (None, None). `embed` is an async fn(query)->np.ndarray (called only if a semantic scan is needed)."""
        if not self.enabled:
            return None, None
        nq = norm_query(query)
        e = await self._read(entry_key(tenant_id, user_id, nq), tenant_id)
        if e:
            tele.inc("cache.hit_exact"); return e["response"], "exact"
        e = await self._read(entry_key(tenant_id, "__shared__", nq), tenant_id)
        if e:
            tele.inc("cache.hit_shared"); return e["response"], "shared"
        if embed is not None:
            sig = signature(nq)
            scopes = [sha256(user_id)[:16], "shared"]
            keys = []
            for sc in scopes:
                keys += await self.b.smembers(part_key(tenant_id, sc))
            if keys:
                qv = await embed(query)
                best, bs = None, 0.0
                for k in keys:
                    ent = await self._read(k, tenant_id)
                    if not ent or ent.get("sig") != sig:
                        continue
                    sim = float(np.dot(qv, np.asarray(ent["vec"], dtype=np.float32)))
                    if sim > bs:
                        best, bs = ent, sim
                if best is not None and bs >= self.threshold:
                    tele.inc("cache.hit_semantic"); return best["response"], "semantic"
        tele.inc("cache.miss")
        return None, None

    async def store(self, tenant_id: str, user_id: str, query: str, response: dict, qvec=None, shareable: bool = False):
        if not self.enabled:
            return
        nq = norm_query(query)
        scope_user = "__shared__" if shareable else user_id
        key = entry_key(tenant_id, scope_user, nq)
        ent = {"tenant_id": tenant_id, "user_id": scope_user, "nq": nq, "sig": signature(nq), "response": response,
               "vec": [round(float(x), 4) for x in qvec] if qvec is not None else []}
        try:
            await asyncio.wait_for(self.b.set(key, json.dumps(ent).encode(), self._ttl()), 0.02)
            if qvec is not None:
                await self.b.sadd(part_key(tenant_id, "shared" if shareable else sha256(user_id)[:16]), key, settings.cache_max_entries_per_partition)
        except Exception:
            tele.inc("cache.backend_error")

    async def purge_tenant(self, tenant_id: str):
        await self.b.flush_prefix(tprefix(tenant_id))
        await self.b.flush_prefix(f"scidx:{sha256(tenant_id)[:32]}")
