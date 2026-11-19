# Deployment

> **Just want a staging copy for stakeholders before budget approval?** Follow [GCP_STAGING_SETUP.md](GCP_STAGING_SETUP.md): a beginner guide that sets up a small Google Cloud server in dry-run mode (no vendor keys needed), using the free trial credit.

## What you need
| Item | Notes |
|---|---|
| A Linux host with Docker and Docker Compose | 4 vCPU and 8 GB RAM is enough for two workers; compile and QA are CPU work (ffmpeg, OpenCV). Disk: small; media lives in R2. |
| A domain name pointing at the host | Caddy gets the certificate. The session cookie is `Secure`, so HTTPS is required. |
| Postgres 16 with `pgvector` | The compose file runs `pgvector/pgvector:pg16`. Or Supabase on a **paid plan** using the **session pooler, port 5432** (never the transaction pooler 6543). The free plan pauses after 7 idle days. |
| A Cloudflare R2 bucket | Keys with read, write, delete on that bucket. |
| Vendor accounts with credit | Anthropic, OpenAI (images and embeddings), fal (video), ElevenLabs **paid plan** (voice design and library voices are refused on the free plan). |
| Outbound network | `api.anthropic.com`, `api.openai.com`, `api.elevenlabs.io`, `queue.fal.run`, `rest.fal.ai`, `fal.media`, `v3.fal.media`, `v3b.fal.media`, `<account>.r2.cloudflarestorage.com`, the Postgres host. |
| Inbound | Only 80 and 443 (Caddy). `POST /webhooks/fal` must be reachable from fal (set `PUBLIC_BASE_URL`). |

## First deploy
1. `cp .env.example .env` and fill in every value. Set `DRY_RUN=0`, a long random `SESSION_SECRET` (`openssl rand -hex 32`), `PUBLIC_BASE_URL=https://<host>`, `PUBLIC_HOST=<host>`.
   Never commit `.env`. The Anthropic key may be named `MICRODRAMA_ANTHROPIC_API_KEY` where the standard name is reserved.
2. `docker compose build && docker compose run --rm migrate` (applies migrations, seeds config; safe to repeat).
3. Create the admin once: `docker compose run --rm web python -m scripts.create_admin --email you@example.com --name "Your Name"` (the password is prompted for, or read from `ADMIN_PASSWORD`; there is no page for this). The admin invites people at `/team` (five seats in total).
4. `docker compose run --rm web python scripts/preflight.py` must print `preflight passed` with no `FAIL`. It checks the database, config, ffmpeg and Bengali captions, face models,
   R2 (put, presign, get, delete), and that each vendor key authenticates. A restricted ElevenLabs key is accepted but Bengali is then reported as **not verified**.
5. `docker compose up -d`. Open `https://<host>`, sign in, and run the checks in `docs/PILOT_RUNBOOK.md` before any real production.

## Operating it
- **Logs:** JSON lines on stdout of every process (`docker compose logs -f worker`). Each line has `series_id`, `job_id`, `kind`, `duration_ms`. Credentials are scrubbed.
- **Alerts:** in the app (`/alerts`, the bell). Gate reminders, budget 100% and 125%, failed steps. There is no email or chat integration yet.
- **Money:** each batch has a video budget; at 100% an alert, at 125% the production is held until an owner approves more. Per-vendor concurrency is the config key `vendor_concurrency` (a limit of 0 pauses a vendor).
- **Config:** every number is in the `config` table (`scripts/seed_config.py` upserts defaults; edit rows in SQL). Calibrate `qa_thresholds` after the pilot.
- **Updating:** `git pull && docker compose build && docker compose run --rm migrate && docker compose up -d`. Migrations are numbered and applied once. After a release that adds config keys or prices, `migrate` re-seeds them.
- **Scaling:** `worker` replicas and `WORKER_CONCURRENCY`. The runner is one process; the web process can be run more than once.
- **Backups:** Postgres daily dump (or Supabase backups), kept off the host; R2 object versioning on. Test a restore once. The LangGraph checkpoints are in the same database.
- **Stuck work:** a dead worker's job returns to the queue after 15 minutes; a killed runner resumes from its checkpoint. A production that failed shows the step and the reason on its page.
- **Retention:** raw clips and failed takes older than `raw_retention_days` (30) go daily; finished episodes, the master and brand assets stay.
- **Users:** the admin cannot be created or changed from the web. Five failed logins for an address block it for 15 minutes.

## Security notes
HTTPS only; session cookie `Secure`, `SameSite=Lax`, holding only the user id; passwords argon2id; every request reloads the user and rejects disabled accounts; deciding routes check owner or admin;
fal webhooks are verified (Ed25519, 300 s window, replay ignored); R2 URLs are presigned and never stored; secrets are read from the environment only; request bodies are limited to 250 MB at the proxy and pictures to 25 MB;
the proxy adds HSTS, no-sniff and no-frame headers. Not built: CSRF tokens (relies on `SameSite=Lax`), per-IP rate limits, email verification, audit-log export.

## What has and has not been verified (19 Nov 2026)
**Verified by automated tests (348, all on fixtures):** every phase's acceptance tests; three productions at once; a killed worker; the full flow of ordinary and ad dramas; retention; the login throttle.
**Verified once against real services:** Anthropic (source analysis, the whole story stage in Bangla, vision in the look stage), R2 (put, presign, get, delete), Postgres, ffmpeg, Bengali caption rendering, face models, fal key and webhook signature handling (offline).
**Never run for real, so unproven:** OpenAI images and embeddings; every fal video model (Seedance 1.5 and 2.0, Kling V3, Avatar, LipSync, LatentSync); ElevenLabs speech, voice design, music and sound effects (and Bengali on `eleven_v3`, which the model list does say is supported);
the real webhook delivery; the face-match thresholds on real video; Kling output size and frame rate; whether fal accepts real brand logos and how faithful Seedance 2.0 keeps a real label. The Docker image has not been built here (no Docker daemon in the build environment); the same dependency file installs and the app boots in a clean virtualenv.
**Not built:** batch mode (Phase 13, a cost saving only), scripted sound effects (shots list `sounds` but none are generated), compositing real label pixels onto a generated product, a lip-sync scorer (people judge sync at the gates), email alerts.
