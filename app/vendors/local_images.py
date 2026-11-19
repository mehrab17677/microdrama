"""Open-source image backend for tests (IMAGE_BACKEND=local_sd): SD-Turbo through diffusers, on CPU.

NOT for production. It exists so the look and build stages can be exercised with real generated faces
when no paid image API is available. SD-Turbo is under the Stability AI Community License (free for
research, non-commercial and limited commercial use; see docs/VENDOR_NOTES.md).

How it fits the pipeline's 1080x1920 requirement: the model is trained on squares, and asking it for a
tall canvas stacks duplicate faces. So it generates at 512x512 and the result is placed in the middle of
a 9:16 canvas over a blurred, enlarged copy of itself.
Reference images are used as the starting point of an image-to-image run (not as identity guidance),
so "the same person" is only approximate. Good enough to test the pipeline, not to judge quality.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import threading
import time

import httpx
from PIL import Image, ImageFilter

MODEL_ID_DEFAULT = "stabilityai/sd-turbo"
# Sheets this backend cannot make: SD-Turbo cannot hold one identity across full-body angles, tight
# expression close-ups or outfits, and img2img from the prime portrait only changes its look slightly.
# The pipeline skips these visibly instead of producing near-identical copies (see images.unsupported_pack_kinds).
UNSUPPORTED_PACK_KINDS = frozenset({"turnaround", "expression", "wardrobe", "pose", "lineup"})
NATIVE = 512
TARGET = (1080, 1920)
_lock = threading.Lock()
_pipes: dict | None = None


def model_id() -> str:
    return os.environ.get("LOCAL_SD_MODEL", MODEL_ID_DEFAULT)


PROMPT_WORDS = 55   # the model reads 77 tokens; punctuation and word pieces use up the rest


def prepare_prompt(prompt: str, max_words: int = PROMPT_WORDS) -> str:
    """The pipeline's prompts end with the most specific instruction (the fixed portrait text, the pose, the
    plate rule) and the model silently drops everything past 77 tokens. Put that last unit first, then as much
    of the rest as fits."""
    units = [u.strip() for u in re.split(r"\n+|(?<=[.!?])\s+", prompt) if u.strip()]
    if not units:
        return prompt
    ordered = [units[-1], *units[:-1]]
    words: list[str] = []
    for unit in ordered:
        take = unit.split()
        if len(words) + len(take) > max_words:
            take = take[: max(0, max_words - len(words))]
        words += take
        if len(words) >= max_words:
            break
    return " ".join(words)


def fit_to_9x16(img: Image.Image) -> Image.Image:
    """The square result, centred full-width on a 1080x1920 canvas over a blurred enlarged copy."""
    img = img.convert("RGB")
    canvas = img.resize(TARGET, Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
    band = img.resize((TARGET[0], TARGET[0]), Image.LANCZOS)
    canvas.paste(band, (0, (TARGET[1] - TARGET[0]) // 2))
    return canvas


def center_square(img: Image.Image) -> Image.Image:
    """The middle square of an image (for a 9:16 master this is the sharp band, not the blurred bands)."""
    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def load_reference(ref: bytes | str) -> Image.Image:
    if isinstance(ref, bytes):
        return Image.open(io.BytesIO(ref)).convert("RGB")
    if ref.startswith("data:"):
        return Image.open(io.BytesIO(base64.b64decode(ref.split(",", 1)[1]))).convert("RGB")
    if ref.startswith("file://"):
        return Image.open(httpx.URL(ref).path).convert("RGB")
    return Image.open(io.BytesIO(httpx.get(ref, timeout=60, follow_redirects=True).content)).convert("RGB")


def _load_pipes() -> dict:
    """Load once per process (about 25 s the first time, 2.5 GB on disk)."""
    global _pipes
    with _lock:
        if _pipes is None:
            import torch
            from diffusers import AutoPipelineForImage2Image, AutoPipelineForText2Image

            t2i = AutoPipelineForText2Image.from_pretrained(model_id(), torch_dtype=torch.float32, variant="fp16", safety_checker=None)
            t2i.set_progress_bar_config(disable=True)
            i2i = AutoPipelineForImage2Image.from_pipe(t2i)
            i2i.set_progress_bar_config(disable=True)
            _pipes = {"t2i": t2i, "i2i": i2i}
        return _pipes


def _seed(prompt: str) -> int:
    return int(hashlib.sha256(prompt.encode()).hexdigest()[:8], 16)


def generate(prompt: str, references: list[bytes | str] | None = None) -> tuple[bytes, dict]:
    """Returns (1080x1920 PNG bytes, usage). Free: cost_usd is 0."""
    import torch

    pipes = _load_pipes()
    gen = torch.Generator().manual_seed(_seed(prompt))
    prompt = prepare_prompt(prompt)
    started = time.time()
    with _lock:  # one generation at a time per process
        if references:
            init = center_square(load_reference(references[0])).resize((NATIVE, NATIVE), Image.LANCZOS)
            # SD-Turbo needs steps * strength >= 1
            image = pipes["i2i"](prompt=prompt, image=init, strength=0.6, num_inference_steps=2, guidance_scale=0.0, generator=gen).images[0]
        else:
            image = pipes["t2i"](prompt=prompt, num_inference_steps=1, guidance_scale=0.0, width=NATIVE, height=NATIVE, generator=gen).images[0]
    out = io.BytesIO()
    fit_to_9x16(image).save(out, format="PNG")
    return out.getvalue(), {"model": f"local:{model_id()}", "cost_usd": 0.0, "size": f"{NATIVE}x{NATIVE}", "seconds": round(time.time() - started, 1),
                            "used_reference": bool(references)}
