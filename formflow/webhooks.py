"""Webhook verification. Scheme (src/modules/webhooks/signing.ts):
header  FormFlow-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>
Several v1 values may be present while a secret is rotated; any one may match."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, Union

from .errors import SignatureError

Payload = Union[bytes, str]


def _raw(payload: Payload) -> bytes:
    return payload if isinstance(payload, bytes) else payload.encode("utf-8")


def sign_payload(secret: str, payload: Payload, timestamp: Optional[int] = None) -> str:
    t = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode("utf-8"), f"{t}.".encode("ascii") + _raw(payload), hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify_signature(secret: str, payload: Payload, header: Optional[str], tolerance: int = 300, now: Optional[float] = None) -> bool:
    """Pass the RAW request body. Re-serialized JSON will not match."""
    if not header:
        return False
    parts = [p.strip().split("=", 1) for p in header.split(",")]
    try:
        t = int(next(v for k, v in parts if k == "t"))
    except (StopIteration, ValueError):
        return False
    if abs((time.time() if now is None else now) - t) > tolerance:
        return False
    expected = sign_payload(secret, payload, t).split("v1=", 1)[1]
    return any(k == "v1" and hmac.compare_digest(v, expected) for k, v in (p for p in parts if len(p) == 2))


def construct_event(secret: str, payload: Payload, header: Optional[str], tolerance: int = 300) -> Dict[str, Any]:
    """Verify, then return the parsed event: {id, type, created, workspace_id, data}. Raises SignatureError."""
    if not verify_signature(secret, payload, header, tolerance):
        raise SignatureError()
    try:
        return json.loads(_raw(payload))
    except ValueError as e:
        raise SignatureError("Webhook body is not JSON") from e
