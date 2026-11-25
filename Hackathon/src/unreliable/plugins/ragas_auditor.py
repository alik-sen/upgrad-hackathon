"""RAGAS auditor (POST_EXECUTION): claim-level grounding check; vetoes ungrounded drafts (faithfulness < min_faithfulness)."""
from __future__ import annotations

from ..verification import verify_answer
from .base import hookimpl


class RagasAuditor:
    @hookimpl
    def post_execute(self, state):
        if state.status != "OK" or not state.answer or not state.evidence:
            return
        if state.meta.get("verified_by_llm"):      # already judged claim-by-claim (lexical + independent LLM entailment) in the orchestrator
            f = state.faithfulness
        else:
            f, bad = verify_answer(state.answer, [e["text"] for e in state.evidence], state.query)
            state.faithfulness, state.unsupported = f, bad
        if f < self.conf.get("min_faithfulness", 0.92):
            state.status = "VETOED"
            state.meta["veto_reason"] = f"faithfulness {f:.2f} < {self.conf.get('min_faithfulness', 0.92)}"
