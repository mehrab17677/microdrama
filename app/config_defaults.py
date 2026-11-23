"""Default rows for the `config` table (guide section 14). Seeded by scripts/seed_config.py.

Values that differ from the guide because vendor docs differ are marked VERIFIED-CHANGE
and explained in docs/VENDOR_NOTES.md.
"""

CONFIG_DEFAULTS: dict[str, tuple[object, str]] = {
    "llm_roles": (
        {
            "story": "anthropic:claude-sonnet-5-5",
            "writer": "anthropic:claude-sonnet-5-5",
            "originality": "anthropic:claude-sonnet-5-5",
            "director": "anthropic:claude-opus-5-5",
            "compiler": "anthropic:claude-sonnet-5-5",
            "vision_qa": "anthropic:claude-haiku-5-5",
            "vision_qa_strong": "anthropic:claude-sonnet-5-5",
            "product_sheet": "anthropic:claude-sonnet-5-5",    # reads the real product photos once; its own role so a demo can run it live without making the shot judges live
            "rule_writer": "anthropic:claude-sonnet-5-5",
            "copy": "anthropic:claude-haiku-5-5",     # line tags, emotion groups, feedback sorting, marketing text: no OpenAI account needed
        },
        "Role -> model string (guide 6.1)",
    ),
    "llm_max_tokens": (
        {"default": 8000, "story": 16000, "writer": 16000, "director": 16000, "originality": 2000, "compiler": 2000, "vision_qa": 2000,
         "vision_qa_strong": 2000, "product_sheet": 4000, "rule_writer": 500, "copy": 4000},
        "Output token limit per language-model role",
    ),
    "price_book": (
        {
            # USD per million tokens
            "llm": {
                "anthropic:claude-opus-5-5": {"in": 4.0, "out": 20.0},
                "anthropic:claude-sonnet-5-5": {"in": 2.0, "out": 10.0},
                # Haiku: 5x higher above 100,000 prompt tokens (in 0.50, out 2.50)
                "anthropic:claude-haiku-5-5": {"in": 0.10, "out": 0.50, "long_in": 0.50, "long_out": 2.50, "long_threshold": 100000},
                "openai:gpt-6-luna": {"in": 0.10, "out": 0.50},
            },
            "embedding_per_m": {"text-embedding-3-small": 0.02},
            "image_per_m": {"text_in": 5.0, "image_in": 8.0, "image_out": 30.0},
            # VERIFIED-CHANGE: guide said 0.10; ElevenLabs API pricing page says 0.08 per 1,000 chars
            "speech_per_1k_chars": 0.08,
            "music_per_minute": 0.15,
            "sfx_per_minute": 0.12,
            "stt_per_hour": 0.20,
            "video": {
                "kling_v3_pro": {"per_s_audio": 0.168, "per_s_silent": 0.112},
                "kling_avatar_v2_pro": {"per_audio_s": 0.115},
                "kling_lipsync": {"per_s": 0.014, "step_s": 5},
                "latentsync": {"flat": 0.20, "flat_up_to_s": 40, "per_s_over": 0.005},
                "seedance_2_0_ref": {"per_s": {"720p": 0.3024, "1080p": 0.682}, "resolution": "1080p"},
                "seedance_2_0_i2v": {"per_s": {"720p": 0.3024, "1080p": 0.682}, "resolution": "1080p"},   # fallback for tiers A, B2, C; the 1080p price is the family rate (fal shows 720p on the page): UNVERIFIED
                # Higgsfield (self-serve list prices per output second, from its estimate endpoint, 23 Nov 2026). hf_kling_v3_pro with sound is the pricing page's figure: UNVERIFIED by an invoice.
                "hf_kling_v4": {"per_s": {"720p": 0.1257, "1080p": 0.1879}, "resolution": "1080p", "draft_resolution": "720p"},
                "hf_kling_v3_pro": {"per_s_audio": 0.1443, "per_s_silent": 0.0962},     # 1080p
                "hf_kling_v3_std": {"per_s_audio": 0.1071, "per_s_silent": 0.0714},     # 720p, the pilot draft
                "hf_seedance_2_0_i2v": {"per_s": {"480p": 0.0804, "720p": 0.1735, "1080p": 0.4293}, "resolution": "1080p", "draft_resolution": "720p"},
                "hf_seedance_2_0_ref": {"per_s": {"480p": 0.0804, "720p": 0.1735, "1080p": 0.4293}, "resolution": "1080p", "draft_resolution": "1080p"},   # no input video; brand shots are never drafts
            },
        },
        "Prices used for estimates and cost_log (guide 6.1-6.4, with verified changes)",
    ),
    "vendor_concurrency": ({"fal": 8, "higgsfield": 6, "elevenlabs": 4, "openai": 4, "anthropic": 6, "local": 2},
                           "Max running+submitted jobs per vendor. Higgsfield queues extra requests itself; its own limit is 2 for a new account, 10 once $25 of credit was added in the last 28 days, 20 at $100"),
    "video_vendors": ({"kling_v4": "higgsfield", "kling_v3_pro": "higgsfield", "seedance_2_0_i2v": "higgsfield", "seedance_2_0_ref": "higgsfield"},
                      "Which vendor renders each video model: higgsfield or fal. The avatar (B1) and both lip-sync engines are fal only. Set a model to fal to move it back"),
    "video_failover": (True, "If Higgsfield has no credit, rejects the key or is down when a clip is submitted, render the clip on fal instead (Kling 4.0 becomes Kling V3 Pro there)"),
    "pilot_quality": ("draft", "draft: pilot clips render at lower quality (720p) for the Gate 4 approval, then the pilot is rendered again at full quality after approval; final: pilot at full quality"),
    "pilot_draft_resolution": ("720p", "Resolution of a draft clip (Kling 4.0 and Seedance 2.0). Lip-sync needs at least 720 px on both sides, so do not go lower than 720p"),
    "max_attempts": (3, "Attempts per failed work item"),
    "lipsync_fallback": ("latentsync", "Lip-sync engine for the first redo and after Kling refuses a clip: latentsync, or none to keep Kling only"),
    "timing": ({"pad_head": 0.25, "pad_tail": 0.35, "line_gap": 0.30}, "Seconds"),
    "qa_thresholds": (
        {
            "likeness_max": 0.30,
            "pack_match_min": 0.40,
            "face_match_min": 0.36,
            "lipsync_conf_min": 3.0,
            "lipsync_offset_max": 3,
            "clipping_max_db": -0.5,
            "loudness_target": -16,
            "loudness_tolerance": 1.5,
            "hook_max_seconds": 4.0,
        },
        "Initial values; calibrate on the pilot",
    ),
    "originality_cosine_max": (0.90, "Scene similarity limit"),
    "image_quality": ("medium", "auto|low|medium|high|xhigh|max (GPT Image models)"),
    "master_image_quality": ("high", "Quality for master (prime) portraits: they set every later image's identity, and there are only a few"),
    "image_provider": ("fal", "Where GPT Image requests go: fal or openai (Nano Banana Pro is fal only)"),
    "embedding_provider": ("fal", "Where embeddings are made: fal (OpenRouter route, text-embedding-3-small) or openai"),
    "image_models": ({"default": "gpt_image_flare", "brand_keyframe": "nano_banana_pro"},
                     "Image model per purpose (master, pack, plate, keyframe, brand_keyframe, else default): gpt_image_flare or nano_banana_pro. Ad-placement shot keyframes use Nano Banana Pro"),
    "nano_banana_resolution": ("2K", "Nano Banana Pro output tier: 1K, 2K or 4K (4K costs double); the result is scaled to 1080x1920"),
    # fal image prices in USD per image (fal pricing pages, Nov 2026). GPT Image 2.5 Flare edit/text at 1024x1536; 1152x2048 is a little larger and may cost slightly more.
    "image_prices": ({"gpt_image_flare": {"edit": {"low": 0.0131, "medium": 0.0186, "high": 0.0495}, "text": {"low": 0.0048, "medium": 0.0104, "high": 0.0413}},
                      "nano_banana_pro": {"1K": 0.15, "2K": 0.15, "4K": 0.30}}, "fal image prices per image; UNVERIFIED against an invoice"),
    # VERIFIED-CHANGE: OpenAI requires both sides to be multiples of 16, so 1080x1920 is invalid.
    # Modes: native_1080x1920 | scale_1152x2048 | crop_1024x1536
    "image_size_mode": ("scale_1152x2048", "Generate 1152x2048 (exact 9:16) and Lanczos-scale to 1080x1920"),
    "seedance_fps": (24, "UNVERIFIED: read from the first clip with ffprobe"),
    "ambience_db": (-24, "Ambience level under dialogue, dB"),
    "fal_timeout_minutes": (30, "Fail submitted jobs after this long"),
    "gate_reminder_hours": (24, "Reminder period for open gates"),
    "story_min_words": (1500, "Minimum length of a user-supplied story"),
    # Library ingestion. Bangla text uses far more embedding tokens per word, so its limits are lower.
    "chunk_words": ({"en": 3000, "bn": 1000}, "Largest source chunk (guide: 3,000 words; Bangla lower to stay inside embedding limits)"),
    "ingest_group_words": ({"en": 12000, "bn": 4000}, "Words of a source sent to the writer model per analysis call"),
    "max_upload_mb": (50, "Largest accepted source file"),
    "pilot_rework_rounds": (1, "Gate 4 feedback rounds allowed"),
    "raw_retention_days": (30, "Retention for raw and failed clips"),
    "shot_count_range": ([8, 12], "Shots per episode (guide 9.6)"),
    "episode_count_range": ([45, 50], "Episodes per series (guide 9.4). Tests lower the minimum to keep runs fast"),
    # DRY_RUN fixtures only: how big the fake productions are
    "dryrun_episode_count": (45, "Episodes in a dry-run story bible (guide: 45 to 50)"),
    "music_model": ("music_v2", "ElevenLabs music model (music_v2_5 also valid; never leave the default music_v1)"),
    # Added from vendor verification (docs/VENDOR_NOTES.md)
    "sfx_model": ("eleven_text_to_sound_v3", "Sound-effects default is deprecated; always send this"),
    "stt_model": ("scribe_v3", "ElevenLabs speech to text"),
    "voice_design_model": ("eleven_multilingual_ttv_v2", "API default; eleven_ttv_v3 is the alternative. Judge Bangla accents in the voice test"),
    "tts_model": ("eleven_v3", "ElevenLabs speech model"),
}
