# Video on Higgsfield, and the lower-quality pilot

From 23 Nov 2026 the video models for tier A, tiers B2 and C, the fallback and the brand shots run on **Higgsfield**. fal keeps the talking-head avatar (tier B1), both lip-sync engines, the images
and the embeddings. Everything is a setting: any model can go back to fal without a code change.

## Which model runs where

| Shot | Model (key) | Higgsfield endpoint | Notes |
|---|---|---|---|
| Tier A (no speech) | Kling 4.0 (`kling_v4`) | `kling-video/v4.0/image-to-video` | first frame, 3 to 30 s, 720p or 1080p. No aspect-ratio field (the keyframe decides) and no sound switch |
| Tiers B2 and C | Kling 3.0 Pro (`kling_v3_pro`) | `kling-video/v3.0/pro/image-to-video` | 1080p, 3 to 15 s, `sound` on only when nobody speaks; lip-sync follows on fal |
| Fallback "S" (after two failed attempts) | Seedance 2.0 (`seedance_2_0_i2v`) | `bytedance/seedance-2.0/image-to-video` | 4 to 15 s, 1080p, no seed field |
| Brand placement shots | Seedance 2.0 reference-to-video (`seedance_2_0_ref`) | `bytedance/seedance-2.0/reference-to-video` | up to 9 reference images, always full quality |
| Tier B1 (close-up speech) | Kling Avatar v2 Pro | fal | not offered by Higgsfield |
| Lip-sync | Kling LipSync, LatentSync | fal | not offered by Higgsfield |

The model keys name the MODEL, whichever host runs it, so learned rules and the camera library carry over when a model moves.

Settings (config table, edited like any other row; `python -m scripts.seed_config` adds the new rows to an older database, and the app falls back to the shipped defaults until then):

| Key | Default | Meaning |
|---|---|---|
| `video_vendors` | all four models on `higgsfield` | set a model to `fal` to move it back |
| `video_failover` | `true` | if Higgsfield has no credit, rejects the key or is down when a clip is submitted, the clip is rendered on fal (Kling 4.0 becomes Kling V3 Pro there) and the job row says so (`request.failed_over`) |
| `pilot_quality` | `draft` | `draft` or `final` (see below) |
| `pilot_draft_resolution` | `720p` | the draft size for Kling 4.0 and Seedance. Do not go lower: lip-sync needs 720 px on both sides |
| `vendor_concurrency.higgsfield` | 6 | Higgsfield queues extra requests itself |

A failover that costs more than the budget plan (fal is dearer than Higgsfield) trips the usual 125% hold and alerts the owner.

## The lower-quality pilot

The pilot is what the owner approves at Gate 4, and it is the stage most likely to be redone. With `pilot_quality = draft`:

1. Pilot clips render at **720p**: Kling 4.0 at 720p, Kling 3.0 **Standard** (the 720p tier of Kling 3.0) instead of Pro, Seedance at the draft resolution. Keyframes, voices, music, captions and the compile are unchanged.
   Brand placement shots and the talking-head avatar shots are always full quality: the owner must see the real product, and the avatar has no cheaper tier.
2. Gate 4 shows a note ("This pilot is a lower-quality draft") and a projection that already uses full-quality prices, plus what rendering the pilot again will cost.
3. **Continue** at Gate 4 renders every draft pilot clip again at full quality, from the same keyframe, prompt, voice-over and camera, and joins those episodes to the full batch. Their lip-sync, compile and
   QA run again, and the full-quality cut is reviewed at Gate 5. Feedback at Gate 4 re-renders only the named shots, still as drafts.

What is marked where: each clip job records `request.quality`; each shot records `spec.rendered_quality`; the raw file of a draft ends `_draft.mp4`, so a draft and its final render never share a
file or a ledger key; an audit note `draft_upgrade:<episode>` records the step. Cost rows (`cost_log`) are written for every clip with the vendor that ran it.

Draft prices are about 33% (Kling 4.0) and 26% (Kling 3.0) lower than final, and 60% lower for Seedance, so the saving is real only when the pilot is judged on story, casting and acting, not on
sharpness. If a pilot is approved without comment, the draft pass is extra cost (about the draft cost of the pilot's video). Set `pilot_quality` to `final` for a client demo.

## Credit, concurrency, limits

- A new account has **2** concurrent generations; **10** once $25 of credit was added in the last 28 days, **20** at $100, **40** at $1,000. Funds expire a year after they are added. Failed and moderated (`nsfw`) clips are not charged.
- The API does not show the balance. An empty balance returns HTTP 403 `not_enough_credits`; the app treats it as "fail over to fal". `scripts/preflight.py` checks the key (free estimate call); the smoke test checks credit.
- Inputs must be public https URLs (the app sends presigned R2 URLs; data URIs and `asset://` are rejected). Outputs are kept 7 days: the app downloads each clip to R2 at once.
- The key is `HIGGSFIELD_API_KEY=KEY_ID:KEY_SECRET` (sent as `Authorization: Key KEY_ID:KEY_SECRET`). On the server: `bash scripts/set_secret.sh HIGGSFIELD_API_KEY`.
- No webhooks: the poller (every 20 s) checks each submitted clip.

## Prices used (USD per output second, Higgsfield self-serve, checked with its free estimate endpoint on 23 Nov 2026)

Kling 4.0: 720p 0.1257, 1080p 0.1879. Kling 3.0 Pro (1080p): 0.0962 silent, 0.1443 with sound (the sound figure is from Higgsfield's price page, not yet confirmed by an invoice). Kling 3.0 Standard (720p): 0.0714 / 0.1071.
Seedance 2.0: 480p 0.0804, 720p 0.1735, 1080p 0.4293. They live in `price_book.video.hf_*`.

## Smoke test (needs credit)

```bash
DRY_RUN=0 python scripts/smoke_vendors.py --out /tmp/smoke --steps higgsfield_estimates,higgsfield_clips --yes-spend
```
`higgsfield_estimates` is free. `higgsfield_clips` makes four real clips (Kling 4.0 1080p 3 s, Kling 3.0 Pro 1080p 3 s, Kling 3.0 Standard 720p 3 s, Seedance 2.0 720p 4 s), about $1.75, and records size, frame rate and
audio. Stop and ask the owner if a full-quality clip is under 1080 px wide or not 9:16.

## Not yet verified (no credit when this was built)

- Output size and frame rate of Kling 4.0 and Kling 3.0 Pro through Higgsfield, and whether Kling 4.0 image-to-video always carries audio (the request has no switch).
- That the Kling 3.0 Standard endpoint takes the same fields as Pro, and its 720p output size.
- Whether the `@Image1` tags in the brand prompt are read by Higgsfield's Seedance 2.0 reference-to-video the way fal reads them.
- Whether Higgsfield's moderation accepts real brand logos and real faces.
- Whether Kling LipSync accepts a Higgsfield clip as it does a fal clip.
