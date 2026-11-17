"""Every status / kind list, defined once (guide section 5). Never write these as bare strings elsewhere."""
from enum import StrEnum


class UserRole(StrEnum):
    admin = "admin"
    member = "member"


class UserStatus(StrEnum):
    pending = "pending"
    active = "active"
    disabled = "disabled"


class SeriesStatus(StrEnum):
    running = "running"
    waiting_jobs = "waiting_jobs"
    waiting_gate = "waiting_gate"
    held_budget = "held_budget"
    done = "done"
    discarded = "discarded"
    failed = "failed"


class SeriesStage(StrEnum):
    gate0 = "gate0"
    gate1 = "gate1"
    story = "story"
    gate2 = "gate2"
    visual = "visual"
    gate3 = "gate3"
    build = "build"
    qa = "qa"
    gate4 = "gate4"
    gate5 = "gate5"
    delivery = "delivery"
    done = "done"


class EpisodeStatus(StrEnum):
    outlined = "outlined"
    scripted = "scripted"
    shots_planned = "shots_planned"
    voiced = "voiced"
    keyframed = "keyframed"
    rendered = "rendered"
    compiled = "compiled"
    qa_passed = "qa_passed"
    approved = "approved"


class ShotStatus(StrEnum):
    planned = "planned"
    keyframe_ready = "keyframe_ready"
    rendering = "rendering"
    rendered = "rendered"
    syncing = "syncing"
    ready = "ready"
    qa_passed = "qa_passed"
    qa_failed = "qa_failed"
    needs_human = "needs_human"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    submitted = "submitted"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class JobPhase(StrEnum):
    submit = "submit"
    finalize = "finalize"


class BudgetClass(StrEnum):
    none = "none"
    video = "video"


class Vendor(StrEnum):
    anthropic = "anthropic"
    openai = "openai"
    elevenlabs = "elevenlabs"
    fal = "fal"
    local = "local"


class JobKind(StrEnum):
    ingest_source = "ingest_source"
    llm_bible = "llm_bible"
    llm_beats = "llm_beats"
    llm_script = "llm_script"
    llm_continuity = "llm_continuity"
    llm_wardrobe = "llm_wardrobe"
    llm_originality = "llm_originality"
    llm_placement_plan = "llm_placement_plan"
    llm_story_check = "llm_story_check"
    llm_trait_sheet = "llm_trait_sheet"
    llm_voice_profile = "llm_voice_profile"
    llm_audio_tag = "llm_audio_tag"
    image_master = "image_master"
    image_pack = "image_pack"
    image_set_plate = "image_set_plate"
    image_keyframe = "image_keyframe"
    voice_design = "voice_design"
    tts_line = "tts_line"
    sfx_ambience = "sfx_ambience"
    sfx_effect = "sfx_effect"
    music_episode = "music_episode"
    llm_shot_list = "llm_shot_list"
    llm_shot_request = "llm_shot_request"
    video_clip = "video_clip"
    lipsync = "lipsync"
    ffmpeg_insert = "ffmpeg_insert"
    ffmpeg_compile = "ffmpeg_compile"
    ffmpeg_master = "ffmpeg_master"
    qa_shot = "qa_shot"
    qa_episode = "qa_episode"
    qa_brand = "qa_brand"
    llm_rule = "llm_rule"
    llm_marketing = "llm_marketing"
    llm_feedback_classify = "llm_feedback_classify"


class Category(StrEnum):
    standard = "standard"
    ad_placement = "ad_placement"


class Language(StrEnum):
    bn = "bn"
    en = "en"


class Batch(StrEnum):
    pilot = "pilot"
    full = "full"


class ShotType(StrEnum):
    non_speaking = "non_speaking"
    speaking_closeup = "speaking_closeup"
    speaking_wider = "speaking_wider"
    hero = "hero"
    static_insert = "static_insert"


class Tier(StrEnum):
    A = "A"
    B1 = "B1"
    B2 = "B2"
    C = "C"
    INSERT = "INSERT"


class QaRound(StrEnum):
    precheck = "precheck"
    qa1 = "qa1"
    qa2 = "qa2"


class FeedbackCategory(StrEnum):
    picture = "picture"
    lipsync = "lipsync"
    voice = "voice"
    sound = "sound"
    music = "music"
    caption = "caption"
    story = "story"
    brand = "brand"


class FeedbackStatus(StrEnum):
    received = "received"
    confirmed = "confirmed"
    in_progress = "in_progress"
    fixed = "fixed"
    declined = "declined"


class PlacementKind(StrEnum):
    phone_full = "phone_full"
    phone_in_hand = "phone_in_hand"
    signage = "signage"
    spoken = "spoken"


class AlertKind(StrEnum):
    action_started = "action_started"
    action_finished = "action_finished"
    gate_open = "gate_open"
    qa_done = "qa_done"
    budget_100 = "budget_100"
    budget_125 = "budget_125"
    step_failed = "step_failed"
    feedback_status = "feedback_status"
    gate_reminder = "gate_reminder"
    access_request = "access_request"
