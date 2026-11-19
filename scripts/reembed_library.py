"""Re-embed every stored vector with the CURRENT embedding backend.  Run after switching from the test embedding model (EMBEDDING_BACKEND=local) to OpenAI:
vectors from different models are not comparable, so similarity search (pattern cards, originality checks, camera setups, learned rules) would be meaningless.

    DRY_RUN=0 python -m scripts.reembed_library [--dry-run]

Re-embeds source chunks, pattern cards, camera setups and learned rules, in batches of 100. Cost: text-embedding-3-small, about $0.02 per million tokens."""
from __future__ import annotations

import os
import sys

from app import db
from app.stages.ingest import EMBED_MAX_CHARS
from app.vendors import images

BATCH = 100
TABLES = {
    "source_chunks": ("select sc.id, sc.text t, src.language lang from source_chunks sc join sources src on src.id = sc.source_id where sc.embedding is not null order by sc.id", None),
    "pattern_cards": ("select id, title || '. ' || body t from pattern_cards where embedding is not null order by id", None),
    "camera_setups": ("select id, name || '. ' || shot_size || ', ' || angle || ', ' || movement || '. ' || composition || '. Best for ' || best_for t from camera_setups where embedding is not null order by id", None),
    "learned_rules": ("select id, rule_text t from learned_rules where embedding is not null order by id", None),
}


def run(dry_run: bool = False) -> dict:
    if os.environ.get("EMBEDDING_BACKEND") == "local":
        raise SystemExit("EMBEDDING_BACKEND=local is set: unset it first, or the vectors would be re-made with the test model.")
    counts = {}
    for table, (sql, _) in TABLES.items():
        rows = db.fetch_all(sql)
        counts[table] = len(rows)
        if dry_run:
            continue
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            vectors, _usage = images.embed([r["t"][:EMBED_MAX_CHARS.get(r.get("lang"), 7000)] for r in chunk])   # the same per-language cut as at ingest
            with db.transaction() as conn:
                for r, v in zip(chunk, vectors):
                    conn.execute(f"update {table} set embedding = %s::vector where id = %s", (db.to_vector(v), r["id"]))
    return counts


if __name__ == "__main__":
    print(run(dry_run="--dry-run" in sys.argv[1:]))
