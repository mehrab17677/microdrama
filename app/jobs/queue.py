"""Claim one job at a time, fairly across series (guide 7.2)."""
from __future__ import annotations

from app import budget, db
from app.config import get_config
from app.jobs import ledger

CLAIM_LOCK = 7_364_002  # serialises claims so vendor limits are exact
CANDIDATES = 50

_CANDIDATE_SQL = """
select j.* from jobs j
left join series s on s.id = j.series_id
where j.status = 'queued'
  and coalesce(j.vendor, 'local') <> all(%(blocked)s::text[])
  and not (j.budget_class = 'video' and j.phase = 'submit' and s.status = 'held_budget')
order by (select count(*) from jobs a
          where a.series_id is not distinct from j.series_id and a.status in ('running','submitted')),
         j.created_at
limit %(limit)s
for update of j skip locked
"""


def claim(worker_id: str, conn=None) -> dict | None:
    """Returns the claimed job (now 'running') or None."""
    with ledger._use(conn) as c:
        c.execute("select pg_advisory_xact_lock(%s)", (CLAIM_LOCK,))
        limits = get_config("vendor_concurrency", c)
        busy = {r["vendor"]: r["n"] for r in c.execute(
            "select coalesce(vendor, 'local') vendor, count(*) n from jobs where status in ('running','submitted') group by 1"
        ).fetchall()}
        blocked = [v for v, limit in limits.items() if busy.get(v, 0) >= limit]  # a limit of 0 pauses the vendor
        for job in c.execute(_CANDIDATE_SQL, {"blocked": blocked, "limit": CANDIDATES}).fetchall():
            if job["budget_class"] == "video" and job["phase"] == "submit" and not budget.charge_at_claim(c, job):
                continue  # leave it queued and select another
            claimed = c.execute(
                "update jobs set status = 'running', locked_by = %s, locked_at = now() where id = %s returning *",
                (worker_id, job["id"]),
            ).fetchone()
            ledger.touch_series(c, claimed["series_id"])
            return claimed
        return None
