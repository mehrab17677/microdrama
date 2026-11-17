"""Map vendor failures to TransientError (retry in place) or PermanentError (fail the attempt)."""
from __future__ import annotations

import httpx

from app.jobs.errors import PermanentError, TransientError

_QUOTA_WORDS = ("insufficient", "credit", "quota", "billing", "payment")


def classify_status(status: int, body: str = "") -> Exception | None:
    """None for success. HTTP 429 and 5xx are transient, except 429s that say the account is out of
    credit (retrying cannot help). Everything else in 4xx is permanent."""
    if status < 400:
        return None
    text = (body or "")[:500]
    if status == 429 and not any(w in text.lower() for w in _QUOTA_WORDS):
        return TransientError(f"HTTP 429: {text}")
    if status >= 500:
        return TransientError(f"HTTP {status}: {text}")
    return PermanentError(f"HTTP {status}: {text}")


def check_response(resp: httpx.Response) -> None:
    err = classify_status(resp.status_code, resp.text if resp.status_code >= 400 else "")
    if err:
        raise err


def map_exception(exc: Exception) -> Exception:
    """For SDK exceptions (openai, anthropic, fal_client, httpx)."""
    if isinstance(exc, (TransientError, PermanentError)):
        return exc
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)):
        return TransientError(f"{type(exc).__name__}: {exc}")
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return classify_status(status, str(exc)) or PermanentError(str(exc))
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name:
        return TransientError(f"{type(exc).__name__}: {exc}")
    return PermanentError(f"{type(exc).__name__}: {exc}")
