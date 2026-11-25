"""OTel exporter plugin (ALL_SPANS): toggles span export; spans are always timed locally for KPIs."""
from __future__ import annotations

from ..telemetry import tele


class OTelExporter:
    def on_toggle(self, on: bool):
        tele.otel_enabled = on
