# Pilot runbook (a person runs these; they spend money)

Stop and ask the owner if: eleven_v3 does not speak Bengali; both GPT Image sizes are refused; a Kling or Seedance output is not 9:16 or is under 1080 px wide; Kling LipSync refuses a Kling V3 or Seedance 1080p clip;
any single step would cost more than $5 outside the pilots below.

## Before any spending
- Credit: Anthropic; fal (images, embeddings, avatar, lip-sync, video failover); Higgsfield (Kling 4.0, Kling 3.0 Pro, Seedance 2.0 video; $25 or more added in the last 28 days gives 10 concurrent clips); ElevenLabs on a **paid plan**. OpenAI is not needed with the default settings.
- `DRY_RUN=0`, all keys set, `docker compose run --rm web python scripts/preflight.py` passes. Unset `IMAGE_BACKEND`, `EMBEDDING_BACKEND`, `VOICE_BACKEND`, `SKIP_PACK_KINDS`.
- Re-embed the library (`python -m scripts.reembed_library` if you ingested sources with the local embedding model: the vectors from the test model are not comparable with the real ones) and redo any voice made by the local voice backend.
- Seed the new prices and config: `python scripts/seed_config.py`.

## 1. Vendor smoke (about $2 in total)
`DRY_RUN=0 python scripts/smoke_vendors.py --out ./smoke --yes-spend --steps embeddings,image,nanobanana,kling,seedance,avatar,lipsync,latentsync,speech,music`
Record in `docs/VENDOR_NOTES.md`: output resolution and frame rate of each video model, which image size mode worked, whether Bengali speech came back with word timings, any parameter that differed.

## 2. Ad placement smoke (about $1.20, only for an ad client)
`SMOKE_PRODUCT_PHOTO=real_tin.png SMOKE_LOGO=logo.png DRY_RUN=0 python scripts/smoke_vendors.py --out ./smoke --yes-spend --steps brandshot`
Watch the clip: is the product's shape and label right? Did the request go through with a real logo? If moderation refuses real brand assets or the label is garbled every time, stop: the ad design needs the label-compositing repair that is not built yet.

## 3. Voice test (about $1)
The same 10 lines with 5 candidate Bangla voices and 5 English voices. A native listener confirms the Bangla accent and that names are pronounced right (pronunciation list).

## 4. Lip-sync test (about $5)
One Bangla line through tier B1 (avatar) and through tier B2 (a silent Kling V3 clip, then Kling LipSync). Judge sync by eye (there is no scorer). Try the LatentSync fallback on a clip that Kling handled badly.

## 5. Three-episode Bangla pilot (about $60)
Start a production, approve the gates, stop at Gate 4. Record: first-pass yield per tier, lip-sync quality for B1 and B2 separately, generated seconds per used second, cost per image, the tier mix, how often the budget prompt appeared, the projection against the real spend.
Compare the app's video spend with the Higgsfield and fal invoices (whether fal bills failed requests is unverified; Higgsfield does not charge failed or moderated clips). The pilot is a 720p draft by default (`pilot_quality`): the approval at Gate 4 renders it again at full quality, so budget for both passes (docs/HIGGSFIELD.md).

## 6. Calibrate before a full build
Set `qa_thresholds` from the pilot: `face_match_min` (different generated people can score 0.35), `pack_match_min`, `likeness_max`, `hook_max_seconds`. Check the Bangla captions on the real episodes. Decide `pilot_rework_rounds`.
Then continue to the full build only if the Gate 4 projection is acceptable.
