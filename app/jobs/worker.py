"""Worker process: claims jobs and runs their handlers (guide 7.3)."""
from __future__ import annotations

import logging
import os
import socket
import threading
import time

from app.config import get_settings
from app.jobs import ledger, queue
from app.jobs.errors import PermanentError, TransientError
from app.jobs.handlers import get_handler, is_async

log = logging.getLogger("worker")
BACKOFF_SECONDS = (2, 4, 8, 16, 32)  # retry in place, at most 5 times, attempt unchanged
_sleep = time.sleep  # replaced in tests


def _call_with_retry(fn, job: dict):
    for i in range(len(BACKOFF_SECONDS) + 1):
        try:
            return fn(job)
        except TransientError as exc:
            if i == len(BACKOFF_SECONDS):
                raise PermanentError(f"gave up after {len(BACKOFF_SECONDS)} retries: {exc}") from exc
            log.warning("job %s transient error (%s); retry %d in %ds", job["id"], exc, i + 1, BACKOFF_SECONDS[i])
            _sleep(BACKOFF_SECONDS[i])


def process(job: dict) -> None:
    """Run one claimed job to its next state."""
    handler = None
    try:
        handler = get_handler(job["kind"])
        if job["phase"] == "finalize":
            ledger.succeed(job["id"], _call_with_retry(handler.finalize, job))
        elif is_async(handler):
            ledger.mark_submitted(job["id"], _call_with_retry(handler.submit, job))
        else:
            ledger.succeed(job["id"], _call_with_retry(handler.run, job))
    except PermanentError as exc:
        _failed(job, handler, str(exc))
    except Exception as exc:  # noqa: BLE001 - a bug in a handler must not leave the job stuck
        log.exception("job %s crashed", job["id"])
        _failed(job, handler, f"{type(exc).__name__}: {exc}")


def _failed(job: dict, handler, message: str) -> None:
    """Fail the job, then let the handler update its own rows (optional on_failure(job, error))."""
    if ledger.fail(job["id"], message) and handler is not None and hasattr(handler, "on_failure"):
        try:
            handler.on_failure(job, message)
        except Exception:  # noqa: BLE001
            log.exception("on_failure hook failed for job %s", job["id"])


def run_one(worker_id: str) -> bool:
    """Claim and process one job. Returns False when nothing was claimable."""
    job = queue.claim(worker_id)
    if job is None:
        return False
    process(job)
    return True


def _loop(worker_id: str) -> None:
    while True:
        try:
            if not run_one(worker_id):
                time.sleep(1.0)
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
            time.sleep(2.0)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from app.jobs.handlers import load_builtin

    load_builtin()
    n = get_settings().worker_concurrency
    base = f"{socket.gethostname()}:{os.getpid()}"
    log.info("worker %s starting %d thread(s)", base, n)
    threads = [threading.Thread(target=_loop, args=(f"{base}:{i}",), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
