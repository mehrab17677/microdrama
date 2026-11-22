"""Language models (guide 6.1): one function, complete().

complete(role, system, messages, schema=None, images=None, fixture=None, cache_system=False) -> (parsed_or_text, usage)

The role is looked up in config key llm_roles. With a schema the call uses
.with_structured_output(schema, method="json_schema"): the new Claude models reject a forced
tool_choice, which the default function-calling method uses (see docs/VENDOR_NOTES.md).
A validation failure is retried once with the error appended; then it raises PermanentError.
"""
from __future__ import annotations

import base64
from typing import Any

from pydantic import BaseModel, ValidationError

from app.config import dry_run, get_config, live_llm_roles
from app.jobs.errors import PermanentError
from app.vendors import fixtures
from app.vendors.errors import map_exception


def model_for_role(role: str) -> str:
    roles = get_config("llm_roles")
    try:
        return roles[role]
    except KeyError:
        # A role added after this database was seeded: use the shipped default (seed_config never overwrites an existing row).
        from app.config_defaults import CONFIG_DEFAULTS

        default = CONFIG_DEFAULTS["llm_roles"][0].get(role)
        if default is None:
            raise PermanentError(f"unknown language-model role {role!r}") from None
        return default


def llm_cost(model: str, tokens_in: int, tokens_out: int, price_book: dict | None = None) -> float:
    """USD from the price book (per million tokens). Haiku costs more above its long-prompt threshold."""
    prices = (price_book or get_config("price_book"))["llm"][model]
    rate_in, rate_out = prices["in"], prices["out"]
    if "long_threshold" in prices and tokens_in > prices["long_threshold"]:
        rate_in, rate_out = prices["long_in"], prices["long_out"]
    return tokens_in / 1e6 * rate_in + tokens_out / 1e6 * rate_out


def sniff_image_type(data: bytes) -> str:
    """The real media type from the file's first bytes: the API rejects a label that does not match."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _image_block(image: bytes | str) -> dict:
    if isinstance(image, bytes):
        url = f"data:{sniff_image_type(image)};base64," + base64.b64encode(image).decode()
    else:
        url = image
    return {"type": "image_url", "image_url": {"url": url}}


def max_tokens_for(role: str) -> int:
    """Output limit per role (config llm_max_tokens). ChatAnthropic's own default is 128,000, which is
    far too high for a non-streaming call."""
    try:
        limits = get_config("llm_max_tokens")
    except KeyError:
        return 8000
    return int(limits.get(role, limits.get("default", 8000)))


def _build_messages(system: str, messages: list[dict], images, cache_system: bool = False) -> list[dict]:
    if system and cache_system:
        # Anthropic prompt caching: the long shared prefix (instructions, bible, summary) is read from cache on later calls.
        out: list[dict] = [{"role": "system", "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]}]
    else:
        out = [{"role": "system", "content": system}] if system else []
    out.extend(dict(m) for m in messages)
    if images:
        last = out[-1] if out and out[-1]["role"] == "user" else None
        if last is None:
            last = {"role": "user", "content": ""}
            out.append(last)
        text = last["content"] if isinstance(last["content"], str) else ""
        last["content"] = [{"type": "text", "text": text}, *[_image_block(i) for i in images]]
    return out


def _usage(model: str, raw: Any) -> dict:
    meta = getattr(raw, "usage_metadata", None) or {}
    tin, tout = int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0))
    return {"model": model, "tokens_in": tin, "tokens_out": tout, "cost_usd": llm_cost(model, tin, tout)}


def complete(role: str, system: str, messages: list[dict], schema: type[BaseModel] | None = None, images=None, fixture=None, cache_system: bool = False):
    """`fixture` (an object or a zero-argument callable) is what DRY_RUN returns instead of calling a model,
    so the caller's real validation and persistence code still runs. Without one, a minimal valid object is synthesised."""
    model = model_for_role(role)
    if dry_run() and role not in live_llm_roles():
        if fixture is not None:
            value = fixture() if callable(fixture) else fixture
            if schema is not None and not isinstance(value, schema):
                value = schema.model_validate(value)
        else:
            value = fixtures.synthesize(schema) if schema else "fixture text"
        return value, {"model": model, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}

    from langchain.chat_models import init_chat_model

    chat = init_chat_model(model, max_tokens=max_tokens_for(role))
    msgs = _build_messages(system, messages, images, cache_system)
    total_in = total_out = 0
    try:
        if schema is None:
            raw = chat.invoke(msgs)
            usage = _usage(model, raw)
            return raw.content if isinstance(raw.content, str) else _text(raw.content), usage

        runnable = chat.with_structured_output(schema, method="json_schema", include_raw=True)
        for attempt in (1, 2):
            result = runnable.invoke(msgs)
            usage = _usage(model, result["raw"])
            total_in += usage["tokens_in"]
            total_out += usage["tokens_out"]
            parsed, error = result.get("parsed"), result.get("parsing_error")
            if parsed is not None and error is None:
                if not isinstance(parsed, schema):
                    parsed = schema.model_validate(parsed)
                return parsed, {"model": model, "tokens_in": total_in, "tokens_out": total_out, "cost_usd": llm_cost(model, total_in, total_out)}
            if attempt == 2:
                raise PermanentError(f"{model} did not return valid {schema.__name__} twice: {error}")
            msgs = [*msgs, {"role": "user", "content": f"Your last answer failed validation: {error}\nReturn the corrected {schema.__name__}."}]
    except (PermanentError, ValidationError):
        raise
    except Exception as exc:  # noqa: BLE001 - SDK errors
        raise map_exception(exc) from exc
    raise AssertionError("unreachable")


def _text(blocks: list) -> str:
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in blocks)
