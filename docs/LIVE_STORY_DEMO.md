# Live story demo: real Claude, everything else sample

For stakeholder demos when only the Anthropic account has credit. The server stays in dry-run (`DRY_RUN=1`), but the **writing and planning roles call the real Claude models**. Images, voices, music, video, lip-sync, embeddings and the image-judging checks stay as samples, so nothing else is paid and nothing else is contacted.

## What is real and what is a sample
| Stage | In this mode |
|---|---|
| Library source analysis (pattern cards, plot summary) | **Real** (your uploaded text is sent to Anthropic) |
| Story Bible, beats, placement plan, scripts, continuity, wardrobe, originality plot check, story check | **Real** |
| Character trait sheets (reads your two lead photos; supporting characters are written from the story), voice profiles, emotion groups | **Real** (the lead photos are sent to Anthropic) |
| Shot lists (the Lead Director), audio line tags | **Real** (Opus as director, as in the guide; Haiku for tags) |
| Product sheet (ad placement drama: reads your real product photos and logo) | **Real** (the photos are sent to Anthropic) |
| Pictures (portraits, packs, plates, keyframes) | Sample picture |
| Speech, voices, music, ambience | Sample audio |
| Video clips, lip-sync | Sample clip |
| Face / image / brand judging checks (including the brand check on generated shots), shot requests, learning | Sample answers |
| Captions, edit, compile, gates, spend tracking, reviews, delivery | Real code on the sample media |

Stop point for the pitch: the story, the characters, the shot-by-shot plan and the line tags are real, so every page up to the audio stage shows genuine output. Pilot episodes then play the sample clip with the real dialogue as captions.

## Turn it on and off (on the server)
```bash
bash ~/microdrama/scripts/live_story.sh on     # asks for your Anthropic key (hidden), switches the roles, restarts, runs preflight
bash ~/microdrama/scripts/live_story.sh off    # back to a pure dry run and the guide's settings
```
- The key is typed into a hidden prompt and stored only in `~/microdrama/.env` (readable by you only). Never paste it into chat.
- `on` keeps **Opus** as the director (the guide's choice), sets the `copy` role to **Haiku** instead of GPT (no OpenAI credit needed), and plans a **6 to 8 episode** series so it finishes quickly. `off` puts `copy` back to GPT.
- An open-source model for the director is not part of this: it would need a GPU server or a hosted API plus code changes, and the small staging VM cannot run one.
- Preflight should show `OK anthropic` after `on`.

## Cost (estimate, not measured on a full run)
About **$3 to $8** for one drama through Gate 3 and the pilot shot lists (Opus shot lists for 3 pilot episodes are the largest part), depending on the length of the library text and how many redo loops the checks cause. An ad drama adds a few cents for the product sheet. Set a **monthly spend limit in the Anthropic console** (Settings → Limits) to be safe. The app's spend bar shows the real cost per run, because each call is recorded in the job ledger.

## Tips for a good demo
- Upload a **short public-domain story** (under about 5,000 words) to the library, not a full novel: every chunk group is analysed by the real model.
- Use **lead photos you may share** (male lead and female lead): they go to Anthropic for the trait sheets.
- Write 3 to 8 specific keywords and a clear aesthetic note: the real model uses them.
- English only for now (the sample voices and the test setup); Bangla works in the code but is not part of this demo.
- If a real step fails validation, the pipeline redoes it (at most twice, as designed) and the cost shows in the spend bar.
- Ad placement drama: the story, placement plan and product sheet are real. The brand check that judges generated shots stays a sample, because the pictures and clips are samples and a real judge would (rightly) reject them.
