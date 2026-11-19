"""fal webhook signature verification (guide 6.4).

message = request_id \n user_id \n timestamp \n sha256-hex(raw body), UTF-8
signature = hex, Ed25519, checked against the keys at https://rest.fal.ai/.well-known/jwks.json
(each key's `x` is a base64url public key). Any key verifying is a pass.
"""
from __future__ import annotations

import base64
import hashlib
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

JWKS_URL = "https://rest.fal.ai/.well-known/jwks.json"
JWKS_TTL_SECONDS = 24 * 3600
MAX_SKEW_SECONDS = 300
REQUIRED_HEADERS = ("X-Fal-Webhook-Request-Id", "X-Fal-Webhook-User-Id", "X-Fal-Webhook-Timestamp", "X-Fal-Webhook-Signature")

_cache: tuple[float, list[str]] | None = None


class WebhookRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def fetch_jwks() -> list[str]:
    resp = httpx.get(JWKS_URL, timeout=15)
    resp.raise_for_status()
    return [k["x"] for k in resp.json()["keys"]]


def get_keys(force: bool = False) -> list[str]:
    """Cached for at most 24 hours."""
    global _cache
    now = time.time()
    if force or _cache is None or now - _cache[0] > JWKS_TTL_SECONDS:
        _cache = (now, fetch_jwks())
    return _cache[1]


def clear_cache() -> None:
    global _cache
    _cache = None


def _b64url(x: str) -> bytes:
    return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))


def signed_message(request_id: str, user_id: str, timestamp: str, raw_body: bytes) -> bytes:
    return "\n".join([request_id, user_id, timestamp, hashlib.sha256(raw_body).hexdigest()]).encode("utf-8")


def verify(headers, raw_body: bytes, now: float | None = None) -> None:
    """Raises WebhookRejected unless the delivery is authentic and fresh. `raw_body` must be the
    request bytes before any JSON parsing."""
    values = {h: headers.get(h) for h in REQUIRED_HEADERS}
    missing = [h for h, v in values.items() if not v]
    if missing:
        raise WebhookRejected(f"missing header(s): {', '.join(missing)}")
    try:
        ts = int(values["X-Fal-Webhook-Timestamp"])
    except ValueError:
        raise WebhookRejected("timestamp is not an integer") from None
    if abs((now if now is not None else time.time()) - ts) > MAX_SKEW_SECONDS:
        raise WebhookRejected("timestamp is outside the 300 second window")
    try:
        signature = bytes.fromhex(values["X-Fal-Webhook-Signature"])
    except ValueError:
        raise WebhookRejected("signature is not hex") from None
    message = signed_message(values["X-Fal-Webhook-Request-Id"], values["X-Fal-Webhook-User-Id"], values["X-Fal-Webhook-Timestamp"], raw_body)

    def passes(keys: list[str]) -> bool:
        for x in keys:
            try:
                Ed25519PublicKey.from_public_bytes(_b64url(x)).verify(signature, message)
                return True
            except (InvalidSignature, ValueError):
                continue
        return False

    if passes(get_keys()):
        return
    if passes(get_keys(force=True)):  # keys may have rotated since the cache was filled
        return
    raise WebhookRejected("signature does not verify")
