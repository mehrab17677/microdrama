"""Structured logs (Phase 12): one JSON object per line, and never a secret.

Every record carries ts, level, logger and message, plus any of series_id, job_id, kind, vendor, worker, episode that the caller passed with
`extra={...}`. Credentials never appear: fields named like a header or key are dropped, and the message and exception text are scrubbed of
bearer tokens, API keys, cookies and presigned-URL signatures.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

CONTEXT_FIELDS = ("series_id", "job_id", "kind", "vendor", "worker", "episode", "shot_id", "duration_ms", "status")
SECRET_FIELD = re.compile(r"(authorization|api[-_]?key|xi-api-key|x-api-key|cookie|secret|token|password|signature|credentials|headers?)", re.I)
SCRUBBERS = [
    (re.compile(r"(?i)\b(bearer|key|token|basic)\s+[A-Za-z0-9._~+/=:-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)(authorization|xi-api-key|x-api-key|api[-_]?key|cookie|set-cookie)(\"?\s*[:=]\s*\"?)[^\s\",;}]+"), r"\1\2[redacted]"),
    (re.compile(r"(?i)(X-Amz-(?:Signature|Credential|Security-Token)=)[^&\s\"']+"), r"\1[redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{10,}"), "sk-[redacted]"),
]


def scrub(text: str) -> str:
    for pattern, repl in SCRUBBERS:
        text = pattern.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"), "level": record.levelname,
               "logger": record.name, "message": scrub(record.getMessage())}
        for field in CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None and not SECRET_FIELD.search(field):
                out[field] = str(value) if not isinstance(value, (int, float, bool)) else value
        if record.exc_info:
            out["exception"] = scrub(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False)


def setup(level: int = logging.INFO) -> None:
    """Call once at the start of each process (web, worker, runner, poller)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):   # one format for the web server too
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    for noisy in ("httpx", "httpcore", "urllib3", "botocore"):   # their debug and info lines carry request headers and signed URLs
        logging.getLogger(noisy).setLevel(logging.WARNING)
