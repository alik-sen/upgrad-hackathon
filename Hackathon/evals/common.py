"""Shared eval helpers: dataset loading, API client, text normalisation for key-fact matching."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
API = os.environ.get("API_URL", "http://127.0.0.1:8000")
ADMIN = {"X-Admin-Key": os.environ.get("REVIEWER_API_KEY", "dev-reviewer-key")}


def load_jsonl(name: str) -> list[dict]:
    return [json.loads(l) for l in (ROOT / "data" / name).read_text().splitlines() if l.strip()]


def norm(s: str) -> str:
    return s.lower().replace("–", "-").replace(" ", " ")


def key_fact_hit(fact: str, text: str) -> bool:
    return norm(fact) in norm(text)


class Api:
    def __init__(self, url: str = API):
        self.c = httpx.AsyncClient(base_url=url, timeout=30.0, limits=httpx.Limits(max_connections=200))

    async def query(self, tenant: str, user: str, q: str, role: str = "reader", debug: bool = True, session: str | None = None) -> dict:
        body = {"tenant_id": tenant, "user_id": user, "query": q, "debug": debug}
        if session:
            body["session_id"] = session
        r = await self.c.post("/api/v1/agent/query", json=body, headers={"X-Tenant-ID": tenant, "X-Role": role})
        d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        d["_http"] = r.status_code
        return d

    async def admin(self, method: str, path: str, **kw):
        r = await self.c.request(method, f"/api/v1/admin/{path}", headers=ADMIN, **kw)
        return r.json()

    async def status(self) -> dict:
        return await self.admin("GET", "status")

    async def close(self):
        await self.c.aclose()


async def gather_limited(coros, n: int):
    sem = asyncio.Semaphore(n)

    async def run(c):
        async with sem:
            return await c
    return await asyncio.gather(*(run(c) for c in coros))
