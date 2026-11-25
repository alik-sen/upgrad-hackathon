"""Prompt-injection shield (PRE_EXECUTION priority 20, fail-closed) + indirect-injection chunk scanner.

Additive weighted rule score on a normalised copy of the text; block at >= BLOCK. A decayed per-session risk term catches
multi-turn crescendo attacks. Also screens tool-argument overflow/SQLi fuzzing.
"""
from __future__ import annotations

import re

from .base import hookimpl
from .pii import fold

BLOCK = 0.9
V_OVERRIDE = r"(?:ignore|disregard|forget|override|bypass|skip|circumvent|disable|drop|neglect|turn off|switch off)"
T_RULES = r"(?:instruction|rule|prompt|polic(?:y|ies)|guardrail|restriction|safeguard|safety|filter|approval|approver|guideline|constraint|tier ?3|hitl)"
V_EXFIL = r"(?:reveal|print|show|repeat|output|dump|leak|display|tell|write|give|list|expose|echo|disclose|recite|share|send|email)"
T_SECRET = r"(?:system|developer|hidden|initial|original|confidential|secret|internal|underlying)"
T_PROMPT = r"(?:prompt|message|instruction|config|configuration|rules|guideline|api ?key|credential|password|token|tenant data|data of all)"
RULES: list[tuple[float, re.Pattern, str]] = [
    (1.0, re.compile(rf"\b(?:ignore|disregard|forget)\b.{{0,40}}\b(?:previous|prior|above|earlier|preceding|all|your|any|the)\b.{{0,30}}\b{T_RULES}", re.I | re.S), "ignore_instructions"),
    (0.9, re.compile(rf"\b(?:override|bypass|skip|circumvent|disable)\b.{{0,30}}\b(?:the |your |all |any )?{T_RULES}|\b(?:ignore|disregard|neglect|drop)\b.{{0,15}}\b(?:the |your |all |any )?{T_RULES}", re.I | re.S), "override_rules"),
    (1.0, re.compile(rf"\b{V_EXFIL}\b.{{0,70}}\b{T_SECRET}\b.{{0,20}}\b{T_PROMPT}", re.I | re.S), "exfil_system_prompt"),
    (0.8, re.compile(rf"\b{V_EXFIL}\b.{{0,25}}\b(?:exact|verbatim|full|complete|entire)\b.{{0,15}}\b(?:instruction|prompt|rule|wording)s?", re.I | re.S), "exfil_verbatim"),
    (0.8, re.compile(r"\b(?:repeat|print|output)\b.{0,15}\beverything\b.{0,15}\b(?:above|before)\b", re.I), "repeat_above"),
    (0.9, re.compile(rf"\b{V_EXFIL}\b.{{0,20}}\byour\b.{{0,15}}\b(?:prompt|instructions|system|rules|guidelines|configuration)\b", re.I | re.S), "exfil_your_prompt"),
    (0.9, re.compile(r"\b(?:instructions|prompt|rules|guidelines)\b.{0,12}\byou (?:were|have been|are|had been)\b.{0,12}\b(?:given|using|following|told)\b|\bsystem prompt\b", re.I | re.S), "probe_instructions"),
    (0.7, re.compile(r"\b(?:grandma|grandmother|grandpa|deceased|bedtime story)\b.{0,80}\b(?:prompt|instructions|rules|password|key)\b", re.I | re.S), "emotional_roleplay"),
    (0.8, re.compile(rf"\b(?:you are now|from now on|act as|pretend (?:you|to)|roleplay as|play the role|let'?s play a game where you)\b.{{0,60}}\b(?:unrestricted|unfiltered|no restrictions|without (?:any )?(?:rules|restrictions|filters|limits)|dan\b|developer mode|jailbr|no (?:content )?polic|ignore)", re.I | re.S), "persona_jailbreak"),
    (0.7, re.compile(r"\b(?:you are|you're) (?:now )?in (?:developer|debug|god|admin) mode\b|\bdan mode\b|\bjailbr(?:eak|oken)\b|\bdo anything now\b", re.I), "mode_switch"),
    (0.5, re.compile(r"\bDAN\b"), "dan_persona"),
    (0.4, re.compile(r"\bno restrictions\b|\bno limits\b|\bwithout (?:any )?(?:filters?|censorship)\b", re.I), "no_restrictions"),
    (0.6, re.compile(r"\bno (?:content )?polic(?:y|ies)\b|\bwithout (?:any )?(?:rules|restrictions|safety)\b|\bunrestricted (?:ai|mode|assistant)\b|\bdrop your restrictions\b", re.I), "no_policy"),
    (1.0, re.compile(r"</?\s*(?:system|assistant|instructions?)\s*>|<<\s*sys\s*>>|\[\[?\s*system(?: override)?\s*\]?\]|#{2,3}\s*(?:system|new instructions?)|\bsystem\s*override\b|\bsystem:\s", re.I), "delimiter_injection"),
    (1.0, re.compile(r"<!--.{0,300}(?:assistant|ignore|disregard|instruction|call |reveal).{0,300}-->", re.I | re.S), "hidden_comment"),
    (1.0, re.compile(r"\b(?:important|urgent|new)\s+(?:new\s+)?instructions?\s*:", re.I), "new_instructions"),
    (0.6, re.compile(rf"\bbase64\b.{{0,40}}\b(?:instruction|prompt|rules)", re.I), "encoded_exfil"),
    (0.8, re.compile(r"[A-Za-z0-9+/]{80,}={0,2}"), "long_blob"),
    (0.4, re.compile(r"\b(?:as the (?:system )?admin(?:istrator)?|i (?:hereby )?(?:authorize|order|command) you|you must obey|i am the (?:owner|developer|admin))\b", re.I), "authority_claim"),
    (1.0, re.compile(r"\b(?:release|update|mark|put|set|change|approve)\b.{0,50}\b(?:without|skip|bypass|override|ignoring|ignore)\b.{0,25}\b(?:approv|authoriz|review|hitl|qa|approver)", re.I | re.S), "tier3_bypass"),
    (0.6, re.compile(r"\boverride\b.{0,30}\b(?:approver|approval|tier ?3|hitl)", re.I | re.S), "tier3_override"),
    (1.0, re.compile(r"\b(?:other|all|every)\s+tenants?'?s?\b.{0,20}\b(?:data|secrets?|records|information)|\b(?:leak|dump|steal)\b.{0,20}\b(?:api ?keys?|credentials|passwords?|secrets?)", re.I | re.S), "cross_tenant_exfil"),
    (0.5, re.compile(rf"\b(?:which|what) tools? (?:do you|are you) use|\byour (?:internal|hidden|underlying) (?:tools?|instructions?|rules|prompt)", re.I), "probe_internals"),
    (0.3, re.compile(r"\b(?:hypothetically|if there were no|what if (?:i'?m|there is) no|just this once|be (?:a little )?less strict)\b", re.I), "social_engineering"),
    (1.0, re.compile(r"(?:'|\")\s*;?\s*(?:drop|delete|update|insert|alter|union|select|or|and)\b|;\s*(?:drop|delete|update|insert|alter)\s|\bunion\s+select\b|--\s*$|\bor\s+'?1'?\s*=\s*'?1", re.I), "sqli"),
    (1.0, re.compile(r"\$\{\s*jndi:|\{\{.{0,20}\}\}|\.\./\.\./|\brm\s+-rf\b|[\x00-\x08\x0e-\x1f]|\r\n[A-Za-z\-]+:\s", re.I), "template_cmd_injection"),
    (0.5, re.compile(r"\b(?:select|delete)\b.{0,30}\bfrom\b.{0,40}\bwhere\b|\bfrom\s+(?:audit_chain|hitl|lots|skus)\b", re.I), "sql_like"),
]
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
MAX_ARG_FUZZ = 2500  # characters of a single repeated token run considered overflow


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", fold(text))


def score(text: str) -> tuple[float, list[str]]:
    t = norm(text)
    hits, total = [], 0.0
    variants = [t]
    lt = t.translate(LEET)
    if lt != t:
        variants.append(lt)
    for w, rx, name in RULES:
        if any(rx.search(v) for v in variants):
            hits.append(name); total += w
    # header / CRLF injection must be checked on the RAW text (norm() collapses whitespace)
    if re.search(r"[\r\n]\s*(?:X-[A-Za-z\-]+|Authorization|Cookie|Host|Content-[A-Za-z\-]+)\s*:", text, re.I):
        hits.append("crlf_header_injection"); total += 1.0
    # overflow / repeated-token fuzz
    if len(text) > MAX_ARG_FUZZ or re.search(r"(\S{1,12})(?:\s*\1){60,}", text):
        hits.append("arg_overflow"); total += 1.0
    return total, hits


class InjectionShield:
    def __init__(self):
        self.session_risk: dict[str, float] = {}

    @hookimpl
    def pre_execute(self, state, session_risk):
        s, hits = score(state.query)
        state.meta["injection_hits"] = hits
        eff = s + 0.5 * session_risk
        state.risk_score = eff
        if eff >= BLOCK:
            state.blocked, state.status, state.block_reason = True, "BLOCKED_INJECTION", ",".join(hits) or "session_risk"

    @hookimpl
    def chunks_retrieved(self, state, chunks):
        keep = []
        for c in chunks:
            s, hits = score(c["text"] if isinstance(c, dict) else c.text)
            if s >= 0.6:
                state.quarantined_chunks.append({"chunk_id": c["chunk_id"] if isinstance(c, dict) else c.chunk_id, "hits": hits})
            else:
                keep.append(c)
        return keep
