# Open-source stand-ins used for testing

The original plan (Development Guide section 6) uses paid vendors. While some of those accounts are not
available, **test-only** open-source backends stand in for them. Each is switched on by an environment
variable, is **off by default**, is **never in the Docker image**, and is cleared by the test suite so
automated tests always use fixtures. When you have the real accounts, set the keys, **leave the switches unset**,
and these backends are never used. Nothing here changes what production runs.

| Capability | Plan (production) | Test stand-in | Switch | Module |
|---|---|---|---|---|
| Images (portraits, packs, plates, keyframes) | OpenAI `gpt-image-2.5-flare` | SD-Turbo (Stable Diffusion, 1 step, CPU) | `IMAGE_BACKEND=local_sd` | `app/vendors/local_images.py` |
| Embeddings (retrieval, originality, similarity) | OpenAI `text-embedding-3-small` | all-MiniLM-L6-v2, zero-padded to 1536 | `EMBEDDING_BACKEND=local` | `app/vendors/local_embeddings.py` |
| Speech, voice design, ambience, music | ElevenLabs (`eleven_v3`, voice design, sound effects, music) | MMS-TTS plus pitch/tempo variation; procedural ambience and music | `VOICE_BACKEND=local` | `app/vendors/local_voice.py` |

Not stood in for (still needs the real service): video and lip-sync (fal), speech-to-text, Bangla speech.

## What to do when you get the real keys
1. Add `OPENAI_API_KEY` credit (images and embeddings), `ELEVENLABS_API_KEY`, and top up fal.
2. Unset `IMAGE_BACKEND`, `EMBEDDING_BACKEND` and `VOICE_BACKEND` wherever they were set (they are not set anywhere by default).
3. Run `scripts/preflight.py` and `scripts/smoke_vendors.py` (guide section 13, phase 3), and record the first real results in `docs/VENDOR_NOTES.md`.
4. **Re-embed the library.** Vectors from the local embedding backend and from OpenAI are in different spaces and must not be mixed in one database. Any source ingested with `EMBEDDING_BACKEND=local` must be ingested again (or use a fresh database) before real use.
5. Voices created with the local backend (`voice_id` starting `local:`) mean nothing to ElevenLabs; redo voice design at Gate 3.

## Details and known differences

### Images: SD-Turbo (`IMAGE_BACKEND=local_sd`)
- Model `stabilityai/sd-turbo` via `diffusers`; Stability AI Community License (free for research, non-commercial and limited commercial use; commercial use needs registration with Stability and has a revenue ceiling). About 2.5 GB, downloaded from Hugging Face on first use. Install with `pip install -r requirements-local-images.txt`.
- About 13 to 20 s per image on 4 CPU cores.
- **Size:** the model is trained on squares. It generates 512x512 and the result is placed full-width in the middle of a 1080x1920 canvas over a blurred, enlarged copy of itself (a tall canvas produces stacked duplicate faces).
- **References:** only the first reference is used, as the starting point of an image-to-image run. It keeps the person roughly the same; it is not identity guidance, and there is no 16-image reference set.
- **Quality:** much lower than the real model. Use it to test the pipeline (faces are detected, packs match their master), not to judge the look.
- **Prompt length:** the model reads only 77 tokens and silently drops the rest. `prepare_prompt` puts the most specific instruction (the pipeline's prompts end with it) first and trims the rest to 55 words. The OpenAI model has no such limit and gets the full prompt.
- **Little variety:** image-to-image keeps the starting picture's composition, so turnaround, expression and wardrobe images of one character look almost the same (pack match scores 0.63 to 0.74 against a 0.40 floor, which is also why they pass). A master made with the reference photo as its start stays close to the photo (likeness 0.36 against a 0.30 limit), so the graph regenerates it, the third try without the photo.
- **Measured** with real faces: master vs a variation of herself scores 0.57 (pack match needs 0.40), vs a real photo 0.22 (likeness needs under 0.30), vs a different generated person 0.35 (close to the 0.36 face-match floor, so calibrate on the pilot).

### Embeddings: MiniLM (`EMBEDDING_BACKEND=local`)
- Model `sentence-transformers/all-MiniLM-L6-v2` (Apache-2.0) on CPU; 384 numbers padded with zeros to the database's 1536. Padding keeps cosine similarity unchanged between two such vectors.
- **English only**; text is truncated to 256 word pieces, so a long chunk is represented by its start.
- Not comparable with OpenAI vectors (see step 4 above).

### Voice, ambience, music: MMS-TTS and procedural audio (`VOICE_BACKEND=local`)
- Speech: `facebook/mms-tts-eng` (VITS; CC-BY-NC 4.0, non-commercial). About 1 s to synthesise a sentence. Checked by transcribing the output with Whisper: pitch-shifted voices were transcribed word for word.
- **English only.** A Bangla series fails with a clear message ("the local test voice backend is English-only"). Bangla accuracy is not tested here.
- **"Voice design" is simulated.** There is one base voice. A description's keywords (male/female, deep/high, old/young, slow/fast) set a pitch shift and a tempo change; three candidates are offered with small variations. The voice id is `local:<semitones>:<tempo>`. Real voice design produces genuinely different voices.
- **Voices barely differ.** Two characters whose descriptions share keywords ("woman", "measured", "low") got the same voice id in the first real run (`local:-2.8:0.86`). Write clearly different descriptions to hear a difference.
- **No emotion control:** audio tags such as `[angry]` are removed before speaking, so every line is delivered in the same style.
- **No word timings:** alignment is `None`, so captions use the guide's even-spacing fallback.
- **Ambience** is filtered pink noise and **music** is a slow chord pad with a soft pulse (made with ffmpeg). Neither is generated by a model; they only let the compile and mixing steps run.
- Speech-to-text (used for the name spot-check) has no stand-in yet.
