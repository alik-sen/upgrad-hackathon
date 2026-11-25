"""Pydantic v2 contracts shared by API, harness, MCP servers (design.md §1.2)."""
from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .config import settings

TenantId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_\-]{1,64}$")]
SKU = Annotated[str, StringConstraints(pattern=r"^SKU-\d{3}$")]
LOT = Annotated[str, StringConstraints(pattern=r"^LOT-\d{5}$")]


class Role(str, Enum):
    reader = "reader"
    operator = "operator"
    approver = "approver"


class Tier(str, Enum):
    T1 = "TIER_1_READ"
    T2 = "TIER_2_COMPUTE"
    T3 = "TIER_3_MUTATE"


ROLE_MAX_TIER = {Role.reader: 1, Role.operator: 3, Role.approver: 3}  # operator may *request* T3 (HITL gated)
TIER_NUM = {Tier.T1: 1, Tier.T2: 2, Tier.T3: 3}
# T2 compute is allowed for operator/approver only (reader limited to T1)
ROLE_ALLOWED_TIERS = {Role.reader: {Tier.T1}, Role.operator: {Tier.T1, Tier.T2, Tier.T3}, Role.approver: {Tier.T1, Tier.T2, Tier.T3}}


class Strict(BaseModel):
    """extra=forbid; strictness (no str->int/UUID/Enum coercion) is applied at call time by the harness: model_validate(obj, strict=True)."""
    model_config = ConfigDict(extra="forbid")


class ToolCtx(Strict):
    tenant_id: TenantId
    user_id: Annotated[str, StringConstraints(max_length=64)]
    role: Role = Role.reader
    trace_id: str = ""
    session_id: str = ""


# ---------------- MCP tool inputs/outputs ----------------
class HybridRetrieveIn(Strict):
    tenant_id: TenantId
    query: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    top_k: int = Field(4, ge=1, le=10)
    bm25_k: int = Field(50, ge=5, le=100)
    dense_k: int = Field(50, ge=5, le=100)
    k1: float = Field(1.25, ge=0.5, le=3.0)
    b: float = Field(0.75, ge=0.0, le=1.0)
    rrf_k: int = Field(60, ge=1, le=200)
    pool: int = Field(default_factory=lambda: settings.rerank_pool, ge=4, le=100)  # candidates sent to cross-encoder (fusion pool is 40)
    threshold: float = Field(default_factory=lambda: settings.rerank_threshold, ge=0.0, le=1.0)
    query_vec: list[float] | None = None
    debug: bool = False


class Chunk(BaseModel):
    chunk_id: str
    note_id: str
    title: str = ""
    tenant: str = "all"
    text: str
    bm25: float | None = None
    dense: float | None = None
    rrf: float = 0.0
    rerank: float | None = None
    rank: int = 0


class HybridRetrieveOut(BaseModel):
    chunks: list[Chunk]
    top_score: float
    abstain: bool
    degraded: bool = False
    timings_ms: dict[str, float]
    candidates: dict[str, Any] | None = None


class ExpandIn(Strict):
    query: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    tenant_id: TenantId = "all"
    hint_note: str | None = None
    depth: int = Field(2, ge=0, le=2)
    budget_tokens: int = Field(320, ge=50, le=400)


class EmbedIn(Strict):
    query: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class LookupSkuIn(Strict):
    ctx: ToolCtx
    sku: SKU


class LookupLotIn(Strict):
    ctx: ToolCtx
    lot: LOT


class ComputeShelfLifeIn(Strict):
    ctx: ToolCtx
    lot: LOT
    as_of: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]


class ValidateFormIn(Strict):
    ctx: ToolCtx
    form_type: Literal["deviation", "release", "transfer"]
    fields: dict[str, str]


class UpdateLotStatusIn(Strict):
    ctx: ToolCtx
    idem_key: UUID
    lot: LOT
    new_status: Literal["HOLD", "QUARANTINE", "RELEASED", "SCRAP"]
    reason: Annotated[str, StringConstraints(min_length=3, max_length=200)]


class ReleaseBatchIn(Strict):
    ctx: ToolCtx
    idem_key: UUID
    lot: LOT


class SubmitFormIn(Strict):
    ctx: ToolCtx
    idem_key: UUID
    form_type: Literal["deviation", "release", "transfer"]
    fields: dict[str, str]


class PutFactIn(Strict):
    ctx: ToolCtx
    fact: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class GetFactsIn(Strict):
    ctx: ToolCtx
    limit: int = Field(10, ge=1, le=50)


# ---------------- tool registry metadata ----------------
class ToolSpec(BaseModel):
    name: str
    server: Literal["hybrid-rag", "state-store", "graphify-vault"]
    tier: Tier
    model: Any  # pydantic class
    needs_idem: bool = False


# ---------------- API ----------------
class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: TenantId
    user_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    query: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    session_id: str | None = None
    debug: bool = False


class Citation(BaseModel):
    chunk_id: str
    note_id: str
    title: str = ""
    score: float | None = None


class QueryResponse(BaseModel):
    request_id: str
    trace_id: str
    tenant_id: str
    user_id: str
    route: str
    status: str
    answer: str
    citations: list[Citation] = []
    abstained: bool = False
    faithfulness: float = 1.0
    cache_hit: bool = False
    steps: int = 0
    hitl: dict[str, Any] | None = None
    latency_ms: float = 0.0
    timings: dict[str, float] = {}
    trace: list[dict[str, Any]] | None = None
    tokens: dict[str, int] | None = None


class AppendAuditIn(Strict):
    tenant_id: TenantId
    events: list[dict[str, Any]] = Field(max_length=500)


class ListHitlIn(Strict):
    tenant_id: TenantId
    status: str | None = None


class DecideHitlIn(Strict):
    ctx: ToolCtx
    approval_id: str
    decision: Literal["approve", "reject"]
    reviewer_key: str


class VerifyChainIn(Strict):
    tenant_id: TenantId
