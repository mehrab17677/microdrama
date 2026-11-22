"""Images and embeddings (guide 6.2). Default provider: fal (one account for images, embeddings and video); OpenAI direct is kept as an option.

Config image_provider (fal | openai) and embedding_provider (fal | openai) choose where the calls go; config image_models picks the model
per purpose (master, pack, plate, keyframe, brand_keyframe; "default" otherwise):
  gpt_image_flare   fal openai/gpt-image-2.5/flare/edit and /text-to-image (16 references; exact sizes, multiples of 16)   [default]
  nano_banana_pro   fal fal-ai/nano-banana-pro/edit and fal-ai/nano-banana-pro (14 references; aspect_ratio 9:16, 1K/2K/4K)
                    [default for brand_keyframe, and the choice offered at the Look gate]. It exists only on fal, so it always goes through fal.
Embeddings on fal: openrouter/router/openai/v1/embeddings (OpenAI wire format) with openai/text-embedding-3-small: the same 1536-dimension vectors
as OpenAI direct, so nothing already stored changes.
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

IMAGE_MODEL = "gpt-image-2.5-flare"  # OpenAI direct: always sent explicitly, the edits endpoint defaults to another model
EMBEDDING_MODEL = "text-embedding-3-small"
FAL_EMBEDDING_ENDPOINT = "openrouter/router/openai/v1/embeddings"
FAL_EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536          # the vector columns; a model that returns another size is refused, not stored
FAL_EMBED_BATCH = 64
IMAGE_MODELS = {
    "gpt_image_flare": {"label": "GPT Image 2.5 Flare", "fal_edit": "openai/gpt-image-2.5/flare/edit", "fal_text": "openai/gpt-image-2.5/flare/text-to-image", "max_refs": 16},
    "nano_banana_pro": {"label": "Nano Banana Pro", "fal_edit": "fal-ai/nano-banana-pro/edit", "fal_text": "fal-ai/nano-banana-pro", "max_refs": 14},
}
PURPOSES = ("master", "pack", "plate", "keyframe", "brand_keyframe")
REF_LONG_EDGE = 1600                 # references sent to fal are scaled to this long edge and sent as JPEG data URIs
QUALITIES = ("auto", "low", "medium", "high", "xhigh", "max")
MAX_REFERENCES = 16
TARGET = (1080, 1920)
_REQUEST_SIZE = {"scale_1152x2048": "1152x2048", "native_1080x1920": "1080x1920", "crop_1024x1536": "1024x1536"}


PACK_KINDS = ("turnaround", "expression", "wardrobe", "pose", "lineup")


def unsupported_pack_kinds() -> frozenset[str]:
    """Character-sheet kinds the active image backend cannot make well. The master (prime) portrait is
    always made. Real OpenAI images support everything. SKIP_PACK_KINDS (comma list, or `none`) overrides."""
    override = os.environ.get("SKIP_PACK_KINDS")
    if override is not None:
        names = {n.strip() for n in override.split(",") if n.strip() and n.strip() != "none"}
        bad = names - set(PACK_KINDS)
        if bad:
            raise PermanentError(f"SKIP_PACK_KINDS names unknown sheet kinds: {sorted(bad)}")
        return frozenset(names)
    if os.environ.get("IMAGE_BACKEND") == "local_sd":
        from app.vendors import local_images

        return local_images.UNSUPPORTED_PACK_KINDS
    return frozenset()


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


def model_for(purpose: str = "default", override: str | None = None) -> str:
    """The model key for an image purpose: an explicit choice (the Look gate's Nano Banana Pro button) wins, then config image_models."""
    key = override or (get_config("image_models").get(purpose) or get_config("image_models").get("default") or "gpt_image_flare")
    if key not in IMAGE_MODELS:
        raise PermanentError(f"unknown image model {key!r}; use one of {', '.join(IMAGE_MODELS)}")
    return key


def provider_for(model_key: str) -> str:
    """Nano Banana Pro exists only on fal; the GPT model follows config image_provider."""
    return "fal" if model_key == "nano_banana_pro" else get_config("image_provider")


def job_vendor() -> str:
    """The vendor an image job is filed under (concurrency limits and cost rows): fal unless images are set to go to OpenAI direct."""
    return "fal" if get_config("image_provider") == "fal" else "openai"


def fit_to_target(png: bytes) -> bytes:
    """Any returned size to exactly 1080x1920: centre-crop to 9:16 when the shape is off by more than about half a percent, then Lanczos-scale."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = img.size
    if abs(w / h - 9 / 16) > 0.005:
        if w / h > 9 / 16:
            crop_w = round(h * 9 / 16)
            left = (w - crop_w) // 2
            img = img.crop((left, 0, left + crop_w, h))
        else:
            crop_h = round(w * 16 / 9)
            top = (h - crop_h) // 2
            img = img.crop((0, top, w, top + crop_h))
    if img.size != TARGET:
        img = img.resize(TARGET, Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _fal_ref(image: bytes | str) -> str:
    """A reference for fal: URLs pass through; bytes are scaled to REF_LONG_EDGE and sent as a JPEG data URI (large PNG data URIs slow the request down)."""
    if isinstance(image, str):
        return image
    img = Image.open(io.BytesIO(image)).convert("RGB")
    if max(img.size) > REF_LONG_EDGE:
        img.thumbnail((REF_LONG_EDGE, REF_LONG_EDGE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def fal_image_cost(model_key: str, *, edit: bool, quality: str = "medium", resolution: str = "2K", prices: dict | None = None) -> float:
    """USD per image from config image_prices (fal's published prices; fal returns no cost). GPT Image prices depend on quality and on edit or text-only;
    a quality fal does not list (xhigh, max, auto) is priced as high. Nano Banana Pro is a flat price per resolution tier."""
    table = (prices or get_config("image_prices"))[model_key]
    if model_key == "nano_banana_pro":
        return float(table[resolution])
    rates = table["edit" if edit else "text"]
    return float(rates.get(quality, rates["high"]))


def _fal_generate(model_key: str, prompt: str, refs: list, quality: str, mode: str) -> tuple[bytes, dict]:
    import fal_client

    spec = IMAGE_MODELS[model_key]
    edit = bool(refs)
    endpoint = spec["fal_edit"] if edit else spec["fal_text"]
    if model_key == "gpt_image_flare":
        w, h = (int(x) for x in request_size(mode).split("x"))
        if w % 16 or h % 16:
            raise PermanentError(f"image_size_mode {mode!r} asks for {w}x{h}; fal's GPT Image needs both sides to be multiples of 16")
        args = {"prompt": prompt, "image_size": {"width": w, "height": h}, "quality": quality, "output_format": "png", "num_images": 1}
        resolution = ""
    else:
        resolution = get_config("nano_banana_resolution")
        if resolution not in ("1K", "2K", "4K"):
            raise PermanentError(f"nano_banana_resolution must be 1K, 2K or 4K, not {resolution!r}")
        args = {"prompt": prompt, "aspect_ratio": "9:16", "resolution": resolution, "output_format": "png", "num_images": 1}
    if edit:
        args["image_urls"] = [_fal_ref(r) for r in refs]
    try:
        result = fal_client.subscribe(endpoint, arguments=args, interval=2.0, client_timeout=300)
        files = (result or {}).get("images") or []
        if not files:
            raise PermanentError(f"{endpoint} returned no image (blocked by its content check, or empty): {str(result)[:200]}")
        data = httpx.get(files[0]["url"], timeout=120, follow_redirects=True)
        check_response(data)
        raw = data.content
    except Exception as exc:  # noqa: BLE001
        raise map_exception(exc) from exc
    png = fit_to_target(raw)
    cost = fal_image_cost(model_key, edit=edit, quality=quality, resolution=resolution)
    return png, {"model": endpoint, "model_key": model_key, "vendor": "fal", "cost_usd": cost, "size": f"{files[0].get('width')}x{files[0].get('height')}", "references": len(refs)}


def _ref(image: bytes | str) -> dict:
    url = "data:image/png;base64," + base64.b64encode(image).decode() if isinstance(image, bytes) else image
    return {"image_url": url}


def generate_image(prompt: str, references: list[bytes | str] | None = None, quality: str | None = None, *, purpose: str = "default", model: str | None = None) -> tuple[bytes, dict]:
    """Returns (1080x1920 PNG bytes, usage). `references` are bytes or URLs. `purpose` picks the model from config image_models;
    `model` forces one (used when a person asked for Nano Banana Pro). usage carries model, model_key, vendor and cost_usd."""
    quality = quality or get_config("image_quality")
    if quality not in QUALITIES:
        raise PermanentError(f"unknown image quality {quality!r}")
    refs = references or []
    key = model_for(purpose, model)
    limit = IMAGE_MODELS[key]["max_refs"]
    if len(refs) > limit:
        raise PermanentError(f"{IMAGE_MODELS[key]['label']} takes at most {limit} reference images, got {len(refs)}")
    mode = get_config("image_size_mode")
    size = request_size(mode)
    via = provider_for(key)

    if os.environ.get("IMAGE_BACKEND") == "local_sd":
        # Opt-in open-source generator for tests; free, so it also runs under DRY_RUN=1 (see local_images.py).
        from app.vendors import local_images

        return local_images.generate(prompt, refs)
    if dry_run():
        return fixtures.fixture_bytes("image_1080x1920.png"), {"model": IMAGE_MODELS[key]["label"], "model_key": key, "vendor": via, "cost_usd": 0.0, "size": size, "references": len(refs)}

    if via == "fal":
        return _fal_generate(key, prompt, refs, quality, mode)
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
    return png, {"model": IMAGE_MODEL, "model_key": key, "vendor": "openai", "cost_usd": image_cost(usage), "size": size, "usage": usage}


def _fal_embed(texts: list[str]) -> tuple[list[list[float]], dict]:
    import fal_client

    vectors: list[list[float]] = []
    tokens, cost, priced = 0, 0.0, True
    for i in range(0, len(texts), FAL_EMBED_BATCH):
        batch = texts[i:i + FAL_EMBED_BATCH]
        try:
            result = fal_client.subscribe(FAL_EMBEDDING_ENDPOINT, arguments={"model": FAL_EMBEDDING_MODEL, "input": batch}, interval=1.0, client_timeout=180)
        except Exception as exc:  # noqa: BLE001
            raise map_exception(exc) from exc
        rows = sorted((result or {}).get("data") or [], key=lambda d: d.get("index", 0))
        if len(rows) != len(batch):
            raise PermanentError(f"the embeddings endpoint returned {len(rows)} vectors for {len(batch)} texts: {str(result)[:200]}")
        for r in rows:
            vec = r.get("embedding")
            if not isinstance(vec, list) or len(vec) != EMBEDDING_DIMENSIONS:
                raise PermanentError(f"expected {EMBEDDING_DIMENSIONS}-number embeddings from {FAL_EMBEDDING_MODEL}, got {len(vec) if isinstance(vec, list) else type(vec).__name__}")
            vectors.append([float(x) for x in vec])
        usage = (result or {}).get("usage") or {}
        tokens += int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
        if isinstance(usage.get("cost"), (int, float)):
            cost += float(usage["cost"])
        else:
            priced = False
    if not priced:     # fal returned no cost: price the tokens at the published OpenAI rate
        cost = tokens / 1e6 * get_config("price_book")["embedding_per_m"][EMBEDDING_MODEL]
    return vectors, {"model": f"fal:{FAL_EMBEDDING_MODEL}", "vendor": "fal", "tokens": tokens, "cost_usd": cost}


def embed(texts: list[str]) -> tuple[list[list[float]], dict]:
    """1536-dimension embeddings (text-embedding-3-small), through fal by default (config embedding_provider). Callers batch in groups of 100."""
    if not texts:
        return [], {"model": EMBEDDING_MODEL, "vendor": "fal", "tokens": 0, "cost_usd": 0.0}
    if os.environ.get("EMBEDDING_BACKEND") == "local":  # opt-in open-source backend for tests; free, so also under DRY_RUN=1
        from app.vendors import local_embeddings

        return local_embeddings.embed(texts), {"model": f"local:{local_embeddings.model_id()}", "vendor": "local", "tokens": 0, "cost_usd": 0.0}
    if dry_run():
        return [fixtures.fake_embedding(t) for t in texts], {"model": EMBEDDING_MODEL, "vendor": "fal", "tokens": 0, "cost_usd": 0.0}
    if get_config("embedding_provider") == "fal":
        return _fal_embed(texts)
    try:
        from openai import OpenAI

        result = OpenAI().embeddings.create(model=EMBEDDING_MODEL, input=texts)
    except Exception as exc:  # noqa: BLE001
        raise map_exception(exc) from exc
    tokens = result.usage.total_tokens
    cost = tokens / 1e6 * get_config("price_book")["embedding_per_m"][EMBEDDING_MODEL]
    return [d.embedding for d in result.data], {"model": EMBEDDING_MODEL, "vendor": "openai", "tokens": tokens, "cost_usd": cost}
