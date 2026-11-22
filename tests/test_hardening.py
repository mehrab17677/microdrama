"""Pre-deployment hardening: login throttle, upload limits, deployment files."""
from pathlib import Path

from test_phase5 import PW, brief, client, env, post_json  # noqa: F401 (fixtures)

from app import db, intake
from fastapi.testclient import TestClient
from app.web.main import app


def anon():
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def test_repeated_failed_logins_are_throttled_per_address_and_the_answer_does_not_reveal_the_account(env):
    c = anon()
    for _ in range(intake_limit()):
        assert c.post("/auth/login", data={"email": "owner@x.io", "password": "wrong"}).status_code == 401
    blocked = c.post("/auth/login", data={"email": "owner@x.io", "password": PW})                 # even the right password is refused for now
    assert blocked.status_code == 429 and "Too many failed attempts" in blocked.text
    unknown = anon()
    for _ in range(intake_limit()):
        unknown.post("/auth/login", data={"email": "nobody@x.io", "password": "wrong"})
    assert unknown.post("/auth/login", data={"email": "nobody@x.io", "password": "wrong"}).status_code == 429     # an address that has no account gets the same answer
    assert anon().post("/auth/login", data={"email": "other@x.io", "password": PW}).status_code == 303           # other people are not affected
    db.execute("update audit_log set created_at = now() - interval '20 minutes' where action = 'auth.login_failed'")
    assert c.post("/auth/login", data={"email": "owner@x.io", "password": PW}).status_code == 303                # after 15 minutes it works again


def intake_limit():
    from app.web.routes import auth

    return auth.MAX_FAILED_LOGINS


def test_oversized_pictures_are_rejected_with_a_message(env):
    c = client()
    data, files = brief()
    big = b"\x00" * (intake.MAX_IMAGE_BYTES + 1)
    files = [("char_files", ("lead.png", big, "image/png"))]
    text = " | ".join(post_json(c, data, files).json()["errors"])
    assert "Character reference 1 is larger than 25 MB." in text


def test_the_proxy_limits_bodies_and_sets_security_headers():
    caddy = (Path(__file__).resolve().parent.parent / "Caddyfile").read_text()
    for needle in ("max_size", "Strict-Transport-Security", "X-Content-Type-Options nosniff", "X-Frame-Options DENY", "reverse_proxy web:8000"):
        assert needle in caddy


def test_every_compose_service_has_a_command_and_the_image_has_what_the_media_code_needs():
    root = Path(__file__).resolve().parent.parent
    compose, docker = (root / "docker-compose.yml").read_text(), (root / "Dockerfile").read_text()
    for service in ("web", "worker", "runner", "poller", "retention", "migrate"):
        assert f"\n  {service}:" in compose
    for needle in ("ffmpeg", "fonts-noto-core", "USER app", "requirements.txt"):
        assert needle in docker
    assert (root / "models" / "face_detection_yunet_2026may.onnx").exists() and (root / "models" / "face_recognition_sface_2021dec.onnx").exists()


def test_the_reembed_script_remakes_every_vector_with_the_current_backend(env, monkeypatch):
    import uuid

    from app.vendors import images
    from scripts import reembed_library

    genre = db.fetch_one("select id from genres limit 1")["id"]
    src = db.fetch_one("insert into sources (genre_id, batch_id, file_name, file_sha256, r2_key, status, language) values (%s, %s, 'a.txt', 'sha-re', 'k', 'ready', 'bn') returning id", (genre, uuid.uuid4()))["id"]
    old = db.to_vector([1.0] + [0.0] * 1535)
    db.execute("insert into source_chunks (source_id, chapter_index, text, embedding) values (%s, 0, 'chunk text', %s::vector)", (src, old))
    db.execute("insert into pattern_cards (source_id, genre_id, kind, title, body, embedding) values (%s, %s, 'trope', 'Title', 'Body', %s::vector)", (src, genre, old))
    db.execute("insert into camera_setups (name, shot_size, angle, movement, composition, best_for, embedding) values ('N','medium','eye','static','centred','talk', %s::vector)", (old,))
    db.execute("insert into learned_rules (model_key, rule_text, category, embedding) values ('m', 'Avoid x.', 'c', %s::vector)", (old,))
    seen = []
    real = images.embed
    monkeypatch.setattr(images, "embed", lambda texts: (seen.extend(texts), real(texts))[1])
    assert reembed_library.run(dry_run=True) == {"source_chunks": 1, "pattern_cards": 1, "camera_setups": 1, "learned_rules": 1} and seen == []
    reembed_library.run()
    assert sorted(seen) == sorted(["chunk text", "Title. Body", "N. medium, eye, static. centred. Best for talk", "Avoid x."])
    for table in ("source_chunks", "pattern_cards", "camera_setups", "learned_rules"):
        assert db.fetch_one(f"select embedding::text e from {table}")["e"] != str(old)                                   # replaced by the current backend's vector
    monkeypatch.setenv("EMBEDDING_BACKEND", "local")
    import pytest

    with pytest.raises(SystemExit):
        reembed_library.run()


def test_the_compose_file_is_ready_for_a_single_server():
    root = Path(__file__).resolve().parent.parent
    compose = (root / "docker-compose.yml").read_text()
    assert "PUBLIC_HOST: ${PUBLIC_HOST:-localhost}" in compose                    # Caddy needs the host name to get a certificate
    assert "service_healthy" in compose and "service_completed_successfully" in compose and "pg_isready" in compose    # the database is waited for, then migrated, then the app starts
    assert compose.count("media:/tmp/microdrama") == 1 and "\n  media:" in compose  # one shared folder for DRY_RUN files, used by every app process via the anchor
    for script in ("gcp_create_vm.sh", "vm_bootstrap.sh"):
        text = (root / "scripts" / script).read_text()
        assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert "DRY_RUN=1" in (root / "scripts" / "vm_bootstrap.sh").read_text() and "sslip.io" in (root / "scripts" / "gcp_create_vm.sh").read_text()
    assert (root / "docs" / "GCP_STAGING_SETUP.md").exists()


def test_preflight_accepts_ffmpeg_that_gets_harfbuzz_through_libass(monkeypatch):
    from scripts import preflight

    banner = "configuration: --enable-libx264 --enable-libass --enable-libfribidi"
    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, **kw):
        out = banner if cmd[0] == "ffmpeg" else "libass.so.9 => x\n\tlibharfbuzz.so.0 => y"
        return type("R", (), {"stdout": out, "returncode": 0})()

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    assert preflight.check_ffmpeg()[0] == preflight.OK
    monkeypatch.setattr(preflight, "_links_harfbuzz", lambda: False)
    status, message = preflight.check_ffmpeg()
    assert status == preflight.FAIL and "libharfbuzz" in message


def test_a_password_reset_sets_a_new_password_and_lifts_the_lockout(env):
    import pytest
    from scripts import reset_password

    c = anon()
    for _ in range(intake_limit()):
        c.post("/auth/login", data={"email": "owner@x.io", "password": "wrong"})
    assert c.post("/auth/login", data={"email": "owner@x.io", "password": PW}).status_code == 429
    with pytest.raises(ValueError):
        reset_password.reset_password("owner@x.io", "short")
    with pytest.raises(RuntimeError):
        reset_password.reset_password("nobody@x.io", "a-good-new-password")
    reset_password.reset_password("Owner@X.io", "a-good-new-password")
    assert c.post("/auth/login", data={"email": "owner@x.io", "password": PW}).status_code == 401              # the old password is gone
    assert c.post("/auth/login", data={"email": "owner@x.io", "password": "a-good-new-password"}).status_code == 303
