# Lip-sync options (researched 18 Nov 2026)

The pipeline uses fal's Kling LipSync (`fal-ai/kling-video/lipsync/audio-to-video`, about $0.014 per input second, 2 to 10 s clips) for tiers B2 and C. Two separate needs follow:
**making** lip-synced video and **scoring** it (guide 11.2, currently NullScorer).

## Open-source models

| Model | Code licence | Weights / checkpoints | Commercial use | Notes |
|---|---|---|---|---|
| **LatentSync** (ByteDance) | Apache-2.0 (LICENSE file checked) | `ByteDance/LatentSync` and `LatentSync-1.6` on Hugging Face: **OpenRAIL++** (checked on the model cards) | Yes, with OpenRAIL++ use restrictions that must be passed on to users (no harm, deception, etc.) | Latent diffusion, 512x512 face crop, best close-up quality. Ships its own SyncNet (`stable_syncnet.pt`, also `latentsync_syncnet.pt`) and an eval script (`eval/eval_syncnet_acc.sh`) that computes the SyncNet sync confidence. The release's own SyncNet needs input preprocessed by its pipeline (affine transform, audio-visual adjustment). Needs a GPU to generate (roughly 8 GB or more of VRAM). |
| **MuseTalk** (Tencent Music) | MIT | "available for any purpose, even commercially" per the README | Yes | One-step latent inpainting, real-time on a GPU, 256 px face region, weaker detail than LatentSync. Depends on Whisper, ft-mse-vae, DWPose and S3FD under their own licences. |
| Wav2Lip | research only | trained on LRS2 | **No** | The public model is non-commercial. Sync Labs sells a commercial model. |

Correction to an earlier note: the SyncNet weights of LatentSync are **OpenRAIL++**, not Apache-2.0. OpenRAIL++ does allow commercial use, but it is a use-restricted licence, so the owner should read it once before it is adopted (guide section 14: do not substitute another model without asking).
The CPU speed of the SyncNet confidence check was **not measured** here (no GPU and the checkpoints are several GB).

## Hosted vendors on fal (one key, already a pipeline vendor)

| Endpoint | Price on fal (Nov 2026) | Notes |
|---|---|---|
| Kling LipSync (`fal-ai/kling-video/lipsync/audio-to-video`) | $0.014 per input second (5 s steps) | Current choice. Clips 2 to 10 s. |
| LatentSync (`fal-ai/latentsync`) | $0.20 per video up to 40 s | The open-source model above, hosted; fields `video_url`, `audio_url`, `guidance_scale`, `seed`, `loop_mode`. Cheaper than Kling for 10 s+ clips, the same cost for any clip up to 40 s. |
| Sync Lipsync 2 Pro (`fal-ai/sync-lipsync/v2`) | $5 per minute | Sync Labs; close-ups with teeth and facial detail. |
| sync-3 | $8 per minute | Sync Labs' newest. |
| VEED Lipsync v2 | $0.07 per second | UGC and social. |
| PixVerse Lipsync | $0.04 per second | Can also take text. |
| HeyGen Lipsync Precision | $0.10 per second | Captions, partial range. |

## Recommendation

1. **Keep Kling LipSync** for the pilot (cheapest, already wired and tested).
2. **Fallback lip-sync endpoint:** add `fal-ai/latentsync` (open-source model, flat $0.20) as the second choice if Kling refuses a Seedance clip (guide section 14 lists that risk). It is a small change in `app/media/requests.py` plus a price-book entry.
3. **Scorer:** the best open candidate is LatentSync's SyncNet. It needs the owner to accept OpenRAIL++ and a measurement of its CPU speed. Until then the NullScorer and human review stay.
