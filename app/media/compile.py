"""Compile one episode (guide 10.6) and make static inserts (10.8). Pure ffmpeg work on local files:
the handler downloads inputs and uploads results. Commands are argument lists, never a shell."""
from __future__ import annotations

import os
from pathlib import Path

from app.media import ffmpeg

W, H, FPS = 1080, 1920, 24
VIDEO_FILTER = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={FPS}"


def preset() -> str:
    return os.environ.get("FFMPEG_PRESET", "medium")


def shot_video(clip: Path, planned: float, out: Path) -> None:
    """Trim to `planned`, scale and pad to 1080x1920 at 24 fps, no audio. A clip shorter than planned holds its last frame."""
    vf = f"{VIDEO_FILTER},tpad=stop_mode=clone:stop_duration={planned:.3f},format=yuv420p"
    ffmpeg.run(["-i", str(clip), "-t", f"{planned:.3f}", "-vf", vf, "-an", "-c:v", "libx264", "-preset", preset(), "-crf", "18", str(out)])


def shot_audio_wav(planned: float, out: Path, *, speech: Path | None = None, clip: Path | None = None, ambience: Path | None = None, ambience_db: float = -24,
                   gain_db: float = 0.0) -> None:
    """A WAV of exactly `planned` seconds, 48 kHz stereo.
    speaking: speech mixed with the looped, lowered ambience. non-speaking: the clip's own audio (silence if none).
    static insert: the ambience or silence."""
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    main = fmt + (f",volume={gain_db}dB" if gain_db else "")   # the clipping repair lowers the speech (or the clip's own sound), not the ambience
    tail = f"apad=whole_dur={planned:.3f},atrim=0:{planned:.3f}"
    args: list[str] = []
    if speech is not None:
        args += ["-i", str(speech)]
        if ambience is not None:
            args += ["-stream_loop", "-1", "-i", str(ambience)]
            graph = f"[0:a]{main}[s];[1:a]{fmt},volume={ambience_db}dB[a];[s][a]amix=inputs=2:duration=first:normalize=0,{tail}[o]"
        else:
            graph = f"[0:a]{main},{tail}[o]"
    elif clip is not None and ffmpeg.probe(clip)["has_audio"]:
        args += ["-i", str(clip)]
        graph = f"[0:a]{main},{tail}[o]"
    elif ambience is not None:
        args += ["-stream_loop", "-1", "-i", str(ambience)]
        graph = f"[0:a]{fmt},volume={ambience_db}dB,{tail}[o]"
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        graph = f"[0:a]{tail}[o]"
    ffmpeg.run([*args, "-filter_complex", graph, "-map", "[o]", "-ar", "48000", "-ac", "2", "-t", f"{planned:.3f}", str(out)])


def concat(parts: list[Path], out: Path, workdir: Path) -> None:
    listing = workdir / (out.stem + "_list.txt")
    listing.write_text("".join(f"file '{p}'\n" for p in parts))
    ffmpeg.run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)])


def level(joined: Path, out: Path) -> None:
    ffmpeg.run(["-i", str(joined), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000", "-ar", "48000", "-ac", "2", str(out)])


def with_music(dialogue: Path, music: Path | None, runtime: float, out: Path) -> None:
    """Trim the music to the runtime with a 1-second fade out, duck it under the dialogue, mix."""
    if music is None:
        ffmpeg.run(["-i", str(dialogue), "-c", "copy", str(out)])
        return
    fade_start = max(0.0, runtime - 1)
    graph = (f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:{runtime:.3f},afade=t=out:st={fade_start:.3f}:d=1[m];"
             "[0:a]asplit=2[d][sc];[m][sc]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=400[duck];"
             "[d][duck]amix=inputs=2:duration=first:normalize=0[o]")
    ffmpeg.run(["-i", str(dialogue), "-stream_loop", "-1", "-i", str(music), "-filter_complex", graph, "-map", "[o]", "-ar", "48000", "-ac", "2", "-t", f"{runtime:.3f}", str(out)])


def encode_final(video: Path, audio: Path, ass: Path, out: Path) -> None:
    """Burn in the captions and encode H.264 (crf 18), AAC 192 kbps, faststart."""
    ffmpeg.run(["-i", str(video), "-i", str(audio), "-vf", f"ass={ass}", "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", preset(), "-crf", "18",
                "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out)])


def set_insert(plate: Path, planned: float, out: Path) -> None:
    """Loop the set plate for `planned` seconds with a slow zoom from 1.0 to 1.08."""
    frames = max(1, round(planned * FPS))
    zoom = f"zoompan=z='min(1+0.08*on/{frames},1.08)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS}"
    ffmpeg.run(["-loop", "1", "-framerate", str(FPS), "-t", f"{planned:.3f}", "-i", str(plate), "-vf", f"{zoom},format=yuv420p", "-an",
                "-c:v", "libx264", "-preset", preset(), "-crf", "18", str(out)])
