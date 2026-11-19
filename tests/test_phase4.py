import io
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from app import db, library
from app.config import clear_config_cache
from app.jobs import worker
from app.jobs.errors import PermanentError, TransientError
from app.jobs.handlers import HANDLERS, load_builtin
from app.models.library import SourceAnalysis
from app.stages import ingest
from app.text import contains_term, detect_language, has_shared_run, normalize_for_match, normalize_genre
from app.vendors import llm
from app.web import auth
from app.web.main import app
from scripts import migrate, seed_config

PW = "correct horse"


@pytest.fixture()
def env(db_url, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_TMP_DIR", str(tmp_path))
    migrate.apply_migrations(db_url)
    with db.connect(db_url) as conn:
        conn.execute("truncate users, audit_log, alerts, series, jobs, cost_log, graph_commands, config, genres, sources cascade")
        conn.commit()
    seed_config.seed(db_url)
    clear_config_cache()
    monkeypatch.setattr(worker, "_sleep", lambda s: None)
    HANDLERS.clear()
    load_builtin()
    db.execute(
        "insert into users (email, display_name, password_hash, role, status) values ('m@x.io','Mia',%s,'member','active')",
        (auth.hash_password(PW),),
    )
    yield
    clear_config_cache()


def client():
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert c.post("/auth/login", data={"email": "m@x.io", "password": PW}).status_code == 303
    return c


def upload(c, genre, files):
    return c.post("/library/upload", data={"genre": genre}, files=[("files", (n, io.BytesIO(b), "application/octet-stream")) for n, b in files])


def run_jobs():
    n = 0
    while worker.run_one("w"):
        n += 1
    return n


def story(chapters=3, words=400, seed="river"):
    parts = []
    for i in range(1, chapters + 1):
        body = " ".join(f"{seed}{i}w{j}" for j in range(words))
        parts.append(f"Chapter {i}\n\n{body}")
    return "\n\n".join(parts).encode()


def statuses():
    return {r["file_name"]: r["status"] for r in db.fetch_all("select file_name, status from sources")}


# ---------------- text helpers ----------------

def test_genre_normalisation():
    assert normalize_genre("Revenge romance") == normalize_genre("revenge-romance") == normalize_genre("  REVENGE -  Romance ") == "revenge romance"


def test_match_normalisation_and_terms():
    assert normalize_for_match("Rafiq's  Ghat!") == "rafiq s ghat"
    assert contains_term(normalize_for_match("Alice said hi"), "ali") is False     # whole words only for Latin
    assert contains_term(normalize_for_match("Ali said hi"), "ali") is True
    assert contains_term(normalize_for_match("সে রফিককে ডাকল"), normalize_for_match("রফিক")) is True  # Bangla suffixes attach
    assert normalize_for_match("আমি, তোমাকে!") == "আমি তোমাকে"


def test_shared_run_and_language():
    a = "one two three four five six seven eight nine"
    assert has_shared_run(a, "x " + a) and not has_shared_run(a, "one two three four five six seven NOPE nine")
    assert detect_language("আমি তোমাকে ভালোবাসি") == "bn" and detect_language("I love you") == "en" and detect_language("123 ...") == "other"


# ---------------- extraction ----------------

def test_extract_text_variants(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes("﻿Hello world".encode("utf-8"))
    assert ingest.extract_text(p, "a.txt") == "Hello world"
    p.write_bytes("আমি".encode("utf-16"))
    assert ingest.extract_text(p, "a.txt") == "আমি"
    p.write_bytes(b"\x80\x81\x82 abc")
    with pytest.raises(PermanentError):
        ingest.extract_text(p, "b.txt")
    p.write_bytes(b"   \n  ")
    with pytest.raises(PermanentError, match="no text"):
        ingest.extract_text(p, "c.txt")
    with pytest.raises(PermanentError, match="unsupported"):
        ingest.extract_text(p, "d.docx")


def make_pdf(text: str) -> bytes:
    """A minimal one-page text PDF with a correct cross-reference table."""
    stream = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
            b"<</Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream", b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)


def test_extract_epub_and_pdf(env, tmp_path):
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("id1"); book.set_title("T"); book.set_language("en")
    chapters = []
    for i in (1, 2):
        c = epub.EpubHtml(title=f"c{i}", file_name=f"c{i}.xhtml", lang="en")
        c.content = f"<html><body><h1>Chapter {i}</h1><p>First paragraph {i}.</p><p>Second &amp; last {i}.</p></body></html>"
        book.add_item(c); chapters.append(c)
    book.toc = chapters; book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav()); book.spine = ["nav", *chapters]
    path = tmp_path / "b.epub"
    epub.write_epub(str(path), book)
    text = ingest.extract_text(path, "b.epub")
    assert "\f" in text and "First paragraph 1." in text and "Second & last 2." in text
    assert len(ingest.split_chunks(text, "en")) == 3  # nav page + two chapters, each a chunk

    pdf = tmp_path / "c.pdf"
    pdf.write_bytes(make_pdf("Hello pdf world"))
    assert "Hello pdf world" in ingest.extract_text(pdf, "c.pdf")
    pdf.write_bytes(b"this is not a pdf at all")
    with pytest.raises(PermanentError, match="could not be read"):
        ingest.extract_text(pdf, "c.pdf")


# ---------------- chunking ----------------

def test_split_by_chapter_headings(env):
    chunks = ingest.split_chunks(story(3, 400).decode(), "en")
    assert len(chunks) == 3 and chunks[1].startswith("Chapter 2")


def test_split_without_headings_every_cap_words_at_paragraph_breaks(env):
    paras = ["word " * 700 for _ in range(10)]               # 7,000 words in 10 paragraphs
    chunks = ingest.split_chunks("\n\n".join(paras), "en")  # cap 3,000 for English
    assert [len(c.split()) for c in chunks] == [2800, 2800, 1400]
    bn = ingest.split_chunks("\n\n".join(["শব্দ " * 400 for _ in range(10)]), "bn")  # cap 1,000 for Bangla
    assert max(len(c.split()) for c in bn) <= 1000 and len(bn) == 5


def test_long_chapters_and_giant_paragraphs_are_split_further(env):
    text = "Chapter 1\n\n" + ("word " * 7000) + "\n\nChapter 2\n\nshort one"
    chunks = ingest.split_chunks(text, "en")
    assert max(len(c.split()) for c in chunks) <= 3000 and len(chunks) >= 3
    assert len(ingest.split_chunks("line one\nline two\nline three", "en")) == 1  # single-newline text is still text


def test_bangla_chapter_headings(env):
    text = "অধ্যায় ১\n\n" + "শব্দ " * 50 + "\n\nঅধ্যায় ২\n\n" + "শব্দ " * 50
    assert len(ingest.split_chunks(text, "bn")) == 2


# ---------------- acceptance: bulk upload ----------------

def test_bulk_upload_of_three_files_ready_skipped_failed_and_one_alert(env):
    c = client()
    good = story()
    r = upload(c, "Revenge romance", [("good.txt", good), ("same.txt", good), ("broken.pdf", b"not really a pdf")])
    assert r.status_code == 303
    assert statuses() == {"good.txt": "queued", "same.txt": "skipped_duplicate", "broken.pdf": "queued"}
    assert db.fetch_one("select count(*) n from alerts where kind = 'action_finished'")["n"] == 0
    assert run_jobs() == 2
    assert statuses() == {"good.txt": "ready", "same.txt": "skipped_duplicate", "broken.pdf": "failed"}
    finished = db.fetch_all("select message, link from alerts where kind = 'action_finished'")
    assert len(finished) == 1 and "1 ready, 1 skipped, 1 failed" in finished[0]["message"]
    assert db.fetch_one("select count(*) n from alerts where kind = 'action_started'")["n"] == 1
    bad = db.fetch_one("select error from sources where file_name = 'broken.pdf'")["error"]
    assert "could not be read" in bad
    dup = db.fetch_one("select error from sources where file_name = 'same.txt'")["error"]
    assert "good.txt" in dup


def test_ingestion_writes_chunks_cards_blocklist_and_lore_version(env):
    upload(client(), "Revenge romance", [("good.txt", story(3, 400))])
    run_jobs()
    src = db.fetch_one("select * from sources")
    assert src["status"] == "ready" and src["lore_version"] and src["language"] == "en" and src["plot_summary"]
    assert src["source_card"]["chunk_count"] == 3 and src["source_card"]["word_count"] > 1000 and src["source_card"]["logline"]
    assert db.fetch_one("select count(*) n from source_chunks")["n"] == 3
    assert db.fetch_one("select min(vector_dims(embedding)) a, max(vector_dims(embedding)) b from source_chunks") == {"a": 1536, "b": 1536}
    cards = db.fetch_all("select kind, title, body, genre_id from pattern_cards")
    assert 5 <= len(cards) <= 15 and all(len(c["body"].split()) <= 80 and c["genre_id"] == src["genre_id"] for c in cards)
    terms = {r["term"] for r in db.fetch_all("select term from blocklist_terms")}
    assert {"Rafiq", "রফিক", "Shonar Gaon", "সোনার গাঁও"} <= terms           # Latin and Bangla forms both stored
    assert db.fetch_one("select count(*) n from blocklist_terms where term_normalized = 'rafiq'")["n"] == 1
    job = db.fetch_one("select * from jobs")
    assert job["status"] == "succeeded" and job["result"]["pattern_cards"] == len(cards)
    # a second source gets a later lore version
    upload(client(), "Revenge romance", [("two.txt", story(2, 300, "lake"))])
    run_jobs()
    assert db.fetch_one("select max(lore_version) m, count(distinct lore_version) n from sources") == {"m": db.fetch_one("select max(lore_version) m from sources")["m"], "n": 2}


def test_genre_is_listed_for_intake_only_after_a_source_is_ready(env):
    c = client()
    upload(c, "Revenge romance", [("a.txt", story())])
    assert [g["name"] for g in library.ready_genres()] == []
    assert c.get("/library/genres?q=revenge&ready_only=true").json() == []
    assert [g["name"] for g in c.get("/library/genres?q=revenge").json()] == ["Revenge romance"]   # typing suggestions show all
    run_jobs()
    ready = c.get("/library/genres?q=rev&ready_only=true").json()
    assert [(g["name"], g["ready_sources"]) for g in ready] == [("Revenge romance", 1)]


def test_failed_source_does_not_make_a_genre_ready(env):
    upload(client(), "Horror", [("x.pdf", b"garbage")])
    run_jobs()
    assert library.ready_genres() == []


def test_revenge_romance_and_revenge_hyphen_romance_are_one_genre(env):
    c = client()
    upload(c, "Revenge romance", [("a.txt", story(seed="a"))])
    upload(c, "revenge-romance", [("b.txt", story(seed="b"))])
    upload(c, "  REVENGE   romance ", [("c.txt", story(seed="c"))])
    genres = db.fetch_all("select name from genres")
    assert [g["name"] for g in genres] == ["Revenge romance"]
    assert db.fetch_one("select count(distinct genre_id) n from sources")["n"] == 1


def test_genre_suggestions_prefix_first_and_escape_wildcards(env):
    c = client()
    for i, g in enumerate(["Dark romance", "Romance", "Romantic comedy", "Thriller"]):
        upload(c, g, [(f"f{i}.txt", story(seed=f"s{i}"))])
    names = [g["name"] for g in c.get("/library/genres?q=roman").json()]
    assert names[:2] == ["Romance", "Romantic comedy"] and "Dark romance" in names and "Thriller" not in names
    assert c.get("/library/genres?q=100%25").json() == [] and c.get("/library/genres?q=_").json() == []


def test_reuploading_a_failed_file_is_allowed_but_a_ready_one_is_a_duplicate(env):
    c = client()
    upload(c, "g", [("a.pdf", b"garbage")])
    run_jobs()
    upload(c, "g", [("a2.pdf", b"garbage")])
    assert statuses() == {"a.pdf": "failed", "a2.pdf": "queued"}          # the failed attempt does not block a retry
    upload(c, "g", [("ok.txt", story())])
    run_jobs()
    upload(c, "g", [("ok_again.txt", story())])
    assert statuses()["ok_again.txt"] == "skipped_duplicate"


def test_a_batch_of_only_duplicates_finishes_at_once(env):
    c = client()
    upload(c, "g", [("ok.txt", story())])
    run_jobs()
    upload(c, "g", [("copy1.txt", story())])
    msgs = [r["message"] for r in db.fetch_all("select message from alerts where kind = 'action_finished' order by created_at")]
    assert msgs[-1].endswith("0 ready, 1 skipped, 0 failed.")


def test_unsupported_and_oversized_files_are_recorded_as_failed(env):
    c = client()
    upload(c, "g", [("notes.docx", b"x")])
    assert db.fetch_one("select status, error from sources") == {"status": "failed", "error": "Unsupported file type. Use .txt, .epub or .pdf."}
    db.execute("update config set value = '0'::jsonb where key = 'max_upload_mb'")
    clear_config_cache()
    upload(c, "g", [("big.txt", b"some bytes")])
    assert "larger than" in db.fetch_one("select error from sources where file_name = 'big.txt'")["error"]
    assert db.fetch_one("select count(*) n from jobs")["n"] == 0


def test_empty_upload_and_blank_genre_are_rejected(env):
    c = client()
    assert c.post("/library/upload", data={"genre": "x"}).status_code in (400, 422)
    assert upload(c, "   ", [("a.txt", b"hi")]).status_code == 400
    assert db.fetch_one("select count(*) n from sources")["n"] == 0


# ---------------- validation, retries and failure handling ----------------

def patch_final_answers(monkeypatch, answers):
    """Make the SourceAnalysis call return the given objects in turn; everything else stays dry-run."""
    real, calls = llm.complete, {"n": 0}

    def fake(role, system, messages, schema=None, images=None):
        if schema is SourceAnalysis:
            calls["n"] += 1
            obj = answers[min(calls["n"], len(answers)) - 1]
            if callable(obj):
                obj = obj(messages)
            return obj, {"model": "anthropic:claude-sonnet-5-5", "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
        return real(role, system, messages, schema, images)

    monkeypatch.setattr(llm, "complete", fake)
    return calls


def good_analysis():
    return llm.complete("writer", "s", [], schema=SourceAnalysis)[0]


def test_invalid_analysis_is_retried_with_the_problems_and_then_accepted(env, monkeypatch):
    good = good_analysis()
    bad = good.model_copy(deep=True)
    bad.pattern_cards = bad.pattern_cards[:3]                      # too few
    bad.pattern_cards[0].body = "Rafiq returned to Shonar Gaon " + "x " * 10  # leaks names
    seen = []
    calls = patch_final_answers(monkeypatch, [bad, lambda msgs: seen.append(msgs[-1]["content"]) or good])
    upload(client(), "g", [("a.txt", story())])
    run_jobs()
    assert calls["n"] == 2 and statuses() == {"a.txt": "ready"}
    assert "between 5 and 15" in seen[0] and "contains source names" in seen[0]


def test_analysis_that_keeps_failing_fails_the_source(env, monkeypatch):
    bad = good_analysis().model_copy(deep=True)
    bad.plot_summary = "too short"
    patch_final_answers(monkeypatch, [bad])
    upload(client(), "g", [("a.txt", story())])
    run_jobs()
    row = db.fetch_one("select status, error from sources")
    assert row["status"] == "failed" and "plot_summary" in row["error"]
    assert db.fetch_one("select status from jobs")["status"] == "failed"
    assert db.fetch_one("select count(*) n from pattern_cards")["n"] == 0                 # nothing half-written
    assert "0 ready, 0 skipped, 1 failed" in db.fetch_one("select message from alerts where kind = 'action_finished'")["message"]


def test_quoted_source_text_in_a_card_is_rejected(env):
    chunks = ["the quick brown fox jumps over the lazy dog every single morning without fail"]
    a = good_analysis().model_copy(deep=True)
    a.pattern_cards[0].body = "A fox jumps: the quick brown fox jumps over the lazy dog every single morning."
    problems = ingest.validate_analysis(a, [], "\n".join(chunks))
    assert any("quotes the source" in p for p in problems)
    assert ingest.validate_analysis(good_analysis(), ["rafiq"], "unrelated text") == []


def test_card_validation_limits():
    a = good_analysis().model_copy(deep=True)
    a.pattern_cards[0].body = "word " * 81
    assert any("81 words" in p for p in ingest.validate_analysis(a, [], ""))
    a.pattern_cards = a.pattern_cards * 3
    assert any("not 21" in p for p in ingest.validate_analysis(a, [], ""))


def test_retries_that_run_out_mark_the_source_failed(env, monkeypatch):
    def boom(source, path):
        raise TransientError("HTTP 503")

    monkeypatch.setattr(ingest, "ingest", boom)
    upload(client(), "g", [("a.txt", story())])
    run_jobs()
    row = db.fetch_one("select status, error from sources")
    assert row["status"] == "failed" and "gave up" in row["error"]


def test_handler_retry_replaces_earlier_output(env):
    upload(client(), "g", [("a.txt", story(3, 400))])
    run_jobs()
    src = db.fetch_one("select * from sources")
    job = {"request": {"source_id": str(src["id"])}, "id": uuid.uuid4()}
    from app.jobs.handlers import ingest_source

    ingest_source.run(job)
    assert db.fetch_one("select count(*) n from source_chunks")["n"] == 3
    assert db.fetch_one("select count(*) n from blocklist_terms")["n"] == 8


def test_batch_finished_alert_is_sent_once_even_with_a_race(env):
    c = client()
    upload(c, "g", [("a.txt", story(seed="a")), ("b.txt", story(seed="b"))])
    db.execute("update sources set status = 'ready'")
    db.execute("delete from alerts where kind = 'action_finished'")
    batch = db.fetch_one("select batch_id from sources limit 1")["batch_id"]
    barrier = threading.Barrier(5)

    def go():
        barrier.wait()
        with db.transaction() as conn:
            library.check_batch_done(conn, batch)

    ts = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert db.fetch_one("select count(*) n from alerts where kind = 'action_finished'")["n"] == 1


# ---------------- pages ----------------

def test_library_page_needs_login_and_shows_batches_and_cards(env):
    assert TestClient(app, base_url="https://testserver").get("/library").status_code == 401
    c = client()
    upload(c, "Revenge romance", [("good.txt", story()), ("junk.pdf", b"nope")])
    run_jobs()
    page = c.get("/library")
    assert page.status_code == 200
    for needle in ("good.txt", "junk.pdf", "failed", "ready", "Revenge romance", "A woman returns home", "2 file(s)"):
        assert needle in page.text, needle


def test_bangla_source_is_detected_and_chunked_with_the_lower_limit(env):
    text = "\n\n".join("শব্দ " * 400 for _ in range(6)).encode("utf-8")
    upload(client(), "প্রেম", [("bn.txt", text)])
    run_jobs()
    src = db.fetch_one("select language, source_card from sources")
    assert src["language"] == "bn" and src["source_card"]["chunk_count"] == 3
