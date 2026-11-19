"""Create the small media fixtures used by DRY_RUN (needs ffmpeg and Pillow). Outputs are committed."""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1080, 1920))
    px = img.load()
    for y in range(1920):
        for x in range(0, 1080):
            px[x, y] = (int(40 + 120 * y / 1920), int(60 + 100 * x / 1080), 150)
    d = ImageDraw.Draw(img)
    d.ellipse((340, 500, 740, 900), fill=(230, 200, 180))  # a plain "face" shape so detectors have something to find
    img.save(OUT / "image_1080x1920.png", optimize=True)
    run = lambda *a: subprocess.run(["ffmpeg", "-y", "-v", "error", *a], check=True)  # noqa: E731
    run("-f", "lavfi", "-i", "sine=frequency=330:duration=2", "-ac", "1", "-ar", "44100", "-b:a", "128k", str(OUT / "speech_2s.mp3"))
    run("-f", "lavfi", "-i", "sine=frequency=220:duration=30", "-ac", "1", "-ar", "44100", "-b:a", "64k", str(OUT / "ambience_30s.mp3"))
    run("-f", "lavfi", "-i", "sine=frequency=262:duration=30", "-ac", "2", "-ar", "44100", "-b:a", "64k", str(OUT / "music_30s.mp3"))
    run(
        "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=24:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "34", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-shortest",
        str(OUT / "clip_4s_1080x1920.mp4"),
    )
    for p in sorted(OUT.iterdir()):
        print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
