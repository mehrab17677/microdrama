-- 0001_init.sql: schema from Development Guide sections 4, 5 and 7.
-- Every table has id + created_at unless shown otherwise. FKs cascade unless stated.
-- Columns the guide adds later by ALTER (series.waiting_group, jobs.phase,
-- characters.emotion_groups) are declared inline here: the resulting schema is identical.

create extension if not exists vector;
create extension if not exists pgcrypto;

-- Users and access
create table users (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  email text not null unique,
  display_name text not null,
  password_hash text not null,
  role text not null check (role in ('admin','member')),
  status text not null check (status in ('pending','active','disabled')),
  approved_at timestamptz
);
create unique index users_one_admin on users (role) where role = 'admin';

create table audit_log (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  user_id uuid references users(id) on delete set null,
  series_id uuid,
  action text not null,
  detail jsonb not null default '{}'
);

create table alerts (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  user_id uuid not null references users(id) on delete cascade,
  series_id uuid,
  kind text not null,
  message text not null,
  link text not null,
  read_at timestamptz
);
create index alerts_user_unread on alerts (user_id, created_at);

-- Config (every tunable number lives here)
create table config (
  key text primary key,
  created_at timestamptz not null default now(),
  value jsonb not null,
  note text
);

-- Lore library
create table genres (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  name text not null,
  name_normalized text not null unique,
  created_by uuid references users(id) on delete set null
);

create sequence lore_version_seq;

create table sources (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  genre_id uuid references genres(id) on delete cascade,
  series_id uuid,
  uploaded_by uuid references users(id) on delete set null,
  batch_id uuid not null,
  file_name text not null,
  file_sha256 text not null unique,
  r2_key text not null,
  language text,
  status text not null check (status in ('queued','processing','ready','failed','skipped_duplicate')),
  error text,
  source_card jsonb,
  plot_summary text,
  lore_version bigint
);
create index sources_genre on sources (genre_id, status);
create index sources_batch on sources (batch_id);

create table source_chunks (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  source_id uuid not null references sources(id) on delete cascade,
  chapter_index int not null,
  text text not null,
  embedding vector(1536)
);
create index source_chunks_source on source_chunks (source_id);

create table pattern_cards (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  source_id uuid not null references sources(id) on delete cascade,
  genre_id uuid references genres(id) on delete cascade,
  kind text not null check (kind in ('trope','beat_structure','pacing','hook','cliffhanger','archetype','emotional_turn')),
  title text not null,
  body text not null,
  embedding vector(1536)
);

create table blocklist_terms (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  source_id uuid not null references sources(id) on delete cascade,
  term text not null,
  term_normalized text not null,
  kind text not null check (kind in ('character','place','invented'))
);
create index blocklist_term on blocklist_terms (term_normalized);

-- Series
create table series (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  owner_id uuid not null references users(id) on delete cascade,
  title text not null,
  category text not null check (category in ('standard','ad_placement')),
  genre_id uuid references genres(id) on delete cascade,
  custom_genre text,
  language text not null check (language in ('bn','en')),
  voice_route text not null default 'separate' check (voice_route in ('separate')),
  status text not null check (status in ('running','waiting_jobs','waiting_gate','held_budget','done','discarded','failed')),
  current_stage text not null check (current_stage in ('gate0','gate1','story','gate2','visual','gate3','build','qa','gate4','gate5','delivery','done')),
  batch text check (batch in ('pilot','full')),
  episode_count int,
  pinned_lore_version bigint,
  thread_id text not null unique,
  discard_category text check (discard_category in ('story','look','voice','quality','cost')),
  discard_notes text,
  updated_at timestamptz not null default now(),
  waiting_group text,
  check (thread_id = id::text)
);

create table briefs (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  version int not null,
  keywords text[] not null,
  aesthetic_note text,
  story_text text,
  unique (series_id, version)
);

create table brief_refs (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  kind text not null check (kind in ('character','aesthetic')),
  role_label text,
  r2_key text not null,
  face_vector vector(128)
);

create table brand_briefs (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  version int not null,
  brand_name text not null,
  product text not null,
  placement_types text[] not null,
  frequency_note text not null,
  exclusions text[] not null default '{}',
  spoken_wording text,
  unique (series_id, version)
);

create table brand_assets (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  kind text not null check (kind in ('logo','app_screen','product')),
  label text not null,
  r2_key text not null
);

-- Story
create table story_bibles (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  version int not null,
  content jsonb not null,
  series_summary text not null,
  unique (series_id, version)
);

create table characters (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  code text not null,
  name text not null,
  role text not null,
  is_lead boolean not null,
  visual_description text not null,
  voice_profile jsonb,
  voice_id text,
  pack_version int not null default 1,
  emotion_groups jsonb,
  unique (series_id, code)
);

create table character_assets (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  character_id uuid references characters(id) on delete cascade,
  kind text not null check (kind in ('master','turnaround','expression','wardrobe','pose','lineup','voice_sample')),
  tags jsonb not null default '{}',
  r2_key_review text,
  r2_key_clean text not null,
  face_vector vector(128),
  version int not null default 1,
  approved boolean not null default false
);
create index character_assets_char on character_assets (character_id, kind);

create table sets (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  code text not null,
  name text not null,
  description text not null,
  time_of_day text not null,
  plate_r2_key text,
  ambience_r2_key text,
  unique (series_id, code, time_of_day)
);

create table wardrobe_items (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  character_id uuid not null references characters(id) on delete cascade,
  code text not null,
  description text not null,
  scenes jsonb not null default '[]',
  unique (series_id, character_id, code)
);

create table pronunciations (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  term text not null,
  spoken_as text not null,
  unique (series_id, term)
);

create table placements (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  episode_number int not null,
  kind text not null check (kind in ('phone_full','phone_in_hand','signage','spoken')),
  description text not null,
  brand_asset_id uuid references brand_assets(id) on delete cascade,
  shot_id uuid,
  status text not null default 'planned' check (status in ('planned','placed','verified','failed'))
);

-- Episodes and dialogue
create table episodes (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  number int not null,
  batch text not null check (batch in ('pilot','full')),
  status text not null check (status in ('outlined','scripted','shots_planned','voiced','keyframed','rendered','compiled','qa_passed','approved')),
  beat jsonb not null,
  script jsonb,
  runtime_seconds numeric,
  cut_version int not null default 0,
  music_r2_key text,
  final_r2_key text,
  subtitle_r2_key text,
  unique (series_id, number)
);

create table dialogue_lines (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  episode_id uuid not null references episodes(id) on delete cascade,
  idx int not null,
  character_id uuid not null references characters(id) on delete cascade,
  text text not null,
  emotion text not null,
  audio_tag text,
  audio_r2_key text,
  duration_seconds numeric,
  alignment jsonb,
  version int not null default 1,
  unique (episode_id, idx)
);

-- Camera library (team-wide)
create table camera_setups (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  name text not null,
  shot_size text not null,
  angle text not null,
  movement text not null,
  composition text not null,
  best_for text not null,
  prompt_by_model jsonb not null default '{}',
  genre_id uuid references genres(id) on delete set null,
  origin_series_id uuid,
  uses int not null default 0,
  passes int not null default 0,
  lipsync_score_sum numeric not null default 0,
  lipsync_score_n int not null default 0,
  avoid_models text[] not null default '{}',
  embedding vector(1536)
);

-- Shots
create table shots (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  episode_id uuid not null references episodes(id) on delete cascade,
  idx int not null,
  shot_type text not null check (shot_type in ('non_speaking','speaking_closeup','speaking_wider','hero','static_insert')),
  tier text not null check (tier in ('A','B1','B2','C','INSERT')),
  camera_setup_id uuid references camera_setups(id) on delete set null,
  set_id uuid references sets(id) on delete cascade,
  spec jsonb not null,
  line_ids uuid[] not null default '{}',
  speaker_character_id uuid references characters(id) on delete cascade,
  planned_duration numeric,
  render_duration int,
  keyframe_r2_key text,
  raw_clip_r2_key text,
  final_clip_r2_key text,
  status text not null check (status in ('planned','keyframe_ready','rendering','rendered','syncing','ready','qa_passed','qa_failed','needs_human')),
  attempts int not null default 0,
  sync_attempts int not null default 0,
  fallback_used boolean not null default false,
  unique (episode_id, idx)
);
create index shots_series_status on shots (series_id, status);

create table edit_entries (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  episode_id uuid not null references episodes(id) on delete cascade,
  cut_version int not null,
  shot_id uuid not null references shots(id) on delete cascade,
  start_seconds numeric not null,
  end_seconds numeric not null
);
create index edit_entries_episode on edit_entries (episode_id, cut_version);

-- Jobs: queue and ledger in one table
create table jobs (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid,
  episode_id uuid,
  shot_id uuid,
  kind text not null,
  wait_group text,
  idempotency_key text not null unique,
  vendor text,
  endpoint text,
  request jsonb not null,
  vendor_request_id text,
  status text not null check (status in ('queued','running','submitted','succeeded','failed','cancelled')),
  attempt int not null default 1,
  budget_class text not null default 'none' check (budget_class in ('none','video')),
  estimated_cost numeric not null default 0,
  actual_cost numeric,
  result jsonb,
  error text,
  locked_by text,
  locked_at timestamptz,
  submitted_at timestamptz,
  finished_at timestamptz,
  phase text not null default 'submit' check (phase in ('submit','finalize'))
);
create index jobs_claim on jobs (status, created_at);
create index jobs_group on jobs (wait_group, status);
create index jobs_series_status on jobs (series_id, status);
create index jobs_vendor_request on jobs (vendor_request_id);

create table graph_commands (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid,
  graph text not null check (graph in ('ingest','production')),
  thread_id text not null,
  kind text not null check (kind in ('start','resume')),
  payload jsonb not null,
  status text not null default 'queued' check (status in ('queued','running','done','failed')),
  error text
);
create index graph_commands_queue on graph_commands (status, created_at);

-- Money
create table budgets (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  batch text not null check (batch in ('pilot','full')),
  budget_usd numeric not null,
  approved_extra_usd numeric not null default 0,
  spent_usd numeric not null default 0,
  alerted_100 boolean not null default false,
  unique (series_id, batch)
);

create table cost_log (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid,
  job_id uuid references jobs(id) on delete set null,
  vendor text not null,
  model text not null,
  units numeric not null,
  unit_name text not null,
  cost_usd numeric not null
);
create index cost_log_series on cost_log (series_id);

-- QA and learning
create table qa_results (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  episode_id uuid references episodes(id) on delete cascade,
  shot_id uuid references shots(id) on delete cascade,
  qa_round text not null check (qa_round in ('precheck','qa1','qa2')),
  check_name text not null,
  passed boolean not null,
  score numeric,
  category text,
  detail jsonb not null default '{}'
);
create index qa_results_shot on qa_results (shot_id);

create table learned_rules (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  model_key text not null,
  rule_text text not null,
  category text not null,
  source_shot_id uuid,
  hits int not null default 0,
  wins int not null default 0,
  active boolean not null default true,
  embedding vector(1536)
);

-- Human decisions
create table gate_decisions (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  gate int not null check (gate between 0 and 5),
  round int not null default 1,
  decision text not null,
  notes text,
  decided_by uuid not null references users(id) on delete cascade,
  unique (series_id, gate, round)
);

create table feedback_items (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  series_id uuid not null references series(id) on delete cascade,
  episode_number int not null,
  start_seconds int not null,
  end_seconds int not null,
  note text not null,
  category text check (category in ('picture','lipsync','voice','sound','music','caption','story','brand')),
  status text not null default 'received' check (status in ('received','confirmed','in_progress','fixed','declined')),
  shot_ids uuid[] not null default '{}',
  decline_reason text,
  created_by uuid not null references users(id) on delete cascade
);
