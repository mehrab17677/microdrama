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
