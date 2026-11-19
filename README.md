# microdrama

Pipeline that turns a story brief into a series of 75 to 90 second vertical micro-dramas (Bangla or English): story, look, voices, shots, video, captions, QA, human gates,
delivery. Ordinary dramas and **ad placement dramas** (a real product appears inside generated scenes). Python 3.12, FastAPI, LangGraph, Postgres with pgvector, Cloudflare R2,
Anthropic, OpenAI, ElevenLabs and fal.

## How it runs
One image, five process types, one Postgres, one R2 bucket (`docker-compose.yml`):

| Process | Does |
|---|---|
| `web` | pages, gates, feedback, event stream, webhooks. Reads and writes Postgres. Never calls a paid API. |
| `worker` (x2) | claims jobs from the `jobs` table and runs their handlers |
| `runner` | runs the LangGraph production graph (one resume per finished job group) |
| `poller` | checks submitted video jobs, requeues dead workers' jobs, sends gate reminders |
| `retention` | daily: deletes raw and failed clips older than `raw_retention_days` |

Every paid call is a row in the `jobs` table (the ledger). `DRY_RUN=1` runs everything on fixtures at no cost.

## Develop
```bash
./scripts/dev_postgres.sh                 # local Postgres with pgvector on port 5433
python -m venv .venv && .venv/bin/pip install -r requirements.txt
DRY_RUN=1 .venv/bin/python scripts/migrate.py && DRY_RUN=1 .venv/bin/python scripts/seed_config.py
.venv/bin/python -m pytest -q             # about 12 minutes; every test runs with DRY_RUN=1
```
Tests need `TEST_DATABASE_URL` (default `postgresql://postgres@localhost:5433/microdrama_test`).

## Documents
- `docs/DEPLOYMENT.md`: deploy, operate, back up, what is and is not verified.
- `docs/PILOT_RUNBOOK.md`: the paid checks and pilots, in order, with costs and stop conditions.
- `docs/AD_PLACEMENT.md`: how brand placement works and its limits.
- `docs/VENDOR_NOTES.md`: every vendor fact checked, every decision per phase.
- `docs/LIPSYNC_OPTIONS.md`, `docs/OPEN_SOURCE_TEST_SUBSTITUTES.md`, `docs/DEVELOPMENT_PLAN.md`.
