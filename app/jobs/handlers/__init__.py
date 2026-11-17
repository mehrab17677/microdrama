"""Handler registry. One handler per job kind (guide 7.3).

A handler is a module or object with either
    run(job) -> dict                      synchronous kinds
or  submit(job) -> str  and  finalize(job) -> dict      asynchronous kinds (video_clip, lipsync)
Raise TransientError to retry in place; PermanentError to fail the attempt.
"""
from __future__ import annotations

from typing import Any

HANDLERS: dict[str, Any] = {}


def register(kind: str, handler: Any) -> None:
    HANDLERS[str(kind)] = handler


def get_handler(kind: str) -> Any:
    try:
        return HANDLERS[kind]
    except KeyError:
        raise LookupError(f"no handler registered for job kind {kind!r}") from None


def is_async(handler: Any) -> bool:
    return hasattr(handler, "submit") and hasattr(handler, "finalize")


BUILTIN_MODULES = ["ingest_source", "llm_story_check", "story_handlers", "look_handlers"]


def load_builtin() -> None:
    """Register every built-in handler module for the kinds it lists in KINDS."""
    import importlib

    for name in BUILTIN_MODULES:
        module = importlib.import_module(f"app.jobs.handlers.{name}")
        table = getattr(module, "HANDLERS", None)  # a module with several kinds lists them by kind
        if table:
            for kind, handler in table.items():
                register(kind, handler)
        else:
            for kind in module.KINDS:
                register(kind, module)
    from app.config import dry_run

    if dry_run():  # stand-ins for kinds without a real handler; never registered in a real run
        from app.jobs.handlers.dryrun_stubs import HANDLERS as STUBS

        for kind, handler in STUBS.items():
            HANDLERS.setdefault(kind, handler)
