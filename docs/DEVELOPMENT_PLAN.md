# Microdrama Pipeline — Development Plan

## Context
The repo `mehrab17677/microdrama` is empty (only `README.md`). Two documents define the work:
- **Micro-Drama Production Pipeline v4** — the *why*: two actions (ingest, run production), six gates (category, intake, story, look, pilot, final cut), pilot-first builds of 45–50 vertical 75–90 s episodes in Bangla or English, separate voice per character, speech-first shot timing, QA twice, ad-placement track.
- **Development Guide** — the *what*: it is the binding spec (rules in §1). Fixed names for tables, columns, statuses, job kinds and routes; every paid call goes through the job ledger; everything runs with `DRY_RUN=1` using `tests/fixtures/`; numbers live in the `config` table; build strictly in the §13 phase order, and a phase starts only when the previous phase's acceptance tests pass.

Goal: a single Python 3.12 codebase (FastAPI web + worker + runner + poller, one Postgres with pgvector, one R2 bucket) that runs a full production end to end in dry-run, and then runs paid pilots.

## Working rules (from guide §1, applied to every phase)
- Do not invent vendor parameters. Before writing each vendor client, fetch the doc URL in §6 and record any differences in `docs/VENDOR_NOTES.md`.
- Put each status list once, as a `StrEnum` in `app/models/enums.py`. No bare status strings anywhere else.
- No ORM. Use plain SQL through the helpers in `app/db.py`, and numbered migrations applied by `scripts/migrate.py`.
- Pin package versions to the latest stable release at build time and commit the lock file.
- Follow the stop conditions in §14 (Bengali missing from `eleven_v3`, both GPT Image sizes rejected, Kling output not 9:16 or below 1080 px wide, SyncNet unusable, any real-money step over $5): stop and ask the owner.
- Development branch: `claude/affectionate-goodall-r14vry`. Commit once per phase, after its tests pass.

## External dependency verification (done 17 Nov 2026 against official docs)
Every vendor and library in the guide exists and is usable. No blocker stops the build. The corrections below override the guide. Each one is recorded in `docs/VENDOR_NOTES.md` in Phase 0.

| Area | Finding | Change to the build |
|---|---|---|
| **LangGraph** 1.2.22 / checkpoint-postgres 3.2.1 | `PostgresSaver.from_conn_string`, `.setup()`, `interrupt`, `Command(resume=)` and node re-run on resume are all confirmed. | The connection must use `autocommit=True`, `row_factory=dict_row` and `prepare_threshold=None`. |
| **Supabase** | pgvector is available. `gen_random_uuid()` is built in. The direct host is IPv6-only, and the transaction pooler (6543) breaks psycopg prepared statements (known LangGraph issue). The free plan pauses after 7 idle days and is capped at 500 MB. | `DATABASE_URL` uses the **session pooler on port 5432**. A paid plan, or a keep-alive, is needed for production. |
| **Cloudflare R2** | The endpoint, `region_name='auto'` and presigned URLs of up to 7 days (we use 6 h) are confirmed. Presigning works only on the `*.r2.cloudflarestorage.com` domain. | Set botocore `Config(request_checksum_calculation='when_required', response_checksum_validation='when_required')`. Preflight tests put, presign, get and delete. |
| **Anthropic** | All 3 model IDs and all prices are confirmed. Haiku costs 5× more above 100K tokens of prompt. Opus and Sonnet 5.5 reject a forced `tool_choice`. | `llm.py` uses `with_structured_output(schema, method="json_schema")`. |
| **OpenAI images** | `gpt-image-2.5-flare` is confirmed, but the edits endpoint **defaults to a different model**. **1080×1920 is invalid** because both sides must be multiples of 16. | Always send `model` explicitly. The default `image_size_mode` becomes **`1152x2048` → Lanczos downscale to 1080×1920**, which keeps exact 9:16 with no crop; the 1024×1536 crop stays as the second fallback. |
| **OpenAI text** | `gpt-6-luna` costs $0.10/$0.50 for inputs of 272K tokens or less. `text-embedding-3-small` has 1536 dimensions and costs $0.02/M. | None. |
| **ElevenLabs** | The dialogue-with-timestamps body and response match the guide. The auth header is `xi-api-key`. **Bengali is listed for eleven_v3.** v3 costs **$0.08 per 1K characters**, not $0.10. Music, `/v1/sound-generation` and voice design all **default to deprecated models**. | Fix `price_book`. Always send `model_id`: `music_v2_5` with `force_instrumental=true`, `eleven_text_to_sound_v3` with `loop=true` for ambience, and voice design through `/v1/text-to-voice/design` then `/v1/text-to-voice` (preview field `audio_base_64`). Speech to text uses `scribe_v3`. Add `eleven_v4` as a config option for later; v3 stays the default per the guide. |
| **fal** | The four endpoints, their arguments and their prices match the guide. Webhook signing is confirmed. A queue `COMPLETED` status also covers failures (`error`, `error_type`), and webhooks can return `payload_error`. Seedance defaults to 16:9 and Kling defaults to audio on. 4xx and 422 errors can be billed. There is no batch discount. | Treat any error field as `PermanentError`. Always send `aspect_ratio` and `generate_audio` explicitly. Validate lip-sync limits locally before submitting. Preflight authenticates with `GET api.fal.ai/v1/models/pricing`, which generates nothing. Output URLs expire within minutes to an hour, so finalize promptly. The webhook must not redirect. |
| **OpenCV** 5.0.0.93 | YuNet is **MIT**, not Apache-2.0; SFace is Apache-2.0. OpenCV 5 needs `face_detection_yunet_2026may.onnx`. | Use the 2026may YuNet file and the 2021dec SFace file. Correct the licence notes. |
| **SyncNet** | The code is MIT, but **the licence of the pretrained weights is not stated**. | Ship `NullScorer` per guide §14 until the owner clears the weights. LatentSync's SyncNet (Apache-2.0) is the candidate to ask the owner about. |
| **ffmpeg / fonts** | Debian bookworm and trixie ffmpeg link libass, which pulls in harfbuzz and fribidi. `fonts-noto-core` includes Noto Sans Bengali. | The Dockerfile uses a Debian base with apt ffmpeg and fonts-noto-core. Preflight renders a Bengali conjunct. |

### Environment blockers (this cloud session, not the design)
- The network is limited: there is no host allowlist and no secrets. Only package registries and `api.anthropic.com` are reachable. `api.openai.com`, `api.elevenlabs.io`, `*.fal.run`, `api.fal.ai`, fal media hosts, R2 and the Supabase pooler are all blocked, and Postgres over raw TCP is probably not proxied either.
- **Impact:** Phases 0–12 are dry-run and are unaffected. Local Postgres with pgvector is installed from apt for the tests. Real calls (the Phase 3 smoke test, preflight against real vendors, the pilots) need either a host machine or an updated environment with those hosts allowed and the keys stored as network secrets. I'll propose that at Phase 3; it needs your approval.
- Some docs sites (docs.langchain.com, developers.cloudflare.com, fal.ai) block direct fetches from here. The "read the docs before writing the client" rule is satisfied through the Firecrawl scraper.

### Credentials and network status (set up before the build)
This session now runs on the personal environment **microdrama**, with `rest.fal.ai`, `fal.media` and `v3.fal.media` allowed.

| Vendor | Status | How the code gets it |
|---|---|---|
| OpenAI (`api.openai.com`) | **Saved** as a network secret | The proxy injects it. The code runs with `OPENAI_API_KEY=proxy-injected`. |
| fal (`queue.fal.run`) | **Saved** as a network secret (`Authorization: Key …`) | The proxy injects it, with `FAL_KEY=proxy-injected`. Preflight checks the key with a status call on a made-up request ID (401 means a bad key, 404 means a good one), so no second card for `api.fal.ai` is needed. |
| ElevenLabs (`api.elevenlabs.io`) | **Deferred**. **Reminder:** I will prompt you again for the key (header `xi-api-key`) at the start of Phase 3, before the ElevenLabs client's real calls. I'll do it again before the Phase 3 smoke test if it is still missing. | Dry-run only until then. Real ElevenLabs calls, the Bengali preflight and the voice test wait for the key. |
| Anthropic (`api.anthropic.com`) | Not injectable: this host bypasses the proxy | You add `ANTHROPIC_API_KEY` as an environment variable in the microdrama environment settings. It is read at the next session start. |
| Cloudflare R2 | Not injectable: S3 SigV4 signs inside the process | You add `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and `R2_BUCKET` as environment variables. The host `<account_id>.r2.cloudflarestorage.com` must be on the allowlist; I'll propose that once the account ID is known. |
| Supabase Postgres | Raw TCP on port 5432 is not carried by the HTTPS proxy | Tests here use a local Postgres 16 with pgvector installed from apt. The session-pooler connection to Supabase is checked by preflight on the deploy host. |

The code reads every key from environment variables (guide §1). Placeholders work wherever the proxy injects the key.

### Batch API decision
- **Anthropic Batches:** 50% off input and output. It stacks with prompt caching, and most batches finish within 1 h (24 h at most). LangChain doesn't support it, so it needs the `anthropic` SDK directly. The guide puts the saving at about $1 per drama on scripts.
- **OpenAI Batch:** 50% off, and it **covers `/v1/images/generations` and `/v1/images/edits` as well as embeddings**. Images are the larger cost: about 500 keyframes and pack images per drama.
- **fal:** no batch discount.
- **Decision:** batch is cheaper, and it fits the existing async `submit` → poller → `finalize` job pattern. Adopt it **after Phase 12** as Phase 13, behind config key `batch_mode` per job kind. Use it for bulk, non-interactive work: full-build `llm_script`, `image_pack`, full-build `image_keyframe`, and embeddings. Pilot, repair and feedback paths stay synchronous so gates aren't delayed by up to 24 h. This keeps Phases 0–12 identical to the guide.

## Phases (guide §13)

### Phase 0 — Skeleton
- Create the layout from §3: `app/{config,db,alerts,audit,budget}.py`, `app/models/`, `app/vendors/`, `app/jobs/handlers/`, `app/graphs/`, `app/stages/`, `app/media/`, `app/qa/`, `app/web/`, `migrations/`, `models/`, `scripts/`, `tests/fixtures/`, `docs/VENDOR_NOTES.md`.
- `migrations/0001_init.sql`: every table in §4 and §5, including the added columns `series.waiting_group`, `jobs.phase` and `characters.emotion_groups`, plus the indexes `users_one_admin`, `jobs_claim` and `jobs_group`, and the `vector` and `pgcrypto` extensions. `migrate.py` records applied files in `schema_migrations` and runs `PostgresSaver.setup()`.
- `app/models/enums.py`: series status and stage, episode status, shot status, job status, job kinds, tiers.
- `scripts/seed_config.py`: every key in the §14 table (`llm_roles`, `price_book`, `vendor_concurrency`, `qa_thresholds`, `timing`, and the rest). It upserts, so it is safe to re-run.
- `scripts/preflight.py`: one line per §6.6 check. In dry-run, vendor checks report "skipped (dry-run)".
- Dockerfile: Python 3.12, an ffmpeg build with libx264, libass, harfbuzz and fribidi, and the Noto Sans and Noto Sans Bengali fonts. `docker-compose.yml`: postgres (pgvector image) and web, worker ×2, runner and poller. Add `.env.example` and `requirements.txt` with its lock file.
- **Tests:** running migrations twice changes nothing; preflight passes in dry-run; one trivial pytest passes.

### Phase 1 — Login and team (§12.1–12.2)
- `app/web/auth.py`: argon2id hashing, Starlette `SessionMiddleware` (https_only, same_site=lax), and a dependency that loads the user on every request and requires `active`.
- Routes `/auth/register`, `/auth/login`, `/team`, `/team/{id}/approve` (a transaction that locks the users table and enforces the 5-seat cap) and `/team/{id}/disable` (an admin cannot disable themselves). Add `require_decider(series_id)`, `scripts/create_admin.py` and the Team page template.
- **Tests:** a pending user cannot sign in; a sixth approval is refused; a member cannot approve; a second admin insert fails; a disabled user's existing session is rejected.

### Phase 2 — Storage and jobs (§6.5, §7)
- `app/vendors/storage.py`: `put_bytes`, `put_file`, `get_to_file` and `presign_get`. A local-directory fallback is used when the R2 variables are unset.
- `app/jobs/ledger.py`: `enqueue` with the sha256 idempotency key and `ON CONFLICT DO NOTHING`.
- `app/jobs/queue.py`: `claim` respecting `vendor_concurrency`, ordered by fewest active jobs per series and then age, using `SKIP LOCKED`, with the budget check for video jobs in the submit phase.
- `app/jobs/worker.py`: the handler contract (`run`, `submit`, `finalize`), `TransientError` backoff (2, 4, 8, 16, 32 s, retried in place), and `PermanentError`.
- `app/jobs/poller.py`: checks submitted jobs older than 60 s, applies `fal_timeout_minutes`, requeues jobs locked for more than 15 minutes, and sends gate reminders.
- `app/budget.py`: the 100% and 125% logic from §7.5 and `approve_budget`.
- The runner's group-done loop, with the conditional `UPDATE` guard that inserts resume `graph_commands`. Add one fake job kind for the tests.
- **Tests:** a duplicate enqueue creates one row; two series are claimed alternately; a transient error retries in place; a job locked for 15 minutes is requeued; two runners produce exactly one resume; a video job over the limit holds the series and is not claimed.

### Phase 3 — Vendor clients (§6)
- **First step: remind the user to add the ElevenLabs key** (ask for it with the credential card). Also confirm that the Anthropic and R2 environment variables are set, then propose the R2 host for the allowlist.
- Fetch each doc URL first and log any differences in `VENDOR_NOTES.md`. This includes reading the ElevenLabs voice design, sound effects, speech-to-text and music `llms.txt` pages.
- `llm.py`: `complete()` using `init_chat_model` and `.with_structured_output`, with one retry on a validation error.
- `images.py`: generate and edit (up to 16 references), with the `image_size_mode` fallback (1024×1536, crop, then Lanczos scale to 1080×1920), plus embeddings.
- `eleven.py`: text-to-dialogue with timestamps (pronunciations replaced first, word timings parsed outside brackets), music, voice design, sound effects and speech-to-text.
- `falmedia.py`: submit, status and result for the four endpoints, plus the §6.4 price formulas.
- `POST /webhooks/fal`: read raw bytes first; check the 4 headers and the 300 s timestamp window; verify Ed25519 against the JWKS (cached for 24 h); ignore duplicate deliveries; hand off to a worker and return 200 quickly.
- Each client gets a dry-run branch that returns fixtures. Add the fixtures: schema-valid JSON per stage, PNG/MP3/MP4 samples, and a mock webhook.
- **Tests:** every client returns schema-valid data in dry-run; the webhook rejects a bad signature, a stale timestamp and a missing header, and ignores a repeated delivery.
- **Person-run, under $2:** the real smoke calls listed in §13. Record resolution, fps and any differences.

### Phase 4 — Library (§9.3)
- `ingest_source` handler: extract text (txt, epub, pdf), chunk by chapter or every 3,000 words, embed in batches of 100, then produce pattern cards, a plot summary and a source card (role writer), plus a blocklist in Latin and Bangla script. Set `lore_version` and send the batch-complete alert.
- One genre normalisation function in Python. Library page, `/library/upload` (deduplicated by sha256 → `skipped_duplicate`) and `/library/genres?q=`.
- **Tests:** a 3-file batch ends with 1 ready, 1 skipped and 1 failed, and sends one alert; a genre is listed only once it has a ready source; "Revenge romance" and "revenge-romance" resolve to one genre.

### Phase 5 — Graph skeleton (§8, §12.4–12.5)
- `app/graphs/state.py` (`ProductionState` with 4 keys) and `production.py`: every node and edge in §8.5, including `enq_story_ingest`, `llm_story_check`, `prepare_gate1` and `gate1` for user stories. Use `make_wait` and `make_gate` (pause nodes contain only `interrupt()`). Enqueue nodes select work by status.
- `runner.py`: `PostgresSaver`, a per-series advisory lock, and commands processed in `created_at` order.
- All handlers stubbed to fixtures. New production page and `POST /productions` (every validation problem returned at once, one-transaction create). Gate routes with the `FOR UPDATE` / 409 logic. `close_discarded`.
- **Tests:** a dry-run production goes from start to `done` with a test client approving each gate; killing the runner mid-run loses nothing; deciding a gate twice returns 409; no idempotency key runs twice; a Gate 4 discard cancels queued jobs; an incomplete brief lists every problem.

### Phase 6 — Story (§9.1–9.4)
- Pydantic schemas in `app/models/`. Prompt assembly order from §9.2.
- Real handlers for `llm_bible`, `llm_beats`, `llm_placement_plan`, `llm_script`, `llm_continuity`, `llm_wardrobe` and `llm_originality` (blocklist, 8-word runs, cosine over `originality_cosine_max`, LLM plot check). `validate_beats` allows two redos at most.
- Review: story page.
- **Tests:** validation failures redo at most twice; a blocklist term fails originality; a Gate 2 rejection with target scripts redoes only the scripts.

### Phase 7 — Look (§9.5, §11.1)
- `qa/face.py`: YuNet and SFace (read the OpenCV docs for the constructor arguments), with the model files in `models/`.
- Handlers for trait sheets, voice profiles, emotion grouping, `image_master` (the reference photo is dropped on the 3rd attempt), `image_pack` (counts per the §9.5 table), set plates, ambience and `voice_design`. Pillow draws the caption bar on review images.
- Nodes `likeness_check`, `pack_check` and `lock_look`. Review: look page; approval requires a voice for every speaking character.
- **Tests:** a master identical to its reference fails likeness and is regenerated; approval is refused while a voice is missing; review images carry the name and clean images carry no text.

### Phase 8 — Build one episode (§9.6, §10)
- `media/routing.py`, `durations.py` (padding, tier minimums, runtime scaling to 75–90 s), `requests.py` (`*_key` mapped to presigned URLs, `stable_seed`), and `ffmpeg.py`: `build_shot_audio` (asserting the lip-sync limits), compile (trim, scale and pad, 24 fps, ambience mix, loudnorm, ducked music, burned-in ASS captions, `edit_entries`) and inserts.
- `subtitles.py`: chunks of 4 words for bn and 5 for en, timing from the alignment with an even-spread fallback.
- Handlers for `llm_shot_list` (plus the camera library read and write), `llm_audio_tag`, `tts_line`, `image_keyframe` (with the cache key), `llm_shot_request` (prompt rules plus the 5 nearest learned rules), `video_clip`, `lipsync` and `finalize` (ffprobe, plus a precheck row when the size is not 1080×1920).
- **Tests:** a dry-run episode compiles to 1080×1920 at 24 fps and 75–90 s; a 1.2 s B2 line renders at 4 s and is trimmed; shot audio length equals video length; a Bangla caption frame grab renders correctly; `edit_entries` have no gaps or overlaps.

### Phase 9 — QA and learning (§11)
- `qa/lipsync.py`: the `LipSyncScorer` protocol. Check SyncNet's licence and CPU speed; if either fails, ship `NullScorer` and report it.
- `qa/vision.py`, `qa/episode.py` (runtime, hook, cliffhanger, loudness, contact-sheet continuity, captions) and `qa/repair.py` (the §11.5 action table and the fallback routes).
- `plan_repairs` (at most round 2) and `learn`: `llm_rule` with dedup at cosine ≥ 0.88, hits and wins counters, camera library counters and `avoid_models`, and a weekly retire script.
- **Tests:** a wrong-face clip resets the shot to `planned`; a B2 lip-sync failure redoes only the lip-sync; a shot reaches `needs_human` after 3 attempts; a near-duplicate rule increments hits instead of inserting; the loop ends after round 2.

### Phase 10 — Decisions and money (§7.5, §12.5–12.8)
- Feedback parsing, mapping through `edit_entries`, classification, confirm and send, and `apply_feedback` by category. A limit on pilot rework rounds.
- `alerts.notify` and every alert kind. A `/events` SSE stream that polls every 2 s. The dashboard, production page (stage tracker, episode grid, spend bar), review episodes page with the "Mark range" player, Gate 4 projection, and the budget approve route.
- **Tests:** the 100% alert fires once; reaching 125% holds the series and an approval releases it; Episode 5 (00:14-00:20) maps to exactly the overlapping shots; a second rework round is refused; a progress event arrives within 3 s.

### Phase 11 — Ad placement
- Validate the brand brief and brand assets at intake.
- `ffmpeg_insert` full-frame inserts in two variants:
  - **Phone:** each `app_screen` asset in order, on the brand-colour background, with hard cuts.
  - **Product:** each `product` asset, scaled to fit 1080×1920 on the brand-colour background, held for `planned / n` seconds each, with an optional slow zoom like the set insert. Assets are reproduced unchanged.
- `qa_brand` (placement count, asset intact, exclusions) and the placement report.
- **Tests:** an ad production cannot start without a brand asset; a pixel comparison shows the inserted phone screens **and product images** unchanged; an excluded scene fails exclusions.

### Phase 12 — Delivery and hardening
- `ffmpeg_master` (concat with stream copy), `llm_marketing`, the Delivery page, a retention script (`raw_retention_days`) and structured logs that never include request headers.
- **Tests:** 3 concurrent dry-run productions finish with no cross-talk; killing a worker mid-job leaves nothing stuck; retention deletes only raw and failed clips past the limit.

### Phase 13 — Batch mode (owner-approved addition)
- Config key `batch_mode`: `{job_kind: bool}`, off by default.
- Async handlers `submit` and `finalize` for:
  - Anthropic: `client.messages.batches.create`, then retrieve results.
  - OpenAI: upload a JSONL file, `batches.create`, download the output file.
- One batch per wait group. The poller checks batch status, and each result is fanned back to its job row by `custom_id = job.id`. Prices in `price_book` are at 50%.
- **Tests (dry-run):** a group of 3 `llm_script` jobs is sent as one batch and each job finalizes with its own result; a batch with one errored item fails only that job; with `batch_mode` off, behaviour is unchanged.

### After phase 13 — person-run paid pilots
These are a voice test (about $1), a lip-sync test (about $5) and a 3-episode Bangla pilot (about $60). Then calibrate `qa_thresholds` and compare `spent_usd` with the fal invoice.

## Ambiguities to resolve while building (decide per guide §1, or log and ask)
- §12.3 lists `/gate/{n}` for n = 1, 2, 3, 5, but Gate 0 and Gate 1 rows are created at `POST /productions`. Use the Gate 1 route only for the user-story recheck path in §12.4.
- The pipeline doc describes "phone in hand" tracking and signage compositing. The guide only specifies full-frame inserts in the first version. Build the full-frame phone and product inserts. Log in-hand tracking and signage compositing as open items.
- Batch API: decided above. Ordinary calls through Phase 12, batch mode in Phase 13.

## Verification
- `docker compose up` brings up postgres, web, worker ×2, runner and poller. Then run `scripts/migrate.py`, `seed_config.py`, `create_admin.py` and `preflight.py` with `DRY_RUN=1`.
- `DRY_RUN=1 pytest` must pass at the end of every phase, and the phase's acceptance tests are the exit criteria.
- An end-to-end dry-run production driven through the web UI, or a test client, from category to delivery, then checking the master MP4 with ffprobe (1080×1920, 24 fps) and a frame grab of a Bangla caption.
- Real-money checks (the Phase 3 smoke test and the pilots) need vendor keys and the user's go-ahead. They are not run by the agent unattended.

## Owner decisions still open (guide §14)
Hosting provider and region; image quality (medium assumed); raw clip retention (30 days assumed); batch API later.
