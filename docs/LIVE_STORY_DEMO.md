# Live story demo: real Claude, everything else sample

For stakeholder demos when only the Anthropic account has credit. The server stays in dry-run (`DRY_RUN=1`), but the **writing and planning roles call the real Claude models**. Images, voices, music, video, lip-sync, embeddings and the image-judging checks stay as samples, so nothing else is paid and nothing else is contacted.

## What is real and what is a sample
| Stage | In this mode |
|---|---|
| Library source analysis (pattern cards, plot summary) | **Real** (your uploaded text is sent to Anthropic) |
| Story Bible, beats, placement plan, scripts, continuity, wardrobe, originality plot check, story check | **Real** |
| Character trait sheets (reads your reference photo), voice profiles, emotion groups | **Real** (the photo is sent to Anthropic) |
| Shot lists (the Lead Director), audio line tags | **Real** (Sonnet as director, Haiku for tags) |
| Pictures (portraits, packs, plates, keyframes) | Sample picture |
| Speech, voices, music, ambience | Sample audio |
| Video clips, lip-sync | Sample clip |
| Face / image / brand judging checks, shot requests, learning | Sample answers |
| Captions, edit, compile, gates, spend tracking, reviews, delivery | Real code on the sample media |

Stop point for the pitch: the story, the characters, the shot-by-shot plan and the line tags are real, so every page up to the audio stage shows genuine output. Pilot episodes then play the sample clip with the real dialogue as captions.

## Turn it on and off (on the server)
```bash
bash ~/microdrama/scripts/live_story.sh on     # asks for your Anthropic key (hidden), switches the roles, restarts, runs preflight
bash ~/microdrama/scripts/live_story.sh off    # back to a pure dry run and the guide's settings
```
- The key is typed into a hidden prompt and stored only in `~/microdrama/.env` (readable by you only). Never paste it into chat.
- `on` also sets the director to **Sonnet** instead of Opus (cheaper; Opus is the guide's choice and can be restored with `off`), the `copy` role to **Haiku** instead of GPT (no OpenAI credit needed), and plans a **6 to 8 episode** series so it finishes quickly.
- An open-source model for the director is not part of this: it would need a GPU server or a hosted API plus code changes, and the small staging VM cannot run one.
- Preflight should show `OK anthropic` after `on`.

## Cost (estimate, not measured on a full run)
About **$2 to $5** for one standard drama through Gate 3 and the pilot shot lists, depending on the length of the library text and how many redo loops the checks cause. Set a **monthly spend limit in the Anthropic console** (Settings → Limits) to be safe, for example $15. The app's spend bar shows the real cost per run, because each call is recorded in the job ledger.

## Tips for a good demo
- Upload a **short public-domain story** (under about 5,000 words) to the library, not a full novel: every chunk group is analysed by the real model.
- Use a **photo you may share** as the character reference: it goes to Anthropic for the trait sheet.
- Write 3 to 8 specific keywords and a clear aesthetic note: the real model uses them.
- English only for now (the sample voices and the test setup); Bangla works in the code but is not part of this demo.
- If a real step fails validation, the pipeline redoes it (at most twice, as designed) and the cost shows in the spend bar.
- The ad placement drama also works for the text parts, but the product sheet (a vision step on your product photos) stays a sample here.
