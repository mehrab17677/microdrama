"""Review images (guide 9.5): the clean image with a caption bar drawn by Pillow.
Never ask the image model to draw text; clean images carry no text at all."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BAR_HEIGHT = 150
FONT_DIR = Path("/usr/share/fonts/truetype/noto")


def _font(text: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    bengali = any("ঀ" <= c <= "৿" for c in text)
    candidates = [FONT_DIR / ("NotoSansBengali-Regular.ttf" if bengali else "NotoSans-Regular.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def caption_bar(png: bytes, title: str, subtitle: str = "") -> bytes:
    """The clean image with a dark bar along the bottom showing `title` (a character name) and `subtitle` (the tags)."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = img.size
    bar = Image.new("RGB", (w, BAR_HEIGHT), (16, 16, 20))
    draw = ImageDraw.Draw(bar)
    draw.text((32, 18), title, font=_font(title, 56), fill=(255, 255, 255))
    if subtitle:
        draw.text((32, 88), subtitle, font=_font(subtitle, 38), fill=(190, 190, 205))
    img.paste(bar, (0, h - BAR_HEIGHT))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()
