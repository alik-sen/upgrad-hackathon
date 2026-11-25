"""Triage agent: ultra-fast distilled classifier (TF-IDF word+char n-grams -> logistic regression), deterministic (temperature 0).
Returns (intent, domain, risk_tier, complexity) and routes to DIRECT_ANSWER | HYBRID_RAG | TRANSACTION_EXEC | REJECT_OOS."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import FeatureUnion, Pipeline

from .config import settings
from .util import canon_ids, sha256

ROUTES = ["DIRECT_ANSWER", "HYBRID_RAG", "TRANSACTION_EXEC", "REJECT_OOS"]
T3_RX = re.compile(r"\b(?:put|place|set|mark|move)\b.{0,40}\b(?:on hold|hold|quarantine|scrap|released?)\b|\brelease\b.{0,15}\b(?:batch|lot)\b|\bupdate\b.{0,30}\bstatus\b|\b(?:submit|file)\b.{0,20}\bform\b|"
                   r"\bchange\b.{0,30}\bstatus\b|\bquarantine\b\s+(?:lot-|sku-)|\bscrap\b|\bI need\b.{0,15}\breleased\b", re.I)
T2_RX = re.compile(r"\bhow many days\b|\bexpir|\bvalidate\b|\bcompute\b|\bcalculate\b|within expiry", re.I)
HOP_RX = re.compile(r"\bsupplier of\b|\bsupplies\b.{0,10}\bsku|\bcategory of\b|\bgoverns?\b|\bcompany that supplies\b|\bhow (?:often|quickly)\b.{0,60}\b(?:supplier|category)\b|\bstorage category\b|\bproduct category\b", re.I)
REASON_RX = re.compile(r"\bwhat does .{0,40}require\b|\bcan .{0,40}\band\b|\bwho must\b|\bwhat must happen\b|\bwas exposed\b|\bis on quarantine\b|\bclassified as\b", re.I)
SUM_RX = re.compile(r"\bsummari[sz]e\b|\boverview\b|\bexplain what\b", re.I)
DOMAIN = [("inventory", r"\b(stock|status|quantity|expir|lot-|units in)\b"), ("product", r"\bsku-|storage|shelf life|hazard|reorder"), ("policy", r"\bpolicy|approv|retention|purchase|training"),
          ("sop", r"\bsop\b|procedure|quarantine|recall|deviation|audit|release|hazmat|inspection"), ("general", r".")]


INTERROG_RX = re.compile(r"^(?:what|what's|who|whom|which|how|can|could|does|do|is|are|why|when|where|explain|summari[sz]e|describe|tell|give|show|is there|need the|i'm looking)\b")
INV_LOOKUP_RX = re.compile(r"\b(?:in stock|stock level|stock of|quantity on hand|units (?:are )?in|how many units|current status|status of|where is|how many days until|expir(?:y|es|e)\b|within expiry|still within)", re.I)
GREET_RX = re.compile(r"\b(?:hi|hello|hey|thanks?|thank you|bye|goodbye|good (?:morning|afternoon|evening)|help|what can you do|what do you do|who are you|capabilit|ok)\b", re.I)
DOMAIN_RX = re.compile(r"\b(?:sop|policy|policies|procedure|quarantine|recall|deviation|capa|audit|hazmat|flammable|corrosive|cold[- ]chain|storage|shelf life|reorder|approval|purchase|retention|"
                       r"training|gmp|escalation|incident|certificate|coa|inspection|receiving|lot release|release|supplier|vendor|tier|excursion|cabinet|spill|eyewash|change control|"
                       r"batch|stock|expiry|hold|scrap|safety protocol|triage status|reagent|solvent|product|hazard|cfo|director|approver|regulator)", re.I)


@dataclass
class TriageResult:
    route: str
    intent: str
    domain: str
    risk_tier: int
    complexity: str
    confidence: float
    ms: float


class TriageAgent:
    def __init__(self, train_path=None, model_path=None):
        self.train_path = train_path or settings.data_dir / "triage_labeled.jsonl"
        self.model_path = model_path or settings.cache_dir / "triage.joblib"
        self.pipe = self._load_or_train()
        self._vocab = self._vault_vocab()

    def _load_or_train(self):
        sig = sha256(self.train_path.read_text())[:12]
        if self.model_path.exists():
            blob = joblib.load(self.model_path)
            if blob.get("sig") == sig:
                return blob["pipe"]
        rows = [json.loads(l) for l in self.train_path.read_text().splitlines()]
        tr = [r for r in rows if r["split"] == "train"]
        pipe = Pipeline([("f", FeatureUnion([("w", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, lowercase=True)),
                                             ("c", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True))])),
                         ("m", LogisticRegression(C=20, max_iter=2000, class_weight="balanced"))])
        pipe.fit([self._prep(r["text"]) for r in tr], [r["route"] for r in tr])
        joblib.dump({"sig": sig, "pipe": pipe}, self.model_path)
        return pipe

    @staticmethod
    def _vault_vocab() -> set[str]:
        """Words of vault note titles (product names, SOP names) = the enterprise domain vocabulary."""
        from .rag.chunker import load_vault
        stop = {"the", "and", "of", "for", "a", "in", "to", "on", "sku", "sup", "usp", "acs", "xx"}
        v: set[str] = set()
        for n in load_vault(settings.vault_dir):
            if True:   # tenant-private notes extend the domain vocabulary too (routing to RAG is safe: retrieval enforces tenancy)
                v |= {w for w in re.findall(r"[a-z]{4,}", n.title.lower()) if w not in stop}
        return v

    @staticmethod
    def _prep(t: str) -> str:
        return re.sub(r"LOT-\d{5}", "LOT-N", re.sub(r"SKU-\d{3}", "SKU-N", re.sub(r"SUP-\d{2}", "SUP-N", canon_ids(t))))

    def classify(self, text: str) -> TriageResult:
        t0 = time.perf_counter()
        x = self._prep(text)
        proba = self.pipe.predict_proba([x])[0]
        i = int(proba.argmax())
        route, conf = self.pipe.classes_[i], float(proba[i])
        route = str(route)
        low = text.lower().strip()
        canon = canon_ids(text)
        has_id = bool(re.search(r"SKU-\d{3}|LOT-\d{5}|SUP-\d{2}", canon)) or bool(re.search(r"\b(?:lot|sku|batch)\b", low))
        words = low.split()
        interrog = bool(INTERROG_RX.match(low)) or (low.endswith("?") and not re.match(r"^(?:can|could|would|will) you\b", low))
        in_domain = has_id or bool(DOMAIN_RX.search(low)) or bool(self._vocab & set(re.findall(r"[a-z]+", low)))
        # ---- deterministic guards: rules beat the small model on high-risk verbs and on domain/out-of-domain boundaries ----
        if T3_RX.search(text) and has_id and not interrog:
            route = "TRANSACTION_EXEC"                                   # imperative mutation request
        elif INV_LOOKUP_RX.search(low) and has_id:
            route = "TRANSACTION_EXEC"                                   # live inventory state, not documentation
        elif route == "TRANSACTION_EXEC" and interrog and not (INV_LOOKUP_RX.search(low) and has_id):
            route = "HYBRID_RAG"                                         # a question about a procedure, not a request to act
        elif route in ("DIRECT_ANSWER", "REJECT_OOS") and in_domain and len(words) > 3:
            route = "HYBRID_RAG"                                         # domain vocabulary present => knowledge question
        elif route == "DIRECT_ANSWER" and not GREET_RX.search(low):
            route = "REJECT_OOS"                                         # DIRECT is only for greetings / thanks / capability questions
        elif route == "HYBRID_RAG" and not in_domain and len(words) > 2:
            route = "REJECT_OOS"                                         # nothing in the enterprise domain
        risk = 3 if (route == "TRANSACTION_EXEC" and T3_RX.search(text) and not interrog) else (2 if T2_RX.search(text) else 1)
        complexity = "high" if HOP_RX.search(text) else ("medium" if (REASON_RX.search(text) or SUM_RX.search(text)) else "low")
        domain = next(d for d, rx in DOMAIN if re.search(rx, low))
        intent = {"DIRECT_ANSWER": "chitchat", "HYBRID_RAG": "question", "TRANSACTION_EXEC": "transaction", "REJECT_OOS": "out_of_scope"}[route]
        return TriageResult(route, intent, domain, risk, complexity, conf, (time.perf_counter() - t0) * 1000)
