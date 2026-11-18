# Vendor notes

Differences between the Development Guide and the vendors' own documentation, read on 17 Nov 2026.
The vendor page wins (guide rule 2). Config keys mentioned live in `app/config_defaults.py`.

## Changes to the guide
| Area | Guide said | Docs say | What we do |
|---|---|---|---|
| OpenAI image size | 1080x1920 (UNVERIFIED) | Custom sizes must have both sides divisible by 16; 1080 is not | `image_size_mode = scale_1152x2048`: generate 1152x2048 (exact 9:16), Lanczos-scale to 1080x1920. Fallback `crop_1024x1536`. |
| OpenAI image edits | model implied | The edits endpoint defaults to `gpt-image-2.5-sunburst` | Always send `model='gpt-image-2.5-flare'` |
| ElevenLabs v3 price | $0.10 / 1K chars | $0.08 / 1K chars | `price_book.speech_per_1k_chars = 0.08` |
| ElevenLabs music | `model_id music_v2` | API default is deprecated `music_v1`; `force_instrumental` exists; `seed` is not allowed with `prompt` | Always send `model_id`; send `force_instrumental=true` |
| ElevenLabs sound effects | not specified | Default `eleven_text_to_sound_v2` is deprecated; v3 supports `loop` | `sfx_model = eleven_text_to_sound_v3` |
| ElevenLabs voice design | read the reference | `POST /v1/text-to-voice/design` then `POST /v1/text-to-voice`; preview field is `audio_base_64` | |
| Anthropic structured output | `.with_structured_output(Model)` | Opus/Sonnet 5.5 reject forced `tool_choice` | `with_structured_output(Model, method="json_schema")` |
| Postgres connection | any URI | Supabase transaction pooler (6543) breaks prepared statements; direct host is IPv6-only | Session pooler on 5432; `prepare_threshold=None`; checkpointer connection uses autocommit + dict rows |
| fal queue | status IN_QUEUE / IN_PROGRESS / COMPLETED | COMPLETED also reports failures (`error`, `error_type`); webhooks may carry `payload_error` | Treat any error field as PermanentError |
| fal defaults | | Seedance defaults to 16:9 and audio on; Kling v3 defaults to audio on | Always send `aspect_ratio` and `generate_audio` |
| OpenCV | `opencv-python`, YuNet | OpenCV 5 needs `face_detection_yunet_2026may.onnx`; YuNet is MIT, SFace is Apache-2.0 | `opencv-python-headless` (no GUI libs in Docker); model files committed in `models/` |
| R2 / boto3 | | Newer boto3 may send checksum headers R2 rejects (community report, unofficial) | `Config(request_checksum_calculation='when_required', response_checksum_validation='when_required')` |
| SyncNet | | Code is MIT; licence of the pretrained weights is not stated | Ship `NullScorer` until the owner clears the weights (guide 14) |

## Model files (`models/`)
- `face_detection_yunet_2026may.onnx` sha256 `ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0` (MIT)
- `face_recognition_sface_2021dec.onnx` sha256 `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` (Apache-2.0)
- Fetched from `https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/...` (the plain `raw` URL returns a Git LFS pointer).

## Preflight notes
- fal has no documented key-check call. Preflight asks for the status of an unknown request id on `queue.fal.run`: 401/403 means a bad key, 404 means the key works. Nothing is generated.
- The shape of the ElevenLabs `/v1/models` language list was not in the docs read; preflight searches the `eleven_v3` entry for "bengali" or `'bn'`. Confirm the real shape on the first real call.

## First real calls (Phase 0, 17 Nov 2026)
- fal key accepted (404 for an unknown request id).
- `gpt-6-luna` answered through `init_chat_model("openai:gpt-6-luna")`.
- OpenAI embeddings request returned HTTP 429 "no credits remaining" on the key in use.

## Phase 3: clients built, and what the first real calls showed (17 Nov 2026)
Clients: `app/vendors/{llm,images,eleven,falmedia,falwebhook,storage,errors,fixtures}.py`, webhook route
`app/web/routes/webhooks.py`, smoke script `scripts/smoke_vendors.py`. All have dry-run branches and tests.

Real calls made:
| Call | Result |
|---|---|
| `gpt-6-luna` through `init_chat_model("openai:gpt-6-luna")`, plain text | Works |
| `gpt-6-luna` with `.with_structured_output(Model, method="json_schema", include_raw=True)` | Works; `usage_metadata` gives token counts used for the cost log |
| OpenAI embeddings and images | **Blocked: HTTP 429 "You have no credits remaining"** on the key in use. Mapped to PermanentError (not retried) |
| fal queue, Seedance 4 s 1080p silent | **Blocked: HTTP 403 "User is locked. Reason: Exhausted balance"**. Key is accepted; the account needs a top-up |
| Cloudflare R2 | First attempt failed at the egress proxy (TLS handshake). Later the same day, with `R2_BUCKET` set, **it works**: put, presigned GET (6 h, bytes match), get_to_file, delete, through `app/vendors/storage.py` and the preflight |
| Anthropic, ElevenLabs | No key in this session |

Still to record once the accounts allow it (guide section 13): output resolution and frame rate of Seedance, Kling V3,
Kling Avatar and Kling LipSync; whether fal accepts base64 data URIs for `image_url` / `audio_url` (not stated in the docs
read; the smoke script tries it); whether Kling LipSync accepts a Seedance 1080p clip (stop condition, guide 14); Bengali on
`eleven_v3`; the `/v1/models` language list shape; whether image requests at `1152x2048` are accepted.

## Webhook behaviour
- Verified with locally generated Ed25519 keys: bad signature, changed body, stale or future timestamp (over 300 s), and each
  missing header give 401; repeated deliveries change nothing; unknown request ids give 404 so fal retries (the row may not be
  saved yet; the poller is the backstop); keys are cached for 24 hours and refetched once on a failed verification (rotation).
- Not yet exercised: a real delivery from fal. It needs a public HTTPS `PUBLIC_BASE_URL`; the sandbox has none.

## Other decisions
- `R2_ENDPOINT` (full URL) is accepted instead of `R2_ACCOUNT_ID`.
- ElevenLabs voice design uses `eleven_multilingual_ttv_v2` (the API default) via config key `voice_design_model`; compare with
  `eleven_ttv_v3` in the voice test.
- `price_book` gained `sfx_per_minute` (0.12) and `stt_per_hour` (0.20) from the ElevenLabs pricing page; voice design has no
  price in the docs read and is logged at 0.

## Phase 4 (library) decisions
- **Duplicates.** `sources.file_sha256` is unique, so a skipped duplicate is stored as `<sha>:dup:<id>` with status `skipped_duplicate` and a note naming the original. A failed source releases its hash (`<sha>:failed:<id>`) so the same file can be uploaded again; unsupported or oversized files are stored as `<sha>:rejected:<id>`.
- **Chunk size.** The guide says 3,000 words. Bangla needs many more embedding tokens per word, which would exceed the 8,192-token input limit, so `chunk_words` is `{"en": 3000, "bn": 1000}`. A chapter longer than the limit is split further. A last-resort character cap also applies before embedding.
- **Analysis.** Two steps so long books fit: per-part notes and name lists (`ingest_group_words`), then pattern cards, plot summary and source card from the notes. Code rejects cards that are not 5 to 15, run over 80 words, contain a source name or term, or share 8 identical consecutive words with the source; the model gets one retry with the problems listed.
- **Bangla matching.** Python's `\w` does not treat Bangla vowel signs as word characters; matching text is normalised by Unicode category so words stay whole. Latin terms match whole words only; Bangla terms match as substrings because suffixes attach to names.
- **Alerts.** `action_started` is sent when a batch is accepted; `action_finished` (with ready, skipped and failed counts) once when its last source finishes, even if several finish at once.
- **Worker hook.** A handler may define `on_failure(job, error)`; the worker calls it after a job fails for good, so a source never stays "processing".
- **Real calls so far:** the writer model analysis (below). Embeddings are still dry-run only (OpenAI has no credit).

## Phase 5 (graph skeleton) decisions
- **Waiting and retrying.** A step is `enq_X` plus `wait_X`. A failed job is retried as a new attempt (up to `max_attempts`) in a new wait group (`...:a2`), so earlier failures never count against the retry. A repeat of the same request would be answered from the ledger, so every redo loop changes its request (`try`, `rev`, `attempt`).
- **Stale resumes.** The edge after a wait checks the resume's group against `series.waiting_group` and pauses again on a mismatch, so a stray or late resume cannot skip a step.
- **Routes come from the database.** Check nodes (`validate_*`, `likeness_check`, `pack_check`, `keyframe_check`, `plan_repairs`) make their changes atomically and the edge after them decides from what is in the database ("does any episode still need a shot list?"), so a node re-run after a crash cannot leave a stale verdict.
- **Revisions.** `rev` in a request is the number of rejections or feedback rounds at a gate; an approval never changes it, so finished steps stay finished.
- **Isolated shot failures.** Per-shot steps (keyframes, requests, clips, lip-sync) that run out of attempts mark the shot `needs_human` and the batch carries on; other steps stop the series with an alert. An episode with a flagged shot is not compiled and is listed at the gate.
- **Runner recovery.** A command left `running` by a dead runner is requeued once its advisory lock is free. The runner then reads the checkpoint: a pending pause gets the resume, a run cut off mid-way carries on, a finished run is ignored.
- **Added steps** the guide implies but does not list: `enq_placement` (the placement plan needs the beat sheet first), `enq_emotion_groups` (before the pack images), and `enq_audio_tags` / `enq_tts` (the guide's `enq_dialogue`: tags first, then speech).
- **Notes** the graph keeps (reviewer notes, validation errors, retry counts) are rows in `audit_log` with action `note:<key>`; the schema has no other place for them.
- **Dry-run stubs** (`app/jobs/handlers/dryrun_stubs.py`) fake every kind that has no real handler. They register only when `DRY_RUN=1`. A stub production uses `dryrun_episode_count` episodes and ten shots per episode (five speaking, three lines each) so the real checks (8 to 12 shots, runtime 75 to 90 s) pass honestly.
- **Not done yet, by design:** the per-failure repair table (phase 9: `plan_repairs` resets any failed shot to a fresh keyframe for now), the full feedback flow and its pages (phase 10: `apply_feedback` handles the categories but has no route), the script redo when runtime is outside 75 to 90 s (phase 8: it is recorded as a failed `runtime` check and shown at the gate).
- **Face module** (`app/qa/face.py`) verified on a real photo with OpenCV 5: one face found, 128-number vector, a downscaled copy still matches at 0.96. The photo is not in the repo; tests replace face detection.

## Real Anthropic calls (17 Nov 2026)
- Preflight: `claude-opus-5-5`, `claude-sonnet-5-5` and `claude-haiku-5-5` all answered with the key from `MICRODRAMA_ANTHROPIC_API_KEY`.
- Source analysis on a 3-chapter original story, role `writer` (Sonnet 5.5): two calls, 22 s, $0.027. `with_structured_output(..., method="json_schema", include_raw=True)` returned valid `GroupAnalysis` and `SourceAnalysis` objects first time; the code checks passed (10 pattern cards, none naming a character or place, 179-word plot summary). Names came back with Latin and Bangla forms (for example Kabir / কবীর). First names and full names are listed as separate blocklist terms.

## Open-source image backend for tests (`IMAGE_BACKEND=local_sd`)
Added because the OpenAI image account has no credit. **Tests and development only; never the production default.**
- **Model:** `stabilityai/sd-turbo` through `diffusers`, CPU, one step. Licence: Stability AI Community License (free for research, non-commercial and limited commercial use; commercial use needs registration with Stability and has a revenue ceiling). Install with `pip install -r requirements-local-images.txt` (not in the Docker image). The model (about 2.5 GB) downloads from Hugging Face on first use.
- **Speed on 4 CPU cores:** about 25 s to load once per process, then about 13 to 20 s per image. Memory about 5 GB.
- **1080x1920:** the model is trained on squares and a tall canvas stacks duplicate faces (the detector counted two). It generates at 512x512 and the picture is placed full-width in the middle of a 9:16 canvas over a blurred, enlarged copy of itself.
- **References:** the first reference (its middle square) is the starting point of an image-to-image run. This keeps the person roughly the same; it is not identity guidance. Only one reference is used.
- **Switch:** `IMAGE_BACKEND=local_sd` also applies under `DRY_RUN=1` (it costs nothing). The tests clear it, so they always use fixtures.
- **Embeddings are not covered** (OpenAI embeddings still need credit).

First real runs (17 Nov 2026), through `images.generate_image`:
| Comparison (SFace cosine) | Score | Rule |
|---|---|---|
| master vs an image-to-image variation of her | 0.57 | pack match needs at least 0.40: passes |
| master vs a real reference photo | 0.22 | likeness needs under 0.30: passes |
| master vs a different generated person (older man) | **0.35** | face match needs at least 0.36 |

The last row matters: two different people can score about as high as the keyframe face-match threshold, so that check separates characters only weakly on stylised faces. Calibrate `qa_thresholds` on the pilot (the guide already asks for this) and consider a margin between "same person" and "other characters in the cast" rather than a single floor.

## Phase 6 (story stage) decisions
- **Real handlers** for `llm_bible`, `llm_beats`, `llm_placement_plan`, `llm_script`, `llm_continuity`, `llm_wardrobe`, `llm_originality` (`app/jobs/handlers/story_handlers.py`, logic in `app/stages/story.py` and `originality.py`). In DRY_RUN the same code runs: `llm.complete(fixture=...)` returns a schema-valid fixture (`story_fixtures.py`) and the real validators and database writes still execute. The Phase 5 stand-ins for these kinds are no longer used.
- **Free-form dicts return empty from Claude.** Tested for real: a `dict[str, str]` field (`EpisodeBeat.handoff`) is accepted by the API but comes back `{}`. The model is asked for a list of `{code, state}` (and `{episode, scene}` for the wardrobe) and the result is converted to the guide's shapes.
- **Output limits.** `llm_max_tokens` (config) sets a limit per role. ChatAnthropic's own default is 128,000 tokens, too high for a non-streaming call.
- **Prompt caching.** Stage instructions, Story Bible JSON and series summary form one identical system prefix (marked cacheable); the per-call material and reviewer notes go in the user message (guide 9.2 order).
- **Beats in chunks of 15 episodes**, each told the last four beats. A chunk is retried up to three times with the errors fed back; a model that stops early keeps its valid start and is asked only for the remainder (seen for real: it returned 12 of 15). The graph's own check still validates the whole sheet and redoes the step at most twice.
- **Retries show the model what went wrong.** A failed attempt's error is stored on the job; the next attempt's prompt lists it ("Your previous attempt was rejected for these problems"). Gate 2 reviewer notes are added too.
- **Ranges are config:** `episode_count_range` [45, 50]; tests lower the minimum to keep runs fast.
- **Originality** (`app/stages/originality.py`): blocklist (every script line, action and beat); wording (8 identical words in a row, whole corpus streamed once, same language); similarity (nearest chunk by cosine, over `originality_cosine_max`, same language, pinned lore version, own story included); plot (the model, any language, top five sources). Failing episodes get ONE rewrite inside the originality job; `same_story` is only reported. The report is a `qa_results` row (`check_name = 'originality'`) shown on the review page.
- **Gate 2 page:** `GET /productions/{id}/review/story` (logline, cast, sets, beat sheet, scripts, wardrobe, originality, placement plan; approve and reject with a redo target for deciders at the gate).
- **Open-source embeddings for tests** (`EMBEDDING_BACKEND=local`): all-MiniLM-L6-v2, 384 numbers zero-padded to 1536 (padding keeps cosine similarity). English only; vectors must not be mixed with OpenAI ones in one database. Tests clear it.

## Real story-stage run (17 Nov 2026, Claude Sonnet 5.5, Bangla, `scripts/smoke_story.py`)
A library source (short English story, analysed by the model) and a new Bangla series, run through the real handlers with local embeddings.
| Stage | Time | Cost | Result |
|---|---|---|---|
| Library source analysis | n/a | $0.039 | 3 chunks, 11 pattern cards, 12 blocklist terms |
| Story bible | 62 s | $0.073 | passed first time: 45 episodes, 8 characters, 7 sets |
| Beat sheet (45 episodes, 3 chunks) | **632 s** | **$0.891** | valid; one chunk first came back 3 episodes short and was handled per chunk |
| Script, episode 1 | 62 s | $0.056 | attempt 1 estimated at 60 s (rejected), attempt 2 passed with the error shown to the model |
| Script, episode 2 | 35 s | $0.064 | passed first time (Bangla lines, 20 words or fewer) |
| Continuity (2 scripts) | 11 s | $0.027 | |
| Wardrobe | 11 s | $0.032 | scene coverage validated |
| Originality | 10 s | $0.012 | passed; the plot comparison noted shared motifs and judged it a different story |

- **Cost and time of a whole story stage** extrapolate to roughly $4 to $5 (bible 0.07, beats 0.9, 45 scripts at about 0.06 each, then continuity, wardrobe, originality over all scripts) and 25 to 40 minutes, mostly the beat sheet and scripts. Costs are computed from token counts without the prompt-cache discount, so they overstate slightly. The guide's batch API would roughly halve the script cost (phase 13).
- Everything the model returned passed through the real validators and the same persistence code as the dry run.
- A script's own length estimate often lands just outside 75 to 90 s; the retry with the error shown fixes it. Expect about one script in a few to need a second attempt.

## Phase 7 (look stage) decisions
- **Real handlers** (`app/jobs/handlers/look_handlers.py`, logic in `app/stages/look.py`): trait sheet (vision, role writer), voice profile (writer), emotion groups and line tags (role copy, `gpt-6-luna`), master portrait, pack images, set plates, ambience and voice design. Dry run executes the same code on fixtures; a dry-run master with no detectable face uses a deterministic stand-in vector so the likeness loop is still exercised.
- **Reference order for masters:** the reference photo goes first on tries 1 and 2 and is left out on try 3 (guide 8.5/9.5 read together: twice at most, the second regeneration without the photo). With the OpenAI edits endpoint order is irrelevant; the local backend uses only the first image.
- **Review images** are the clean image plus a Pillow caption bar (name and tags); clean images carry no text. Both are stored (`_review.png`, `_clean.png`) and tested pixel for pixel.
- **Media route** `GET /media/{series_id}/{path}`: login required, any active member may view; it redirects to a one-hour presigned R2 URL (or serves the local test store) and refuses `..` paths and unknown series.
- **Gate 3 page** `GET /productions/{id}/review/look`: reference photo beside each master with the likeness score (flagged if it stayed too similar), pack images, voice sample player with the sample line, set plates and ambience, and a "redo" checkbox per asset. Approving posts the current voice of each speaking character; ticked assets plus "reject" redo exactly those.
- **Image media type:** the model wrapper now labels images with their real type (found by the first bytes); Claude rejected a JPEG labelled PNG in the first real vision call. Intake converts references to PNG, so the pipeline itself was unaffected.
- **Open-source stand-ins** are documented separately in `docs/OPEN_SOURCE_TEST_SUBSTITUTES.md`.

### Real look-stage run (17 Nov 2026; Claude and gpt-6-luna real, images and voices from the open-source backends)
| Step | Time | Cost | Result |
|---|---|---|---|
| Trait sheet (Claude, from a real photo) | 7 s | $0.004 | a faithful 55-word description; no names |
| Voice profile x2 (Claude) | 5 s each | $0.005 each | valid, distinct descriptions |
| Emotion groups x2 (`gpt-6-luna`) | 2 to 4 s | $0.0001 | every emotion grouped (for example "defiant" under anger) |
| Master portraits x2 (local SD) | 14 to 23 s | free | one face each; Lead 1 vs the photo scored 0.36 (limit 0.30: it would be regenerated) |
| Pack images (turnaround, expression, wardrobe) | 19 s each | free | match to master 0.63, 0.66, 0.74 (floor 0.40) |
| Line-up, set plate, ambience, voice design x2 | 4 to 19 s | free | all written; review bars show the right names and tags |
Whole stage about 3 minutes and $0.015 for this slice. With the real image and voice APIs the cost is dominated by images (about 25 per lead character).

### Phase 7 adjustment: sheets follow the plan, limited backends skip sheets
- `look.pack_prompt`: turnaround, wardrobe and pose are full body head to toe; expression is a tight face close-up; all reference the prime portrait.
- `images.unsupported_pack_kinds()` (env `SKIP_PACK_KINDS`, `local_sd` default list) filters `work.pack_items`; `pack_check` writes `note:pack_skipped`; Gate 3 page shows the notice.
- Intake rejects `bn` when `VOICE_BACKEND=local`.

## Phase 8 decisions (build one episode)
- Shot list: role `director`, schemas in `app/models/shots.py` (`est_seconds` added per guide 10). The 30 nearest camera setups are retrieved by embedding and exclude any that avoid `seedance_1_5_pro` (the likely tier A/B2 model). New setups are inserted once, with an embedding, origin series and genre. `emotion_group` is added to the stored spec from the speaker's `characters.emotion_groups` map (falls back to `calm`) so the keyframe cache key and the expression reference can use it.
- Wardrobe check: a shot's outfit must exist for that character and, where the wardrobe plan covers (episode, scene) for them, must equal the planned outfit.
- Keyframe references: master portrait, the outfit image, the speaker's expression image for the emotion group, the set plate. A kind that does not exist (sheets skipped by a limited image backend) is left out, so the master portrait alone is enough. The face pre-check (`keyframe_face`, threshold = `face_match_min` 0.36) is recorded as a precheck row; it only fails the job when the image backend can hold an identity (not `local_sd`). A shot with no detectable face passes with `{no_face: true}`. One internal retry, then PermanentError (the graph retries as a new attempt).
- Shot request: role `compiler`; the prompt rules are checked in code (ending phrases, line text present, no character names or codes, no brand name, no on-screen-text words, one sentence for B1, at most 1,500 characters). Spoken line text is exempt from the name check. The ids of the learned rules used are returned in the job result as `rules_used` for Phase 9 to credit.
- Shot audio: B1 builds it at video submit with T = planned; B2 and C build it at lip-sync submit with T = the probed length of the raw clip (equals render_duration in practice, and always equals the video). The compile rebuilds it locally with T = max(planned, 2 s) and trims to planned.
- Durations: a B1 shot with dialogue is stretched to at least 2.0 s, because the Kling audio limit is 2 s (guide 10.3 asserts it) and a 1.2 s line plus pads is only 1.8 s. This is the one deviation from guide 10.2.
- Compile: per-shot video is trimmed and scaled/padded as in 10.6, and a clip shorter than planned holds its last frame (`tpad`) so audio and video lengths stay equal. Scripted sound effects are not generated yet (`sfx_effect` is still a stub), so speaking shots get speech plus ambience only.
- Captions: the guide's "offset of the line inside the shot audio" already includes pad_head, so it is not added twice. When the respelled text and the original have different word counts, word timings are mapped by position. Frame-grab test confirms Bangla conjuncts render with Noto Sans Bengali.
- `FAST_COMPILE=1` (set by `tests/conftest.py`, honoured only under DRY_RUN) makes graph tests use the quick stub compile; `tests/test_phase8.py` turns it off and runs the real compile. `FFMPEG_PRESET` overrides the x264 preset (default medium).

## Phase 9 decisions (QA, repairs, learning)
- **Lip-sync scorer: NullScorer shipped.** The SyncNet reference code is MIT but the licence of its pretrained weights is not stated (checked at planning time), so the licence condition of guide 11.2 is not met; CPU speed was therefore not evaluated. Every speaking shot's `lipsync` check is recorded as failed with category `unscored`, never auto-repaired, and listed in the episode's `lipsync_review` row ("judged by people at the gates"). A scorer can be added with `app/qa/lipsync.set_scorer()`; LatentSync's SyncNet (Apache-2.0) is the candidate to ask the owner about.
- **QA rows are kept per render attempt** (`detail.attempts`, `detail.sync_attempts`), so a failure that was repaired stays on record and the learn step can write a rule from it; re-checking the same attempt (after a clipping repair) replaces that attempt's rows. `repair.failures()` reads the newest attempt of the newest round.
- **Repairs** (`app/qa/repair.py`, `plan_repairs`): the table in guide 11.5, earliest stage wins when several checks fail. Rounds: repairs run while `round < 3`, so at most three repair passes; the loop also ends when nothing was reset. A shot that fails with `attempts = 2` on the main model switches to the fallback route (`fallback_used`, `attempts = 3`, render duration recomputed for the fallback method); `attempts >= 3` becomes `needs_human`. A shot that is still failing when the rounds run out stays `qa_failed` and is listed at the gate. Clipping: speech gain goes down 3 dB per repair (stored as `spec.audio_gain_db`, applied by the compile), and a shot still too loud at -9 dB becomes `needs_human`.
- **Fallback routing** is `routing.method_tier()`: A -> Kling V3 (audio on), B1 -> the B2 method (Seedance silent clip + lip-sync), B2 -> Kling V3 audio off + lip-sync, C -> Seedance (+ lip-sync with speech). Requests, costs, lip-sync need and the shot request's model key all use it.
- **Dry-run:** face matching is recorded as `{dry_run: true}` (fixture clips have no faces to match) and the four frame grabs are skipped, since the fixture vision answer ignores images. Real runs extract the frames at 10/35/65/90 %.
- **Episode QA:** runtime, hook (is_hook and planned <= 4 s), cliffhanger, loudness (ebur128 integrated, -16 LUFS +-1.5), continuity (contact sheet, role `vision_qa`) and captions (every word of every line covered, no overlaps; the caption chunk end is now clamped to the next chunk's start). Episode-level failures are not auto-repaired. After the learn step all compiled episodes become `qa_passed`; flagged shots stay listed.
- **Learned rules:** `llm_rule` (role `rule_writer`, at most 25 words) -> de-duplicated by embedding (cosine >= 0.88 increments `hits`) per model key. `llm_shot_request` adds `hits` when it reads rules, returns their ids as `rule_ids`, and the learn step adds a `win` for each when the shot passed QA on its first attempt (`attempts = 0` and `sync_attempts = 0`). Retirement: `python -m scripts.retire_rules` (hits >= 20 and wins/hits < 0.5).
- **Camera library:** the guide's schema has only `uses` and `passes` per setup, so the per-model pass rate for `avoid_models` (>= 10 uses on one model key, first-attempt pass rate < 0.5) is derived from the `shots` table instead of stored. Counters are updated once per shot (`spec.learn_counted`), so running the learn step again never double counts. The first wording that passes on a model key is saved in `prompt_by_model`.

## ElevenLabs key check (18 Nov 2026)
- The saved key authenticates and has the generation permissions (dialogue, music, sound effects, voice design, voice create and speech-to-text all passed authentication and returned a body-validation 422), but it **lacks the read permissions** `models_read`, `voices_read`, `user_read` and `speech_history_read`. `scripts/preflight.py` therefore proves the key with a free 422 probe and says plainly that Bengali on `eleven_v3` is not verified.
- The account is on the **free plan**: a real speech call with a library voice returned HTTP 402 `paid_plan_required`, and voice design / saving a voice returned HTTP 403 `feature_not_available` ("Creating a voice through the API is only available on a paid plan"). **No real ElevenLabs speech, voice or (probably) music call can run until the plan is upgraded.** The Bengali stop condition of guide section 14 therefore stays unchecked. Local test voices (`VOICE_BACKEND=local`) keep working for English.

## Phase 10 decisions (decisions and money)
- **Feedback** (`app/feedback.py`): `Episode N (MM:SS-MM:SS)` plus a note, validated (compiled, start < end, end <= runtime), mapped to the shots of the episode's latest cut whose `edit_entries` strictly overlap the range (a shot that only touches the boundary is not included), classified by `llm_feedback_classify` (role copy; the dry-run fixture is keyword-based), editable and confirmed by a person, then sent with decision `feedback`. Pilot feedback is limited to `pilot_rework_rounds` (1); a recompile ("reassemble", notes `reassemble`) is not counted as a rework round. At Gate 5 there is no limit.
- **Rework** (`apply_feedback`): the guide's table. The feedback id is stored on the shot (`spec.feedback_id`) and added to every job request so ledger keys are new (a reworked shot never uses the keyframe cache). A voice change re-fits durations (episode goes back to `shots_planned`) and passes the note to the audio-tag prompt. Sound and caption rebuild only in the compile. An episode reworked at Gate 5 that belongs to the pilot batch joins the current batch (its rework spend is counted against the full budget). Items become `fixed` (with an alert to the author) when the episode passes QA again.
- **Money:** `POST /productions/{id}/budget` approves an amount (positive dollars) or declines (audit row, series stays held). The 100% / 125% logic is the Phase 2 code. The Gate 4 projection is pilot video spend per pilot second times the planned seconds of the remaining episodes (82.5 s each until measured).
- **Live updates:** `GET /events?series=id,id` polls every 2 s for new alerts and for changes of `series.updated_at` (every job status change bumps it), sending `alert` and `progress` events; pages reload on `progress` unless a form has focus. Alerts that existed when the stream opened are not replayed.
- **Pages:** dashboard (waiting strip + table), production page (stage tracker, episode grid, spend bar with marks at 100% and 125%, activity feed), episode review page (player with Mark range, QA report, feedback list, projection, continue/discard/approve, reassemble), alerts list with unread count in the header.
