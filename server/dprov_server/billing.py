"""Stripe billing webhook — close the loop between subscriptions and plan/quota.

The server already meters usage and enforces per-plan quotas; ``Tenancy.set_plan`` is the
seam. This wires Stripe's webhook to it: verify the signature, read the project from the
event metadata, map the subscription to a plan, and call ``set_plan``.

Signature verification is the real Stripe scheme (HMAC-SHA256 over ``"{t}.{payload}"`` keyed
by the endpoint signing secret, with a timestamp tolerance) — implemented with the standard
library, so the whole server side is testable without a Stripe account or the Stripe SDK.
What's left to *you* is the Stripe dashboard config (the endpoint URL + signing secret, and
attaching ``metadata.project_id`` at checkout).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Dict, Optional, Tuple


def sign(payload: bytes, secret: str, timestamp: int) -> str:
    """Produce a ``Stripe-Signature`` header value for ``payload`` (used by clients/tests)."""
    signed = str(timestamp).encode("utf-8") + b"." + payload
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(payload: bytes, sig_header: str, secret: str,
                     tolerance: int = 300, now: Optional[float] = None) -> bool:
    """Verify a ``Stripe-Signature`` header against the raw body. Constant-time; rejects a
    missing/invalid signature, a tampered body, a wrong secret, or a stale timestamp."""
    if not sig_header or not secret:
        return False
    parts: Dict[str, list] = {}
    for item in sig_header.split(","):
        key, _, value = item.partition("=")
        parts.setdefault(key.strip(), []).append(value.strip())
    timestamps = parts.get("t", [])
    signatures = parts.get("v1", [])
    if not timestamps or not signatures:
        return False
    try:
        ts = int(timestamps[0])
    except ValueError:
        return False
    current = time.time() if now is None else now
    if tolerance and abs(current - ts) > tolerance:
        return False
    signed = str(ts).encode("utf-8") + b"." + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)


def parse_price_plans(raw: Optional[str]) -> Dict[str, str]:
    """``"price_abc:pro,price_def:free"`` → ``{"price_abc": "pro", "price_def": "free"}``."""
    out: Dict[str, str] = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, value = pair.partition(":")
        if key.strip() and value.strip():
            out[key.strip()] = value.strip()
    return out


def plan_for_event(event: dict, price_plans: Optional[Dict[str, str]] = None) -> Optional[Tuple[str, str]]:
    """Map a Stripe event to ``(project_id, plan)``, or ``None`` if it isn't actionable.

    The project is read from ``data.object.metadata.project_id`` (attach it at checkout). A
    cancelled subscription → ``free``; otherwise the price's ``lookup_key``/``id`` is looked
    up in ``price_plans`` (defaulting to ``pro`` for an active subscription).
    """
    price_plans = price_plans or {}
    obj = (event.get("data") or {}).get("object") or {}
    project_id = (obj.get("metadata") or {}).get("project_id")
    if not project_id:
        return None
    event_type = event.get("type", "")
    if event_type == "customer.subscription.deleted":
        return (project_id, "free")
    price_key = None
    items = (obj.get("items") or {}).get("data") or []
    if items:
        price = items[0].get("price") or {}
        price_key = price.get("lookup_key") or price.get("id")
    plan = price_plans.get(price_key, "pro") if price_key else "pro"
    return (project_id, plan)
