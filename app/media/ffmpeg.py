"""ffmpeg / ffprobe helpers. Commands run as argument lists, never through a shell (guide 10.6)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def probe(path: str | Path) -> dict:
    """{width, height, fps, duration, has_audio, bytes} from ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,r_frame_rate", "-show_entries", "format=duration,size", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    info = json.loads(out)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps = None
    if video and video.get("r_frame_rate") and "/" in video["r_frame_rate"]:
        num, den = video["r_frame_rate"].split("/")
        fps = round(float(num) / float(den), 3) if float(den) else None
    return {
        "width": video and video.get("width"), "height": video and video.get("height"), "fps": fps,
        "duration": float(info["format"]["duration"]), "has_audio": audio is not None, "bytes": int(info["format"]["size"]),
    }


def duration(path: str | Path) -> float:
    return probe(path)["duration"]
