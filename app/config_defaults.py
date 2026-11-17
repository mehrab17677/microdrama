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
            "rule_writer": "anthropic:claude-sonnet-5-5",
            "copy": "openai:gpt-6-luna",
        },
        "Role -> model string (guide 6.1)",
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
                "seedance_1_5_pro": {"per_m_tokens_audio": 2.40, "per_m_tokens_silent": 1.20},
                "kling_v3_pro": {"per_s_audio": 0.168, "per_s_silent": 0.112},
                "kling_avatar_v2_pro": {"per_audio_s": 0.115},
                "kling_lipsync": {"per_s": 0.014, "step_s": 5},
            },
        },
        "Prices used for estimates and cost_log (guide 6.1-6.4, with verified changes)",
    ),
    "vendor_concurrency": ({"fal": 8, "elevenlabs": 4, "openai": 4, "anthropic": 6, "local": 2}, "Max running+submitted jobs per vendor"),
    "max_attempts": (3, "Attempts per failed work item"),
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
    "image_quality": ("medium", "auto|low|medium|high|xhigh|max"),
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
    "music_model": ("music_v2", "ElevenLabs music model (music_v2_5 also valid; never leave the default music_v1)"),
    # Added from vendor verification (docs/VENDOR_NOTES.md)
    "sfx_model": ("eleven_text_to_sound_v3", "Sound-effects default is deprecated; always send this"),
    "stt_model": ("scribe_v3", "ElevenLabs speech to text"),
    "voice_design_model": ("eleven_multilingual_ttv_v2", "API default; eleven_ttv_v3 is the alternative. Judge Bangla accents in the voice test"),
    "tts_model": ("eleven_v3", "ElevenLabs speech model"),
}
