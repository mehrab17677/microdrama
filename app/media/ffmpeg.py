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


def run(args: list[str]) -> None:
    """Run an ffmpeg command as an argument list (never a shell). Raises PermanentError with the tail of stderr."""
    from app.jobs.errors import PermanentError

    proc = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise PermanentError(f"ffmpeg failed: {proc.stderr.strip()[-400:]}")


LIPSYNC_AUDIO_SECONDS = (2.0, 60.0)
LIPSYNC_AUDIO_MAX_BYTES = 5 * 1024 * 1024


def build_shot_audio(line_files: list[str | Path], line_seconds: list[float], timing: dict, total: float, out: str | Path) -> dict:
    """shot_audio.mp3 (guide 10.3): silence(pad_head) + line 1 + silence(line_gap) + line 2 + ..., padded or cut to
    exactly `total` seconds. mono, 44.1 kHz, 128 kbps. Asserts the Kling lip-sync limits (2 to 60 s, 5 MB)."""
    from app.jobs.errors import PermanentError

    lo, hi = LIPSYNC_AUDIO_SECONDS
    if not lo <= total <= hi:
        raise PermanentError(f"shot audio would be {total:.2f} s; it must be {lo:g} to {hi:g} s")
    if len(line_files) != len(line_seconds):
        raise PermanentError("one duration is needed for each line file")
    args: list[str] = []
    labels: list[str] = []

    def silence(seconds: float) -> None:
        args.extend(["-f", "lavfi", "-t", f"{seconds:.3f}", "-i", "anullsrc=r=44100:cl=mono"])
        labels.append(f"[{len(labels)}:a]")

    silence(timing["pad_head"])
    for i, path in enumerate(line_files):
        if i:
            silence(timing["line_gap"])
        args.extend(["-i", str(path)])
        labels.append(f"[{len(labels)}:a]")
    graph = ";".join(f"{lab}aresample=44100,aformat=sample_fmts=fltp:channel_layouts=mono[a{n}]" for n, lab in enumerate(labels))
    graph += ";" + "".join(f"[a{n}]" for n in range(len(labels))) + f"concat=n={len(labels)}:v=0:a=1[c];[c]apad=whole_dur={total:.3f}[o]"
    run([*args, "-filter_complex", graph, "-map", "[o]", "-t", f"{total:.3f}", "-ac", "1", "-ar", "44100", "-b:a", "128k", str(out)])
    size = Path(out).stat().st_size
    if size > LIPSYNC_AUDIO_MAX_BYTES:
        raise PermanentError(f"shot audio is {size} bytes; the limit is 5 MB")
    return {"seconds": duration(out), "bytes": size}
