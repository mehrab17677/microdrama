import threading
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import alerts, budget, db
from app.config import clear_config_cache
from app.graphs import runner
from app.jobs import ledger, poller, queue, worker
from app.jobs.errors import PermanentError, TransientError
from app.jobs.handlers import HANDLERS, register
from app.vendors import storage
from scripts import migrate, seed_config


@pytest.fixture()
def env(db_url, monkeypatch):
    migrate.apply_migrations(db_url)
    with db.connect(db_url) as conn:
        conn.execute("truncate users, audit_log, alerts, series, jobs, cost_log, graph_commands, config cascade")
        conn.commit()
    seed_config.seed(db_url)
    clear_config_cache()
    sleeps = []
    monkeypatch.setattr(worker, "_sleep", sleeps.append)
    HANDLERS.clear()
    yield SimpleNamespace(sleeps=sleeps)
    clear_config_cache()


def make_user(email="owner@x.io", role="member"):
    return db.fetch_one(
        "insert into users (email, display_name, password_hash, role, status) values (%s, %s, 'h', %s, 'active') returning id",
        (email, email.split("@")[0], role),
    )["id"]


def make_series(owner, status="running", batch="pilot"):
    sid = uuid.uuid4()
    db.execute(
        "insert into series (id, owner_id, title, category, language, status, current_stage, thread_id, batch)"
        " values (%s, %s, 'S', 'standard', 'bn', %s, 'build', %s, %s)",
        (sid, owner, status, str(sid), batch),
    )
    return sid


def make_episode(sid, number=1, batch="pilot"):
    return db.fetch_one(
        "insert into episodes (series_id, number, batch, status, beat) values (%s, %s, %s, 'outlined', '{}') returning id",
        (sid, number, batch),
    )["id"]


def job(series_id=None, kind="fake", request=None, **kw):
    kw.setdefault("vendor", "test")  # a vendor with no configured limit
    return ledger.enqueue(kind, series_id=series_id, request=request or {"n": str(uuid.uuid4())}, **kw)


def status(job_id):
    return ledger.get_job(job_id)


# ---------- enqueue / ledger ----------

def test_enqueue_same_request_twice_creates_one_row(env):
    s = make_series(make_user())
    a = ledger.enqueue("fake", series_id=s, request={"b": 2, "a": 1})
    b = ledger.enqueue("fake", series_id=s, request={"a": 1, "b": 2})  # key order does not matter
    assert a["id"] == b["id"]
    assert db.fetch_one("select count(*) n from jobs")["n"] == 1
    assert ledger.enqueue("fake", series_id=s, request={"a": 1, "b": 2}, attempt=2)["id"] != a["id"]


def test_enqueue_returns_a_finished_row_unchanged(env):
    s = make_series(make_user())
    a = ledger.enqueue("fake", series_id=s, request={"x": 1})
    register("fake", SimpleNamespace(run=lambda j: {"answer": 42}))
    assert worker.run_one("w1")
    again = ledger.enqueue("fake", series_id=s, request={"x": 1})
    assert again["status"] == "succeeded" and again["result"] == {"answer": 42}


def test_presigned_urls_are_rejected(env):
    with pytest.raises(ValueError):
        ledger.enqueue("fake", request={"url": "https://r2/x?X-Amz-Signature=abc"})


def test_series_updated_at_moves_when_a_job_changes_status(env):
    s = make_series(make_user())
    t0 = db.fetch_one("select updated_at from series where id = %s", (s,))["updated_at"]
    j = job(s)
    t1 = db.fetch_one("select updated_at from series where id = %s", (s,))["updated_at"]
    queue.claim("w")
    t2 = db.fetch_one("select updated_at from series where id = %s", (s,))["updated_at"]
    assert t0 < t1 < t2 and j


# ---------- claim ----------

def test_two_series_are_claimed_alternately(env):
    a, b = make_series(make_user("a@x.io")), make_series(make_user("b@x.io"))
    for _ in range(5):
        job(a)
    for _ in range(5):
        job(b)
    order = [queue.claim("w")["series_id"] for _ in range(4)]
    assert order == [a, b, a, b]


def test_vendor_concurrency_limit(env):
    s = make_series(make_user())
    db.execute("update config set value = '{\"fal\": 1, \"local\": 2}'::jsonb where key = 'vendor_concurrency'")
    clear_config_cache()
    f1, f2 = job(s, vendor="fal"), job(s, vendor="fal")
    loc = job(s, vendor="local")
    assert queue.claim("w")["id"] == f1["id"]
    nxt = queue.claim("w")
    assert nxt["id"] == loc["id"]  # fal is at its limit, so the second fal job is skipped
    assert queue.claim("w") is None and status(f2["id"])["status"] == "queued"


def test_concurrent_workers_never_claim_the_same_job(env):
    s = make_series(make_user())
    for _ in range(12):
        job(s)
    got, lock = [], threading.Lock()

    def go():
        while (j := queue.claim(f"w{threading.get_ident()}")) is not None:
            with lock:
                got.append(j["id"])

    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(got) == 12 and len(set(got)) == 12


# ---------- worker ----------

def test_transient_error_retries_in_place(env):
    s = make_series(make_user())
    calls = []

    def flaky(j):
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("HTTP 429")
        return {"ok": True}

    register("fake", SimpleNamespace(run=flaky))
    j = job(s)
    worker.run_one("w")
    row = status(j["id"])
    assert row["status"] == "succeeded" and row["attempt"] == 1
    assert env.sleeps == [2, 4] and len(calls) == 3


def test_transient_errors_give_up_after_five_retries(env):
    register("fake", SimpleNamespace(run=lambda j: (_ for _ in ()).throw(TransientError("down"))))
    j = job()
    worker.run_one("w")
    row = status(j["id"])
    assert env.sleeps == [2, 4, 8, 16, 32]
    assert row["status"] == "failed" and "gave up" in row["error"] and row["attempt"] == 1


def test_permanent_error_and_crash_fail_the_job(env):
    def bad(j):
        raise PermanentError("content policy")

    register("fake", SimpleNamespace(run=bad))
    j1 = job()
    worker.run_one("w")
    assert status(j1["id"])["status"] == "failed" and status(j1["id"])["error"] == "content policy"
    register("fake", SimpleNamespace(run=lambda j: 1 / 0))
    j2 = job()
    worker.run_one("w")
    assert "ZeroDivisionError" in status(j2["id"])["error"]
    j3 = job(kind="no_such_kind")
    worker.run_one("w")
    assert status(j3["id"])["status"] == "failed"


def test_costs_are_recorded(env):
    s = make_series(make_user())
    register("fake", SimpleNamespace(run=lambda j: {
        "text": "hi", "actual_cost": 0.5,
        "cost_log": [{"vendor": "openai", "model": "m", "units": 100, "unit_name": "tokens_in", "cost_usd": 0.5}],
    }))
    j = job(s, estimated_cost=1)
    worker.run_one("w")
    row = status(j["id"])
    assert row["actual_cost"] == Decimal("0.5") and row["result"] == {"text": "hi"} and row["finished_at"]
    assert db.fetch_one("select count(*) n, sum(cost_usd) c from cost_log where job_id = %s", (j["id"],)) == {"n": 1, "c": Decimal("0.5")}


def test_async_job_goes_submit_poll_finalize(env):
    s = make_series(make_user())
    seen = {}

    def finalize(j):
        seen["payload"] = j["result"]
        return {"copied": True}

    register("fake", SimpleNamespace(submit=lambda j: "req-123", finalize=finalize))
    j = job(s)
    worker.run_one("w")
    row = status(j["id"])
    assert row["status"] == "submitted" and row["vendor_request_id"] == "req-123"
    assert poller.tick()["completed"] == 0  # too young: dry-run completes after 1 second
    db.execute("update jobs set submitted_at = now() - interval '2 seconds' where id = %s", (j["id"],))
    assert poller.tick()["completed"] == 1
    row = status(j["id"])
    assert row["status"] == "queued" and row["phase"] == "finalize"
    assert not ledger.vendor_completed(j["id"], {"again": 1})  # a repeated webhook changes nothing
    worker.run_one("w")
    row = status(j["id"])
    assert row["status"] == "succeeded" and row["result"] == {"copied": True} and seen["payload"]["dry_run"] is True and seen["payload"]["video"]["url"].startswith("file://")


def test_vendor_error_fails_the_job(env):
    register("fake", SimpleNamespace(submit=lambda j: "r", finalize=lambda j: {}))
    j = job()
    worker.run_one("w")
    assert ledger.vendor_failed(j["id"], "ERROR from vendor")
    assert status(j["id"])["status"] == "failed"
    assert not ledger.vendor_failed(j["id"], "again")


# ---------- poller ----------

def test_job_locked_for_15_minutes_is_requeued(env):
    j = job()
    queue.claim("dead-worker")
    db.execute("update jobs set locked_at = now() - interval '14 minutes' where id = %s", (j["id"],))
    assert poller.tick()["requeued"] == 0
    db.execute("update jobs set locked_at = now() - interval '16 minutes' where id = %s", (j["id"],))
    assert poller.tick()["requeued"] == 1
    row = status(j["id"])
    assert row["status"] == "queued" and row["locked_by"] is None
    assert queue.claim("w2")["id"] == j["id"]


def test_submitted_job_times_out(env):
    register("fake", SimpleNamespace(submit=lambda j: "r", finalize=lambda j: {}))
    j = job()
    worker.run_one("w")
    db.execute("update jobs set submitted_at = now() - interval '31 minutes' where id = %s", (j["id"],))
    assert poller.tick()["timed_out"] == 1
    assert "timed out" in status(j["id"])["error"]


def test_gate_reminder_is_sent_once_per_period(env):
    admin, owner = make_user("admin@x.io", "admin"), make_user("owner@x.io")
    s = make_series(owner, status="waiting_gate")
    assert poller.tick()["reminders"] == 0
    db.execute("update series set updated_at = now() - interval '25 hours' where id = %s", (s,))
    assert poller.tick()["reminders"] == 2
    assert poller.tick()["reminders"] == 0
    assert db.fetch_one("select count(*) n from alerts where kind = 'gate_reminder'")["n"] == 2 and admin


# ---------- runner: job groups ----------

def waiting_series(group, jobs_status=("succeeded",)):
    s = make_series(make_user(f"{uuid.uuid4().hex[:8]}@x.io"), status="waiting_jobs")
    db.execute("update series set waiting_group = %s where id = %s", (group, s))
    for st in jobs_status:
        j = job(s, wait_group=group)
        db.execute("update jobs set status = %s where id = %s", (st, j["id"]))
    return s


def test_group_with_pending_jobs_is_not_resumed(env):
    s = waiting_series("g:1", ("succeeded", "running"))
    assert runner.resume_finished_groups() == 0
    assert db.fetch_one("select status from series where id = %s", (s,))["status"] == "waiting_jobs"


def test_finished_group_resumes_once_with_failed_count(env):
    s = waiting_series("g:2", ("succeeded", "failed", "failed"))
    assert runner.resume_finished_groups() == 1 and runner.resume_finished_groups() == 0
    cmd = db.fetch_one("select * from graph_commands where series_id = %s", (s,))
    assert cmd["kind"] == "resume" and cmd["graph"] == "production" and cmd["thread_id"] == str(s)
    assert cmd["payload"] == {"type": "jobs_done", "group": "g:2", "failed": 2}


def test_two_runners_started_together_produce_exactly_one_resume(env):
    for i in range(15):
        s = waiting_series(f"g:race{i}")
        barrier = threading.Barrier(3)

        def go():
            barrier.wait()
            runner.resume_finished_groups()

        ts = [threading.Thread(target=go) for _ in range(2)]
        [t.start() for t in ts]
        barrier.wait()
        [t.join() for t in ts]
        n = db.fetch_one("select count(*) n from graph_commands where series_id = %s", (s,))["n"]
        assert n == 1, f"round {i}: {n} resume commands"


# ---------- budget ----------

def video_setup(budget_usd=10):
    admin, owner = make_user("admin@x.io", "admin"), make_user("owner@x.io")
    s = make_series(owner, status="waiting_jobs")
    ep = make_episode(s)
    with db.transaction() as c:
        budget.create_budget(c, s, "pilot", budget_usd)
    return s, ep, owner, admin


def video_job(s, ep, cost=4):
    return job(s, episode_id=ep, vendor="fal", budget_class="video", estimated_cost=cost)


def spent(s):
    return db.fetch_one("select spent_usd from budgets where series_id = %s", (s,))["spent_usd"]


def test_budget_100_alerts_once_and_125_holds_the_series(env):
    s, ep, owner, admin = video_setup(10)  # limit 12.5
    jobs = [video_job(s, ep) for _ in range(4)]
    assert queue.claim("w") and queue.claim("w")           # spent 8
    assert queue.claim("w")                                 # spent 12: over 100%
    assert db.fetch_one("select count(*) n from alerts where kind = 'budget_100'")["n"] == 1
    assert queue.claim("w") is None                         # 16 > 12.5: held
    assert db.fetch_one("select status from series where id = %s", (s,))["status"] == "held_budget"
    assert status(jobs[3]["id"])["status"] == "queued" and spent(s) == 12
    held_alerts = db.fetch_all("select user_id from alerts where kind = 'budget_125'")
    assert {a["user_id"] for a in held_alerts} == {owner, admin}
    for _ in range(3):
        assert queue.claim("w") is None
    assert db.fetch_one("select count(*) n from alerts where kind = 'budget_125'")["n"] == 2  # no repeats
    assert db.fetch_one("select count(*) n from alerts where kind = 'budget_100'")["n"] == 1


def test_held_series_still_runs_non_video_jobs_and_approval_releases_it(env):
    s, ep, owner, admin = video_setup(10)
    [video_job(s, ep) for _ in range(4)]
    for _ in range(3):
        queue.claim("w")
    assert queue.claim("w") is None  # held
    other = job(s, vendor="openai")
    assert queue.claim("w")["id"] == other["id"]
    with db.transaction() as c:
        budget.approve_budget(c, s, "pilot", 5, owner)
    assert db.fetch_one("select status from series where id = %s", (s,))["status"] == "waiting_jobs"  # previous status
    assert queue.claim("w") is not None and spent(s) == 16


def test_budget_follows_actual_cost_failures_and_dead_workers(env):
    s, ep, *_ = video_setup(100)
    register("fake", SimpleNamespace(run=lambda j: {"actual_cost": 3}))
    video_job(s, ep, 4)
    worker.run_one("w")
    assert spent(s) == 3                       # charged 4, completed at 3

    def boom(j):
        raise PermanentError("no")

    register("fake", SimpleNamespace(run=boom))
    j2 = video_job(s, ep, 5)
    worker.run_one("w")
    assert status(j2["id"])["status"] == "failed" and spent(s) == 3   # the estimate is subtracted again

    j3 = video_job(s, ep, 6)
    queue.claim("dead")
    assert spent(s) == 9
    db.execute("update jobs set locked_at = now() - interval '20 minutes' where id = %s", (j3["id"],))
    ledger.requeue_stale_running()
    assert spent(s) == 3                       # refunded; it is charged again on re-claim


def test_video_job_without_a_budget_row_is_not_claimed(env):
    s = make_series(make_user())
    ep = make_episode(s)
    j = video_job(s, ep)
    assert queue.claim("w") is None and status(j["id"])["status"] == "queued"


def test_cancel_series_jobs(env):
    s = make_series(make_user())
    a, b = job(s), job(s)
    queue.claim("w")
    assert ledger.cancel_series_jobs(s) == 2
    assert {status(a["id"])["status"], status(b["id"])["status"]} == {"cancelled"}
    assert queue.claim("w") is None


# ---------- storage ----------

def test_local_storage_round_trip(env, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_TMP_DIR", str(tmp_path))
    storage.put_bytes("series/s1/a.txt", b"hello", "text/plain")
    out = tmp_path / "out" / "a.txt"
    storage.get_to_file("series/s1/a.txt", out)
    assert out.read_bytes() == b"hello"
    src = tmp_path / "src.bin"
    src.write_bytes(b"\x00\x01")
    storage.put_file("series/s1/b.bin", src)
    assert storage.presign_get("series/s1/b.bin").startswith("file://")
    with pytest.raises(FileNotFoundError):
        storage.get_to_file("missing", tmp_path / "x")


@pytest.mark.parametrize("key", ["", "/abs", "a/../b", "../x"])
def test_storage_rejects_bad_keys(env, key):
    with pytest.raises(ValueError):
        storage.put_bytes(key, b"x")


def test_r2_presigned_url_is_valid_for_six_hours(env, monkeypatch):
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    for k, v in {"R2_ACCOUNT_ID": "acct123", "R2_ACCESS_KEY_ID": "AK", "R2_SECRET_ACCESS_KEY": "SK", "R2_BUCKET": "bkt"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(storage, "_client", None)
    url = storage.presign_get("series/s1/final.mp4")
    assert url.startswith("https://acct123.r2.cloudflarestorage.com/") and "bkt" in url and "series/s1/final.mp4" in url
    assert "X-Amz-Expires=21600" in url


def test_real_mode_without_r2_settings_refuses(env, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    for k in ("R2_ACCOUNT_ID", "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        storage.put_bytes("k", b"x")


def test_r2_endpoint_variable_is_an_alternative_to_the_account_id(env, monkeypatch):
    monkeypatch.delenv("R2_ACCOUNT_ID", raising=False)
    for k, v in {"R2_ENDPOINT": "https://abc999.r2.cloudflarestorage.com/", "R2_ACCESS_KEY_ID": "AK", "R2_SECRET_ACCESS_KEY": "SK", "R2_BUCKET": "bkt"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(storage, "_client", None)
    assert storage.presign_get("a/b.mp4").startswith("https://abc999.r2.cloudflarestorage.com/")
