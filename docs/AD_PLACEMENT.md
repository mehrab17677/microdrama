# Ad placement: the brand appears inside the generated scene

This replaces the Phase 11 design (full-frame inserts), which behaved like an in-stream ad. An ad placement drama is a normal drama in which a real,
paid-for product appears **naturally in the story**: a character takes a Vaseline lip balm tin from her bag and applies it. The video model draws the
shot. The brand's real assets (logo, product photos) are fed to the models as references and are never restyled, regenerated or replaced.

## Honest limits (read first)

No video model guarantees pixel-exact packaging text in motion. The design therefore does three things: (1) it gives the models the real pictures and
keeps the shot easy (short, steady, label to the camera); (2) it **checks every placement shot** against the real assets and rebuilds a failing shot;
(3) it makes a **person sign off** every placement against the real assets before the final cut can be approved. A shot that cannot be made faithful
ends as `needs_human` and is never delivered silently.

## What the user provides (intake, ad placement category)

| Field | Why |
|---|---|
| Brand, product name and variant | Identity, report |
| What the product is, in a few sentences (at least 20 words) and **how it is used** | Writers and directors show real use (apply to lips, open the tin) |
| Physical notes (size, shape, colours, packaging, where the label sits) | Locked description for prompts |
| Text printed on the pack (one per line) | The vision check compares the label it reads with this list |
| Placement types | `product_in_use`, `product_shown` (hero, label to camera), `product_ambient`, `logo_in_scene`, `spoken` |
| How often, and the minimum seconds the product must be on screen per placement | Plan and exposure check |
| Never show next to (exclusions) and claims the story must not make | Exclusion and claims checks |
| Spoken wording, brand colour | Exact words for a spoken mention |
| **Assets**: logo file(s) and **real product photos**, each tagged with a view (front, back, side, top, open, in_use) | The references. At least one logo and one front product photo. Raster images, shortest side at least 800 px for product photos. Stored exactly as supplied. |

## Where the brand enters the pipeline (the "injection" layers)

1. **Story Bible and beats** (`llm_bible`, `llm_beats`, `llm_placement_plan`): the brief and the product sheet's *use* rules are context. The placement plan picks
   episodes and story moments where use is natural; it respects exclusions (existing check).
2. **Look stage**: `llm_product_sheet` reads the real product photos once and writes a **product sheet** (shape, colours, proportions, scale, material, each
   printed element and where it sits, what must never change). It is shown on the Gate 3 page with the real photos. It is the only brand text that goes into prompts.
3. **Shot list**: each planned placement becomes a *brand shot* inside a normal scene: non-speaking (tier A) or a speaking wider shot (tier B2). Close-ups for
   the avatar model and hero shots are not allowed for brand shots. Rules checked in code: the kind is in the plan, the shot is long enough to read the product
   (planned at least the minimum exposure seconds), the product action is the story action, no brand name in the action text.
4. **Keyframe** (`image_keyframe`): the character's references plus the **real product photos** (front, a second view and the logo, at most 3), with a locked
   instruction: *the product is exactly the product in the reference images: same shape, colour, proportions and label; never invent or change label text; label
   toward the camera*. The keyframe is checked against the real photos before it is used.
5. **Video request** (`llm_shot_request` + code): the compiler model writes only **one action sentence**. Code then assembles the final prompt:
   `[action] + [product lock] + [motion guard] + [no on-screen text] + [audio rule]` with `@Image` tags for the keyframe and the product photos. Nothing the
   user typed reaches the video model except through validated fields.
6. **Model**: `bytedance/seedance-2.0/reference-to-video` on fal (Seedance 2.0 is the strongest model offered for reference images): `image_urls` (up to 9, we use 4),
   1080p, 9:16, `bitrate_mode` high, audio off when a line is spoken (lip-sync follows) or on for silent shots. About $0.68 per generated second at 1080p
   (fal, Nov 2026), so a 5 s brand shot is about $3.40 per attempt; this is in the budget estimate.
7. **QA** (`qa_shot`, shot level): `brand_fidelity` with the strong vision role: the product is visible, its shape and colours match the real photos, the label
   text read matches the printed-text list, the logo is not distorted, no other brand or invented text, the product is used correctly. It also estimates the
   seconds the product is visible. Failing shots go back through the repair ladder.
8. **Repair ladder** (brand shots): redraw the keyframe and the clip with stricter motion (attempt 1 and 2), then `needs_human`. No fallback model: a different
   model would be a different, unapproved look.
9. **qa_brand** (episode level): `placement_count`, `exposure` (seconds visible against the minimum), `exclusions`, `claims` (the dialogue of the placement scene against the forbidden claims).
10. **Human brand approval**: the Gate 4 and Gate 5 pages show each placement shot as a clip next to the real assets, with the checks. Approve or reject (a
    rejection becomes a confirmed `brand` feedback item and is reworked). **Gate 5 approval is refused until every placement is approved.**
11. **Placement report**: episode, type, timestamp, shot, seconds visible, fidelity, status, approved by, for the brand (CSV and page).

## Security of the brand text

Everything the user types about the brand is untrusted data. It is length-limited and stripped of control characters, handed to models inside delimited
data blocks (never as instructions), and the compiler's output is validated in code (no brand name, no on-screen text words, one sentence). The final video
prompt is assembled by code from fixed templates.

## Not built / open items

- Compositing the real label or logo pixels onto a generated product (a repair route for a stubborn label) is not built; a person decides at the gate.
- App-screen brands (phone in hand) use the same route with app screens as references; legibility of UI text is the riskiest case and is expected to need human approval.
- Whether fal/ByteDance moderation accepts real brand logos is unknown until a real smoke test (about one 4 s 720p shot, roughly $1.20). Run it before the pilot.
- Whether a branded drama needs an advertising label is for the owner to confirm with the brand.
