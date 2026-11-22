# Image models and embeddings (all on fal)

One fal account pays for images, embeddings and video. Anthropic pays for the language models. **No OpenAI account is needed** with the defaults.

## Models (config `image_models`, one per purpose)
| Key | fal endpoints | Reference images | Price per image (fal, Nov 2026) | Used for |
|---|---|---|---|---|
| `gpt_image_flare` (default) | `openai/gpt-image-2.5/flare/edit` and `/text-to-image` | 16 | edit at 1024×1536: low $0.013, **medium $0.019**, high $0.050 | master portraits (high quality), character sheets, set plates, ordinary shot keyframes |
| `nano_banana_pro` | `fal-ai/nano-banana-pro/edit` and `fal-ai/nano-banana-pro` | 14 | **$0.15** at 1K or 2K, $0.30 at 4K | **ad-placement shot keyframes (default)**, and any picture you send back from the Look gate |

Purposes: `master`, `pack` (character sheets and the line-up), `plate` (set plates), `keyframe`, `brand_keyframe` (shots with the product in them), else `default`.
Change the mapping in the `config` table (`image_models`), for example to make every keyframe Nano Banana Pro. Other keys: `image_provider` (`fal` or `openai`; Nano Banana Pro is fal only), `image_quality` (`medium`), `master_image_quality` (`high`), `nano_banana_resolution` (`2K`), `image_prices`.

How a picture is made: references (character master, outfit, expression, set plate, real product photos) go to the edit endpoint as scaled JPEG data URIs; GPT Image is asked for 1152×2048 (exact 9:16), Nano Banana Pro for `aspect_ratio 9:16` at 2K; the result is centre-cropped to 9:16 if needed and scaled to exactly 1080×1920.
The ledger records the price from `image_prices` (fal returns no cost). A 1152×2048 GPT Image edit may cost a little more than fal's 1024×1536 figure; compare with the first invoice.

## Look gate: redo pictures with the model you choose
On the Look review page tick any master portrait, character-sheet picture, cast line-up or set plate, write what to change, choose **GPT Image 2.5 Flare** or **Nano Banana Pro**, and press *Redo the ticked pictures*.
- The model receives your note **and the rejected picture** (as the last reference) and is told to change only what the note asks.
- A redone master also redoes that character's sheets and the line-up, because they are drawn from it.
- Ticked voices are designed again.
- The note, the model and the rejected picture are kept with the redo job, so a later automatic retry of the same picture keeps them.

## Embeddings
`embedding_provider` = `fal`: endpoint `openrouter/router/openai/v1/embeddings`, model `openai/text-embedding-3-small`, 1536 numbers (the same vectors as OpenAI direct, so nothing stored changes). A response with any other size is refused. Cost is read from the response (`usage.cost`), or priced at $0.02 per million tokens if fal returns none.

## Not verified yet (no fal credit when this was built)
- Image quality and identity hold of each model on real faces and real labels. Run `scripts/smoke_vendors.py --steps embeddings,image,nanobanana` (about $0.40) and compare in fal's playground.
- Whether either model accepts your lead photos and real brand logos (content checks).
- The exact size Nano Banana Pro returns at 2K for 9:16 (the smoke step records it).
- fal's embedding route: batch limits and rate limits (requests are batched in groups of 64).
