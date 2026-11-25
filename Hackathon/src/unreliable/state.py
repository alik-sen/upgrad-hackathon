"""Persistent state store (SQLite WAL; Postgres-compatible schema — G-15).

Every query filters on tenant_id (design §1.1/§1.2). Tier-3 mutations are *only* executed through `decide_hitl`.
Audit log is SHA-256 hash-chained per tenant: hash = sha256(prev_hash + canonical(payload)).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import date
from pathlib import Path

from .config import settings
from .util import canonical_json, sha256

SCHEMA = """
CREATE TABLE IF NOT EXISTS skus(tenant_id TEXT, sku TEXT, data TEXT, PRIMARY KEY(tenant_id, sku));
CREATE TABLE IF NOT EXISTS lots(tenant_id TEXT, lot TEXT, sku TEXT, status TEXT, qty INTEGER, expiry TEXT, hold_reason TEXT, PRIMARY KEY(tenant_id, lot));
CREATE TABLE IF NOT EXISTS session_facts(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, session_id TEXT, fact TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS ix_facts ON session_facts(tenant_id, session_id);
CREATE TABLE IF NOT EXISTS hitl(approval_id TEXT PRIMARY KEY, tenant_id TEXT, requester TEXT, action TEXT, params TEXT, status TEXT, reviewer TEXT, created REAL, decided REAL, executed INTEGER DEFAULT 0, idem_key TEXT);
CREATE INDEX IF NOT EXISTS ix_hitl ON hitl(tenant_id, status);
CREATE TABLE IF NOT EXISTS idem(tenant_id TEXT, idem_key TEXT, result TEXT, PRIMARY KEY(tenant_id, idem_key));
CREATE TABLE IF NOT EXISTS audit_chain(tenant_id TEXT, seq INTEGER, prev_hash TEXT, hash TEXT, payload TEXT, ts REAL, PRIMARY KEY(tenant_id, seq));
CREATE TABLE IF NOT EXISTS forms(tenant_id TEXT, form_id TEXT, form_type TEXT, fields TEXT, ts REAL, PRIMARY KEY(tenant_id, form_id));
"""
GENESIS = "0" * 64


class StateError(Exception):
    def __init__(self, code: int, name: str, msg: str):
        super().__init__(f"{name}: {msg}")
        self.code, self.name, self.msg = code, name, msg


class StateStore:
    def __init__(self, path: Path | str | None = None, seed_path: Path | None = None):
        self.path = str(path or settings.db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._wlock = threading.Lock()
        self._seed = json.loads((seed_path or settings.data_dir / "inventory_seed.json").read_text())
        self._seeded: set[str] = set()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.row_factory = sqlite3.Row
            self._local.c = c
        return c

    # ---- tenancy ----
    def ensure_tenant(self, tenant_id: str):
        if tenant_id in self._seeded:
            return
        c = self._conn()
        with self._wlock:
            if c.execute("SELECT 1 FROM skus WHERE tenant_id=? LIMIT 1", (tenant_id,)).fetchone() is None:
                c.execute("BEGIN IMMEDIATE")
                try:
                    c.executemany("INSERT OR IGNORE INTO skus VALUES(?,?,?)", [(tenant_id, k, json.dumps(v)) for k, v in self._seed["skus"].items()])
                    c.executemany("INSERT OR IGNORE INTO lots VALUES(?,?,?,?,?,?,NULL)",
                                  [(tenant_id, v["lot"], v["sku"], v["status"], v["qty"], v["expiry"]) for v in self._seed["lots"].values()])
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK"); raise
        self._seeded.add(tenant_id)

    # ---- T1 reads ----
    def lookup_sku(self, tenant_id: str, sku: str) -> dict:
        self.ensure_tenant(tenant_id)
        r = self._conn().execute("SELECT data FROM skus WHERE tenant_id=? AND sku=?", (tenant_id, sku)).fetchone()
        if not r:
            raise StateError(-32002, "NOT_FOUND", f"{sku} not found")
        return json.loads(r["data"])

    def lookup_lot(self, tenant_id: str, lot: str) -> dict:
        self.ensure_tenant(tenant_id)
        r = self._conn().execute("SELECT lot,sku,status,qty,expiry,hold_reason FROM lots WHERE tenant_id=? AND lot=?", (tenant_id, lot)).fetchone()
        if not r:
            raise StateError(-32002, "NOT_FOUND", f"{lot} not found")
        return dict(r)

    # ---- T2 compute ----
    def compute_shelf_life(self, tenant_id: str, lot: str, as_of: str) -> dict:
        row = self.lookup_lot(tenant_id, lot)
        days = (date.fromisoformat(row["expiry"]) - date.fromisoformat(as_of)).days
        return {"lot": lot, "expiry": row["expiry"], "days_remaining": days, "expired": days < 0}

    def validate_form(self, form_type: str, fields: dict) -> dict:
        req = {"deviation": ["lot", "description"], "release": ["lot", "approver"], "transfer": ["lot", "from_site", "to_site"]}[form_type]
        errs = [f"missing field: {k}" for k in req if not fields.get(k)]
        return {"valid": not errs, "errors": errs}

    # ---- T3 request (never executes) ----
    def create_hitl(self, tenant_id: str, requester: str, action: str, params: dict, idem_key: str) -> dict:
        self.ensure_tenant(tenant_id)
        c = self._conn()
        with self._wlock:
            prev = c.execute("SELECT result FROM idem WHERE tenant_id=? AND idem_key=?", (tenant_id, idem_key)).fetchone()
            if prev:
                out = json.loads(prev["result"]); out["replayed"] = True
                return out
            aid = str(uuid.uuid4())
            c.execute("INSERT INTO hitl(approval_id,tenant_id,requester,action,params,status,created,idem_key) VALUES(?,?,?,?,?,'PENDING_APPROVAL',?,?)",
                      (aid, tenant_id, requester, action, canonical_json(params), time.time(), idem_key))
            out = {"status": "PENDING_APPROVAL", "approval_id": aid, "action": action, "executed": False}
            c.execute("INSERT INTO idem VALUES(?,?,?)", (tenant_id, idem_key, json.dumps(out)))
        return out

    def list_hitl(self, tenant_id: str, status: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM hitl WHERE tenant_id=?", [tenant_id]
        if status:
            q += " AND status=?"; a.append(status)
        return [dict(r) for r in self._conn().execute(q + " ORDER BY created DESC LIMIT 200", a)]

    def decide_hitl(self, tenant_id: str, approval_id: str, decision: str, reviewer: str) -> dict:
        c = self._conn()
        with self._wlock:
            r = c.execute("SELECT * FROM hitl WHERE tenant_id=? AND approval_id=?", (tenant_id, approval_id)).fetchone()
            if not r:
                raise StateError(-32002, "NOT_FOUND", "approval not found")
            if r["status"] != "PENDING_APPROVAL":
                raise StateError(-32004, "IDEMPOTENT_REPLAY", f"already {r['status']}")
            if reviewer == r["requester"]:
                raise StateError(-32001, "TIER_DENIED", "four-eyes: reviewer must differ from requester")
            status = "APPROVED" if decision == "approve" else "REJECTED"
            executed = 0
            if status == "APPROVED":
                self._execute_locked(c, tenant_id, r["action"], json.loads(r["params"]))
                executed = 1
            c.execute("UPDATE hitl SET status=?, reviewer=?, decided=?, executed=? WHERE approval_id=?", (status, reviewer, time.time(), executed, approval_id))
        return {"approval_id": approval_id, "status": status, "executed": bool(executed)}

    def _execute_locked(self, c, tenant_id: str, action: str, p: dict):
        if action == "update_lot_status":
            c.execute("UPDATE lots SET status=?, hold_reason=? WHERE tenant_id=? AND lot=?", (p["new_status"], p.get("reason"), tenant_id, p["lot"]))
        elif action == "release_batch":
            row = c.execute("SELECT status FROM lots WHERE tenant_id=? AND lot=?", (tenant_id, p["lot"])).fetchone()
            if row and row["status"] in ("HOLD", "QUARANTINE"):
                raise StateError(-32001, "TIER_DENIED", "lot on hold/quarantine must be cleared before release (SOP-LR-06)")
            c.execute("UPDATE lots SET status='RELEASED' WHERE tenant_id=? AND lot=?", (tenant_id, p["lot"]))
        elif action == "submit_form":
            c.execute("INSERT INTO forms VALUES(?,?,?,?,?)", (tenant_id, str(uuid.uuid4()), p["form_type"], canonical_json(p["fields"]), time.time()))

    # ---- facts ----
    def put_fact(self, tenant_id: str, session_id: str, fact: str):
        with self._wlock:
            self._conn().execute("INSERT INTO session_facts(tenant_id,session_id,fact,ts) VALUES(?,?,?,?)", (tenant_id, session_id, fact, time.time()))

    def get_facts(self, tenant_id: str, session_id: str, limit: int = 10) -> list[str]:
        return [r["fact"] for r in self._conn().execute(
            "SELECT fact FROM session_facts WHERE tenant_id=? AND session_id=? ORDER BY id DESC LIMIT ?", (tenant_id, session_id, limit))]

    # ---- audit hash chain ----
    def append_audit(self, tenant_id: str, payload: dict) -> dict:
        return self.append_audit_batch(tenant_id, [payload])[-1]

    def append_audit_batch(self, tenant_id: str, payloads: list[dict]) -> list[dict]:
        c = self._conn()
        out = []
        with self._wlock:
            c.execute("BEGIN IMMEDIATE")
            try:
                r = c.execute("SELECT seq,hash FROM audit_chain WHERE tenant_id=? ORDER BY seq DESC LIMIT 1", (tenant_id,)).fetchone()
                seq, prev = (r["seq"], r["hash"]) if r else (0, GENESIS)
                for p in payloads:
                    seq += 1
                    body = canonical_json(p)
                    h = sha256(prev + body)
                    c.execute("INSERT INTO audit_chain VALUES(?,?,?,?,?,?)", (tenant_id, seq, prev, h, body, time.time()))
                    out.append({"seq": seq, "hash": h})
                    prev = h
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK"); raise
        return out

    def verify_chain(self, tenant_id: str) -> dict:
        prev, n = GENESIS, 0
        for r in self._conn().execute("SELECT seq,prev_hash,hash,payload FROM audit_chain WHERE tenant_id=? ORDER BY seq", (tenant_id,)):
            if r["prev_hash"] != prev or sha256(prev + r["payload"]) != r["hash"]:
                return {"ok": False, "broken_at": r["seq"], "checked": n}
            prev, n = r["hash"], n + 1
        return {"ok": True, "checked": n}

    def audit_events(self, tenant_id: str, limit: int = 100) -> list[dict]:
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in self._conn().execute(
            "SELECT seq,hash,payload,ts FROM audit_chain WHERE tenant_id=? ORDER BY seq DESC LIMIT ?", (tenant_id, limit))]

    def tenants(self) -> list[str]:
        return [r[0] for r in self._conn().execute("SELECT DISTINCT tenant_id FROM audit_chain")]
