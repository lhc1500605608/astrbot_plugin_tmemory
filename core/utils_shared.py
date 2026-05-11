"""Shared stateless utilities used across core modules.

Moved here from admin_service.py and web_server.py to eliminate
circular imports between admin_service ↔ mixins and web_server ↔ web_handlers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Optional

_VALID_MEMORY_TYPES = frozenset({"preference", "fact", "task", "restriction", "style"})
_VALID_FACET_TYPES = frozenset({"preference", "fact", "style", "restriction", "task_pattern"})
_VALID_ITEM_STATUSES = frozenset({"active", "superseded", "contradicted", "archived"})


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _safe_memory_type(value: object) -> str:
    s = str(value or "fact").strip().lower()
    return s if s in _VALID_MEMORY_TYPES else "fact"


def _clamp01(value: object) -> float:
    try:
        num = float(value)  # type: ignore[arg-type]
    except Exception:
        num = 0.0
    return max(0.0, min(1.0, num))


# ── JWT utilities (moved from web_server.py) ──────────────────────────────


def _b64url_encode(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    import base64

    s += "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)


def jwt_encode(payload: dict, secret: str, exp_seconds: int = 86400) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        **payload,
        "exp": int(time.time()) + exp_seconds,
        "iat": int(time.time()),
    }
    h = _b64url_encode(json.dumps(header).encode())
    p = _b64url_encode(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url_encode(sig)}"


def jwt_decode(token: str, secret: str) -> Optional[dict]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        h, p, s = parts
        expected_sig = hmac.new(
            secret.encode(), f"{h}.{p}".encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64url_decode(s), expected_sig):
            return None
        payload = json.loads(_b64url_decode(p))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None
