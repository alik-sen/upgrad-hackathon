"""Observability: W3C TraceContext spans -> OpenTelemetry SDK (+JSONL exporter), in-process latency store, counters.

Span names follow design.md §1.4: hook.pre, cache.lookup, triage.classify, mcp.rag.bm25/.biencoder/.rrf/.rerank,
agent.react.step, llm.generate, hook.post.ragas (+ http.ingress, mcp.call, audit.append).
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult

from .config import settings
from .util import percentile

_trace: ContextVar[dict | None] = ContextVar("ua_trace", default=None)


class JsonlSpanExporter(SpanExporter):
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def export(self, spans):
        with self._lock, open(self.path, "a") as f:
            for s in spans:
                f.write(json.dumps({"name": s.name, "trace_id": f"{s.context.trace_id:032x}", "span_id": f"{s.context.span_id:016x}",
                                    "start": s.start_time, "end": s.end_time, "dur_ms": (s.end_time - s.start_time) / 1e6,
                                    "attrs": {k: v for k, v in (s.attributes or {}).items()}}) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


class InMemorySpanExporter(SpanExporter):
    def __init__(self, maxlen=5000):
        self.spans: deque = deque(maxlen=maxlen)

    def export(self, spans):
        for s in spans:
            self.spans.append({"name": s.name, "trace_id": f"{s.context.trace_id:032x}", "dur_ms": (s.end_time - s.start_time) / 1e6,
                               "start": s.start_time, "attrs": dict(s.attributes or {})})
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


class Telemetry:
    def __init__(self):
        self.lat: dict[str, deque] = defaultdict(lambda: deque(maxlen=100_000))
        self.counters: dict[str, float] = defaultdict(float)
        self.mem = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(BatchSpanProcessor(self.mem, max_queue_size=8192, schedule_delay_millis=1000))
        self.jsonl_enabled = False
        if os.environ.get("OTEL_JSONL", "") == "1":
            self.enable_jsonl(str(settings.reports_dir / "spans.jsonl"))
        self.tracer = self.provider.get_tracer("unreliable")
        self.otel_enabled = True  # toggled by the otel_exporter plugin

    def enable_jsonl(self, path: str):
        self.provider.add_span_processor(BatchSpanProcessor(JsonlSpanExporter(path), max_queue_size=8192, schedule_delay_millis=1000))
        self.jsonl_enabled = True

    # ---- trace context ----
    def start_trace(self, traceparent: str | None = None) -> dict:
        tid = None
        if traceparent:
            parts = traceparent.split("-")
            if len(parts) == 4 and len(parts[1]) == 32:
                tid = parts[1]
        tid = tid or secrets.token_hex(16)
        t = {"trace_id": tid, "spans": [], "traceparent": f"00-{tid}-{secrets.token_hex(8)}-01"}
        _trace.set(t)
        return t

    def current(self) -> dict | None:
        return _trace.get()

    @contextmanager
    def span(self, name: str, **attrs: Any):
        t0 = time.perf_counter()
        cm = self.tracer.start_as_current_span(name, attributes={k: v for k, v in attrs.items() if isinstance(v, (str, int, float, bool))}) if self.otel_enabled else None
        sp = cm.__enter__() if cm else None
        try:
            yield
        finally:
            ms = (time.perf_counter() - t0) * 1000
            self.lat[name].append(ms)
            tr = _trace.get()
            if tr is not None:
                tr["spans"].append({"name": name, "ms": round(ms, 3), "offset_ms": round((t0 - tr.get("t0", t0)) * 1000, 3), **attrs})
            if cm:
                cm.__exit__(None, None, None)

    def record(self, name: str, ms: float):
        self.lat[name].append(ms)

    def inc(self, name: str, v: float = 1.0):
        self.counters[name] += v

    def stats(self, name: str) -> dict:
        v = list(self.lat.get(name, []))
        return {"n": len(v), "p50": percentile(v, 50), "p95": percentile(v, 95), "p99": percentile(v, 99), "mean": (sum(v) / len(v)) if v else 0.0}

    def all_stats(self) -> dict:
        return {k: self.stats(k) for k in list(self.lat)}

    def prometheus(self) -> str:
        lines = []
        for k, v in list(self.counters.items()):
            lines.append(f'ua_counter{{name="{k}"}} {v}')
        for k, s in self.all_stats().items():
            for q in ("p50", "p95", "p99"):
                lines.append(f'ua_latency_ms{{span="{k}",q="{q}"}} {s[q]:.3f}')
        return "\n".join(lines) + "\n"

    def reset(self):
        self.lat.clear(); self.counters.clear()


tele = Telemetry()
