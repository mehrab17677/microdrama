"""Preflight (guide 6.6). Prints one line per check and exits non-zero if any check fails.

DRY_RUN=1 skips every check that needs a paid vendor, R2 or network; the database, ffmpeg,
Bangla rendering and face-model checks always run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

OK, FAIL, SKIP = "OK", "FAIL", "SKIP"
Result = tuple[str, str]  # (status, message)

REQUIRED_CLAUDE_ROLES = ("story", "director", "vision_qa")


def check_database() -> Result:
    from app import db

    try:
        with db.connect() as conn:
            conn.execute("select 1")
            ext = conn.execute("select 1 from pg_extension where extname = 'vector'").fetchone()
            if not ext:
                return FAIL, "pgvector extension is not installed"
            have = {r["name"] for r in conn.execute("select name from schema_migrations").fetchall()}
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"cannot use database: {type(exc).__name__}: {exc}"
    wanted = {p.name for p in (ROOT / "migrations").glob("*.sql")}
    missing = sorted(wanted - have)
    if missing:
        return FAIL, f"migrations not applied: {', '.join(missing)}"
    return OK, f"reachable, vector present, {len(have)} migration(s) applied"


def check_config_seeded() -> Result:
    from app import db
    from app.config_defaults import CONFIG_DEFAULTS

    try:
        have = {r["key"] for r in db.fetch_all("select key from config")}
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"cannot read config: {exc}"
    missing = sorted(set(CONFIG_DEFAULTS) - have)
    return (FAIL, f"config keys missing, run scripts/seed_config.py: {', '.join(missing)}") if missing else (OK, f"{len(have)} config keys")


def check_ffmpeg() -> Result:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return FAIL, "ffmpeg/ffprobe not on PATH"
    out = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True).stdout
    needed = ["--enable-libx264", "--enable-libass", "--enable-libfribidi"]
    missing = [n.replace("--enable-", "") for n in needed if n not in out]
    # harfbuzz shapes Bengali conjuncts. Some builds (Debian bookworm) get it through libass and do not list the flag,
    # so accept either the flag or a linked library. The Bangla caption check below is the real proof.
    if "--enable-libharfbuzz" not in out and not _links_harfbuzz():
        missing.append("libharfbuzz")
    return (FAIL, f"ffmpeg is missing: {', '.join(missing)}") if missing else (OK, "libx264, libass, harfbuzz, fribidi present")


def _links_harfbuzz() -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ldd"):
        return False
    return "libharfbuzz" in subprocess.run(["ldd", ffmpeg], capture_output=True, text=True).stdout


def check_bangla_caption() -> Result:
    """Render a Bangla caption (with conjuncts) through libass and check it produced pixels
    with the Noto Sans Bengali font resolved by fontconfig."""
    match = subprocess.run(["fc-match", "-f", "%{family}|%{file}", "Noto Sans Bengali"], capture_output=True, text=True)
    if "Bengali" not in match.stdout:
        return FAIL, f"Noto Sans Bengali not installed (fontconfig gave: {match.stdout or match.stderr})"
    with tempfile.TemporaryDirectory() as tmp:
        ass = Path(tmp) / "t.ass"
        ass.write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 400\n\n"
            "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BorderStyle,Outline,Alignment,MarginV\n"
            "Style: Default,Noto Sans Bengali,64,&H00FFFFFF,&H00000000,1,4,5,20\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Text\n"
            "Dialogue: 0,0:00:00.00,0:00:05.00,Default,আমি তোমাকে ভালোবাসি, বিশ্বাস করো\n",
            encoding="utf-8",
        )
        png = Path(tmp) / "out.png"
        run = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1080x400:d=1", "-vf", f"ass={ass}", "-frames:v", "1", str(png)],
            capture_output=True, text=True,
        )
        if run.returncode != 0 or not png.exists():
            return FAIL, f"ffmpeg ass render failed: {run.stderr.strip()[:200]}"
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(png), "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True
        ).stdout
        white = sum(1 for b in raw if b > 200)
        if white < 500:
            return FAIL, f"caption render looks blank ({white} bright pixels)"
    return OK, f"rendered with {match.stdout.split('|')[0]} ({white} bright pixels)"


def check_face_models() -> Result:
    try:
        import cv2

        cv2.FaceDetectorYN.create(str(ROOT / "models/face_detection_yunet_2026may.onnx"), "", (320, 320))
        cv2.FaceRecognizerSF.create(str(ROOT / "models/face_recognition_sface_2021dec.onnx"), "")
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"YuNet/SFace failed to load: {exc}"
    return OK, f"YuNet and SFace load (OpenCV {cv2.__version__})"


# ---- vendor checks (skipped in dry-run) -------------------------------------------------

def check_r2() -> Result:
    s = get_settings()
    if not s.r2_configured:
        return FAIL, "R2_ACCOUNT_ID (or R2_ENDPOINT), R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET must all be set"
    import boto3
    import httpx
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=s.r2_endpoint,
        aws_access_key_id=s.r2_access_key_id,
        aws_secret_access_key=s.r2_secret_access_key,
        region_name="auto",
        config=Config(request_checksum_calculation="when_required", response_checksum_validation="when_required"),
    )
    key = "preflight/test.txt"
    try:
        client.put_object(Bucket=s.r2_bucket, Key=key, Body=b"preflight", ContentType="text/plain")
        url = client.generate_presigned_url("get_object", Params={"Bucket": s.r2_bucket, "Key": key}, ExpiresIn=21600)
        body = httpx.get(url, timeout=30).content
        client.delete_object(Bucket=s.r2_bucket, Key=key)
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"R2 round trip failed: {type(exc).__name__}: {exc}"
    return (OK, "write, presign, fetch, delete") if body == b"preflight" else (FAIL, "presigned fetch returned different bytes")


def check_anthropic() -> Result:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return FAIL, "ANTHROPIC_API_KEY is not set"
    from app import db
    from app.config import get_config

    try:
        roles = get_config("llm_roles")
    except Exception:  # noqa: BLE001
        from app.config_defaults import CONFIG_DEFAULTS

        roles = CONFIG_DEFAULTS["llm_roles"][0]
    models = sorted({m.split(":", 1)[1] for m in roles.values() if m.startswith("anthropic:")})
    import anthropic

    client = anthropic.Anthropic()
    failures = []
    for model in models:
        try:
            client.messages.create(model=model, max_tokens=16, messages=[{"role": "user", "content": "Reply with one word: ok"}])
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{model}: {type(exc).__name__}")
    return (FAIL, "; ".join(failures)) if failures else (OK, f"{len(models)} Claude model(s) answered")


def check_openai() -> Result:
    if not os.environ.get("OPENAI_API_KEY"):
        return FAIL, "OPENAI_API_KEY is not set (use any placeholder when the proxy injects the key)"
    problems = []
    try:
        from langchain.chat_models import init_chat_model

        init_chat_model("openai:gpt-6-luna").invoke("Reply with one word: ok")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"gpt-6-luna: {type(exc).__name__}: {str(exc)[:120]}")
    try:
        from openai import OpenAI

        emb = OpenAI().embeddings.create(model="text-embedding-3-small", input=["preflight"])
        n = len(emb.data[0].embedding)
        if n != 1536:
            problems.append(f"embedding has {n} dimensions, expected 1536")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"embeddings: {type(exc).__name__}: {str(exc)[:120]}")
    return (FAIL, "; ".join(problems)) if problems else (OK, "gpt-6-luna answered; embeddings are 1536 numbers")


def check_elevenlabs() -> Result:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return FAIL, "ELEVENLABS_API_KEY is not set"
    import httpx

    resp = httpx.get("https://api.elevenlabs.io/v1/models", headers={"xi-api-key": key}, timeout=30)
    if resp.status_code == 401 and "missing_permissions" in resp.text:
        # A restricted key can generate without being allowed to read. Prove the key with a request that is rejected for its body (HTTP 422)
        # after authentication, which costs nothing; the Bengali check then needs a key with models_read or the voice test (guide section 13).
        probe = httpx.post("https://api.elevenlabs.io/v1/text-to-dialogue/with-timestamps", headers={"xi-api-key": key}, json={}, timeout=30)
        if probe.status_code == 422:
            return OK, "key accepted for speech (it lacks models_read, so Bengali on eleven_v3 is NOT verified: run the voice test)"
        return FAIL, f"key lacks models_read and the speech probe returned HTTP {probe.status_code}"
    if resp.status_code != 200:
        return FAIL, f"models endpoint returned HTTP {resp.status_code}"
    entry = next((m for m in resp.json() if m.get("model_id") == "eleven_v3"), None)
    if entry is None:
        return FAIL, "eleven_v3 is not in the models list"
    # The response shape for languages is not documented in what was read; search the entry tolerantly.
    text = str(entry).lower()
    if "bengali" not in text and "'bn'" not in text:
        return FAIL, "eleven_v3 does not list Bengali: STOP and ask the owner (guide section 14)"
    return OK, "eleven_v3 listed, Bengali present"


def check_fal() -> Result:
    import httpx

    key = os.environ.get("FAL_KEY")
    if not key:
        return FAIL, "FAL_KEY is not set (use any placeholder when the proxy injects the key)"
    # No documented key-check call exists; a status lookup for an unknown request id answers 404
    # with a valid key and 401/403 without one. Generates nothing. (Recorded in VENDOR_NOTES.md.)
    resp = httpx.get(
        "https://queue.fal.run/fal-ai/flux/requests/preflight-unknown-request/status",
        headers={"Authorization": f"Key {key}"}, timeout=30,
    )
    if resp.status_code in (401, 403):
        return FAIL, f"fal rejected the key (HTTP {resp.status_code})"
    return OK, f"key accepted (HTTP {resp.status_code} for an unknown request id)"


CORE: list[tuple[str, Callable[[], Result]]] = [
    ("database", check_database),
    ("config", check_config_seeded),
    ("ffmpeg", check_ffmpeg),
    ("bangla caption", check_bangla_caption),
    ("face models", check_face_models),
]
VENDOR: list[tuple[str, Callable[[], Result]]] = [
    ("r2", check_r2),
    ("anthropic", check_anthropic),
    ("openai", check_openai),
    ("elevenlabs", check_elevenlabs),
    ("fal", check_fal),
]


def run_all(dry_run: bool | None = None) -> list[tuple[str, str, str]]:
    dry = get_settings().dry_run if dry_run is None else dry_run
    results = []
    for name, fn in CORE:
        results.append((name, *_safe(fn)))
    for name, fn in VENDOR:
        results.append((name, SKIP, "dry-run: vendor not contacted") if dry else (name, *_safe(fn)))
    return results


def _safe(fn: Callable[[], Result]) -> Result:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"{type(exc).__name__}: {exc}"


def main() -> int:
    results = run_all()
    for name, status, message in results:
        print(f"{status:<4} {name}: {message}")
    failed = [n for n, s, _ in results if s == FAIL]
    print("preflight " + ("FAILED: " + ", ".join(failed) if failed else "passed"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
