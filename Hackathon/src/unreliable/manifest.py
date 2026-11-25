"""Signed MCP server manifests (HMAC-SHA256, G-13): host refuses servers whose tool list/signature does not match."""
from __future__ import annotations

import hashlib
import hmac

from .config import settings
from .util import canonical_json


def sign_manifest(name: str, tools: list[str], key: str | None = None) -> str:
    body = canonical_json({"name": name, "tools": sorted(tools)})
    return hmac.new((key or settings.manifest_key).encode(), body.encode(), hashlib.sha256).hexdigest()


def build_manifest(name: str, tools: list[str]) -> dict:
    return {"name": name, "tools": sorted(tools), "sig": sign_manifest(name, tools)}


def verify_manifest(m: dict, expected_name: str, expected_tools: list[str]) -> bool:
    if m.get("name") != expected_name:
        return False
    if sorted(m.get("tools", [])) != sorted(expected_tools):
        return False
    return hmac.compare_digest(m.get("sig", ""), sign_manifest(expected_name, expected_tools))
