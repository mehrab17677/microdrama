"""Real-money smoke test for the vendor clients (guide section 13, phase 3). Run by a person.

    DRY_RUN=0 python scripts/smoke_vendors.py --out DIR --steps openai_text,embeddings,image,seedance --yes-spend

Steps (estimated cost): openai_text (~$0), embeddings (~$0), image (2 images), seedance (4 s silent
1080p, ~$0.23), avatar (3 s audio, ~$0.35), lipsync (4 s clip, ~$0.07), latentsync (4 s clip, $0.20),
brandshot (4 s 720p Seedance 2.0 with a real product photo, ~$1.20; needs SMOKE_PRODUCT_PHOTO), speech/music (ElevenLabs, paid plan).
It records resolution, frame rate and the path that worked, for docs/VENDOR_NOTES.md.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel  # noqa: E402

from app.config import dry_run  # noqa: E402
from app.vendors import eleven, falmedia, images, llm  # noqa: E402

RESULTS: dict[str, dict] = {}


class OneWord(BaseModel):
    word: str


def data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,r_frame_rate,duration,codec_name", "-show_entries",
         "format=duration,size", "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    return {"video": video and {k: video.get(k) for k in ("width", "height", "r_frame_rate", "codec_name")},
            "has_audio": audio is not None, "duration": float(info["format"]["duration"]), "bytes": int(info["format"]["size"])}


def wait_for_fal(endpoint: str, request_id: str, timeout_s: int = 900) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        state, payload = falmedia.poll_status({"endpoint": endpoint, "vendor_request_id": request_id})
        if state == "done":
            return payload
        if state == "error":
            raise RuntimeError(f"fal error: {payload}")
        time.sleep(5)
    raise TimeoutError(endpoint)


def step_openai_text(out: Path) -> None:
    text, usage = llm.complete("copy", "Answer with one word.", [{"role": "user", "content": "Say ok."}])
    obj, usage2 = llm.complete("copy", "Return JSON.", [{"role": "user", "content": "Give any one English word."}], schema=OneWord)
    RESULTS["openai_text"] = {"text": text[:40], "structured": obj.model_dump(), "usage": usage, "usage_structured": usage2}


def step_embeddings(out: Path) -> None:
    vecs, usage = images.embed(["smoke test"])
    RESULTS["embeddings"] = {"dims": len(vecs[0]), "usage": usage}


def step_image(out: Path) -> None:
    from PIL import Image

    png, usage = images.generate_image("Front-facing head-and-shoulders portrait of an adult woman, neutral expression, even lighting, plain background")
    (out / "image_plain.png").write_bytes(png)
    png2, usage2 = images.generate_image("The same woman in a red coat, full-body, standing, plain background", references=[png])
    (out / "image_ref.png").write_bytes(png2)
    RESULTS["image"] = {
        "plain": {"size": Image.open(out / "image_plain.png").size, "usage": usage},
        "with_reference": {"size": Image.open(out / "image_ref.png").size, "usage": usage2},
    }


def step_seedance(out: Path) -> None:
    image = next((p for p in (out / "image_ref.png", out / "image_plain.png") if p.exists()), ROOT / "tests/fixtures/image_1080x1920.png")
    args = {"prompt": "She turns her head slowly and looks at the camera. Soft room ambience.", "image_url": data_uri(image.read_bytes(), "image/png"),
            "resolution": "1080p", "duration": "4", "aspect_ratio": "9:16", "generate_audio": False, "camera_fixed": False}
    rid = falmedia.submit(falmedia.SEEDANCE, args)
    payload = wait_for_fal(falmedia.SEEDANCE, rid)
    path = falmedia.download(falmedia.video_url(payload), out / "seedance.mp4")
    RESULTS["seedance"] = {"request_id": rid, "probe": probe(path), "video_url": falmedia.video_url(payload),
                           "estimated_cost": falmedia.estimate_cost("seedance_1_5_pro", duration=4, audio=False, fps=24), "payload_keys": sorted(payload)}


def tone(out: Path, seconds: int) -> Path:
    path = out / f"tone_{seconds}s.mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=200:duration={seconds}", "-ac", "1", "-ar", "44100", "-b:a", "128k", str(path)], check=True)
    return path


def step_avatar(out: Path) -> None:
    image = out / "image_plain.png"
    audio = tone(out, 3)
    args = {"image_url": data_uri(image.read_bytes(), "image/png"), "audio_url": data_uri(audio.read_bytes(), "audio/mpeg"), "prompt": "."}
    rid = falmedia.submit(falmedia.KLING_AVATAR, args)
    payload = wait_for_fal(falmedia.KLING_AVATAR, rid)
    path = falmedia.download(falmedia.video_url(payload), out / "avatar.mp4")
    RESULTS["avatar"] = {"request_id": rid, "probe": probe(path), "payload_keys": sorted(payload), "estimated_cost": falmedia.estimate_cost("kling_avatar_v2_pro", duration=3)}


def step_lipsync(out: Path) -> None:
    video_url = RESULTS.get("seedance", {}).get("video_url")
    if not video_url:
        raise RuntimeError("run the seedance step first: lip-sync is tested on a Seedance 1080p clip (guide section 14)")
    audio = tone(out, 4)
    rid = falmedia.submit(falmedia.KLING_LIPSYNC, {"video_url": video_url, "audio_url": data_uri(audio.read_bytes(), "audio/mpeg")})
    payload = wait_for_fal(falmedia.KLING_LIPSYNC, rid)
    path = falmedia.download(falmedia.video_url(payload), out / "lipsync.mp4")
    RESULTS["lipsync"] = {"request_id": rid, "probe": probe(path), "estimated_cost": falmedia.estimate_cost("kling_lipsync", duration=4)}


def step_speech(out: Path) -> None:
    res = {}
    for lang, text in (("en", "[calm] I did not expect to see you here tonight."), ("bn", "[calm] আমি আজ রাতে তোমাকে এখানে দেখব ভাবিনি।")):
        voices = eleven.design_voice("A calm adult woman with a warm low voice" + (", Bangladeshi accent" if lang == "bn" else ""))[0]
        voice_id = eleven.save_voice(voices[0]["generated_voice_id"], f"smoke-{lang}", "smoke test voice")
        data, usage = eleven.speech(text, voice_id, lang, 1)
        (out / f"speech_{lang}.mp3").write_bytes(data["audio"])
        res[lang] = {"voice_id": voice_id, "probe": probe(out / f"speech_{lang}.mp3"), "words": data["words"][:3], "alignment_null": data["alignment"] is None, "usage": usage}
    RESULTS["speech"] = res


def step_music(out: Path) -> None:
    audio, usage = eleven.music("Tense cinematic underscore, slow pulse, instrumental, no vocals", 10_000)
    (out / "music.mp3").write_bytes(audio)
    RESULTS["music"] = {"probe": probe(out / "music.mp3"), "usage": usage}


def step_brandshot(out: Path) -> None:
    """One 4 s 720p Seedance 2.0 reference-to-video shot with a REAL product photo and logo (--product-photo, --logo): does fal/ByteDance accept real brand
    assets, and is the label kept? About $1.20. Look at the result by eye and with the label check before any ad pilot."""
    import os

    photo, logo = os.environ.get("SMOKE_PRODUCT_PHOTO"), os.environ.get("SMOKE_LOGO")
    if not photo:
        raise RuntimeError("set SMOKE_PRODUCT_PHOTO (and SMOKE_LOGO) to the paths of the client's real product photo and logo")
    refs = [data_uri(Path(photo).read_bytes(), "image/png")] + ([data_uri(Path(logo).read_bytes(), "image/png")] if logo else [])
    prompt = ("@Image1 shows the exact product: same shape, colours, proportions and label; do not redraw or invent any printed text. "
              "A woman at a kitchen table takes the product from her bag and holds it up toward the camera for three seconds, label facing the lens. "
              "The camera is steady. No other text, captions, logos or brands appear. No music. No speech.")
    args = {"prompt": prompt, "image_urls": refs, "resolution": "720p", "duration": "4", "aspect_ratio": "9:16", "generate_audio": False}
    rid = falmedia.submit(falmedia.SEEDANCE2_REF, args)
    payload = wait_for_fal(falmedia.SEEDANCE2_REF, rid)
    path = falmedia.download(falmedia.video_url(payload), out / "brandshot.mp4")
    RESULTS["brandshot"] = {"request_id": rid, "probe": probe(path), "estimated_cost": round(4 * 0.3024, 4), "note": "check the label and logo by eye; record whether moderation accepted the real logo"}


def step_latentsync(out: Path) -> None:
    video_url = RESULTS.get("seedance", {}).get("video_url")
    if not video_url:
        raise RuntimeError("run the seedance step first")
    audio = tone(out, 4)
    rid = falmedia.submit(falmedia.LATENTSYNC, {"video_url": video_url, "audio_url": data_uri(audio.read_bytes(), "audio/mpeg"), "guidance_scale": 1.0, "seed": 1})
    payload = wait_for_fal(falmedia.LATENTSYNC, rid)
    path = falmedia.download(falmedia.video_url(payload), out / "latentsync.mp4")
    RESULTS["latentsync"] = {"request_id": rid, "probe": probe(path), "estimated_cost": falmedia.estimate_cost("latentsync", duration=4)}


STEPS = {f.__name__[5:]: f for f in (step_openai_text, step_embeddings, step_image, step_seedance, step_avatar, step_lipsync, step_latentsync, step_brandshot, step_speech, step_music)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--steps", required=True, help="comma separated: " + ",".join(STEPS))
    ap.add_argument("--yes-spend", action="store_true", help="confirm that real money may be spent")
    args = ap.parse_args()
    if dry_run():
        sys.exit("DRY_RUN is set: nothing would be called. Run with DRY_RUN=0.")
    if not args.yes_spend:
        sys.exit("pass --yes-spend to confirm real calls")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name in args.steps.split(","):
        try:
            STEPS[name](out)
            print(f"OK   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            RESULTS[name] = {"error": f"{type(exc).__name__}: {str(exc)[:400]}"}
            print(f"FAIL {name}: {RESULTS[name]['error']}")
    print(json.dumps(RESULTS, indent=2, default=str))
    (out / "smoke_results.json").write_text(json.dumps(RESULTS, indent=2, default=str))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
