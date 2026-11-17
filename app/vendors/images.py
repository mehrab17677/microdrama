"""Images and embeddings (guide 6.2), OpenAI.

Set IMAGE_BACKEND=local_sd to generate images with an open-source model instead (tests only; see
app/vendors/local_images.py).

Generate:  client.images.generate(model='gpt-image-2.5-flare', prompt, size, quality)
Reference: POST /v1/images/edits with `images`, a list of 1 to 16 objects holding image_url.
Size: OpenAI needs both sides divisible by 16, so 1080x1920 is invalid. config image_size_mode:
  scale_1152x2048   request 1152x2048 (exact 9:16) and Lanczos-scale to 1080x1920   [default]
  native_1080x1920  request 1080x1920 as the guide first assumed
  crop_1024x1536    request 1024x1536, centre-crop to 864x1536, scale to 1080x1920
"""
from __future__ import annotations

import base64
import io
import os

import httpx
from PIL import Image

from app.config import dry_run, get_config
from app.jobs.errors import PermanentError
from app.vendors import fixtures
from app.vendors.errors import check_response, map_exception

IMAGE_MODEL = "gpt-image-2.5-flare"  # always sent explicitly: the edits endpoint defaults to another model
EMBEDDING_MODEL = "text-embedding-3-small"
QUALITIES = ("auto", "low", "medium", "high", "xhigh", "max")
MAX_REFERENCES = 16
TARGET = (1080, 1920)
_REQUEST_SIZE = {"scale_1152x2048": "1152x2048", "native_1080x1920": "1080x1920", "crop_1024x1536": "1024x1536"}


def request_size(mode: str | None = None) -> str:
    mode = mode or get_config("image_size_mode")
    try:
        return _REQUEST_SIZE[mode]
    except KeyError:
        raise PermanentError(f"unknown image_size_mode {mode!r}") from None


def to_target_size(png: bytes, mode: str | None = None) -> bytes:
    """Convert what the API returned into a 1080x1920 PNG according to the size mode."""
    mode = mode or get_config("image_size_mode")
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if mode == "crop_1024x1536":
        w, h = img.size
        crop_w = round(h * 9 / 16)
        left = (w - crop_w) // 2
        img = img.crop((left, 0, left + crop_w, h))
    if img.size != TARGET:
        img = img.resize(TARGET, Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def image_cost(usage: dict, price_book: dict | None = None) -> float:
    rates = (price_book or get_config("price_book"))["image_per_m"]
    details = usage.get("input_tokens_details") or {}
    return (
        details.get("text_tokens", 0) / 1e6 * rates["text_in"]
        + details.get("image_tokens", 0) / 1e6 * rates["image_in"]
        + usage.get("output_tokens", 0) / 1e6 * rates["image_out"]
    )


def _ref(image: bytes | str) -> dict:
    url = "data:image/png;base64," + base64.b64encode(image).decode() if isinstance(image, bytes) else image
    return {"image_url": url}


def generate_image(prompt: str, references: list[bytes | str] | None = None, quality: str | None = None) -> tuple[bytes, dict]:
    """Returns (1080x1920 PNG bytes, usage). `references` are bytes or URLs (presigned R2 URLs)."""
    quality = quality or get_config("image_quality")
    if quality not in QUALITIES:
        raise PermanentError(f"unknown image quality {quality!r}")
    refs = references or []
    if len(refs) > MAX_REFERENCES:
        raise PermanentError(f"at most {MAX_REFERENCES} reference images, got {len(refs)}")
    mode = get_config("image_size_mode")
    size = request_size(mode)

    if os.environ.get("IMAGE_BACKEND") == "local_sd":
        # Opt-in open-source generator for tests; free, so it also runs under DRY_RUN=1 (see local_images.py).
        from app.vendors import local_images

        return local_images.generate(prompt, refs)
    if dry_run():
        return fixtures.fixture_bytes("image_1080x1920.png"), {"model": IMAGE_MODEL, "cost_usd": 0.0, "size": size}

    try:
        if refs:
            resp = httpx.post(
                "https://api.openai.com/v1/images/edits",
                headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"},
                json={"model": IMAGE_MODEL, "prompt": prompt, "size": size, "quality": quality, "images": [_ref(r) for r in refs]},
                timeout=300,
            )
            check_response(resp)
            body = resp.json()
            b64, usage = body["data"][0]["b64_json"], body.get("usage") or {}
        else:
            from openai import OpenAI

            result = OpenAI().images.generate(model=IMAGE_MODEL, prompt=prompt, size=size, quality=quality)
            b64 = result.data[0].b64_json
            usage = result.usage.model_dump() if getattr(result, "usage", None) else {}
    except Exception as exc:  # noqa: BLE001
        raise map_exception(exc) from exc
    png = to_target_size(base64.b64decode(b64), mode)
    return png, {"model": IMAGE_MODEL, "cost_usd": image_cost(usage), "size": size, "usage": usage}


def embed(texts: list[str]) -> tuple[list[list[float]], dict]:
    """1536-dimension embeddings (text-embedding-3-small). Callers batch in groups of 100."""
    if not texts:
        return [], {"model": EMBEDDING_MODEL, "tokens": 0, "cost_usd": 0.0}
    if os.environ.get("EMBEDDING_BACKEND") == "local":  # opt-in open-source backend for tests; free, so also under DRY_RUN=1
        from app.vendors import local_embeddings

        return local_embeddings.embed(texts), {"model": f"local:{local_embeddings.model_id()}", "tokens": 0, "cost_usd": 0.0}
    if dry_run():
        return [fixtures.fake_embedding(t) for t in texts], {"model": EMBEDDING_MODEL, "tokens": 0, "cost_usd": 0.0}
    try:
        from openai import OpenAI

        result = OpenAI().embeddings.create(model=EMBEDDING_MODEL, input=texts)
    except Exception as exc:  # noqa: BLE001
        raise map_exception(exc) from exc
    tokens = result.usage.total_tokens
    cost = tokens / 1e6 * get_config("price_book")["embedding_per_m"][EMBEDDING_MODEL]
    return [d.embedding for d in result.data], {"model": EMBEDDING_MODEL, "tokens": tokens, "cost_usd": cost}
