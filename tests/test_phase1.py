import threading
import uuid

import psycopg
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import db
from app.web.deps import require_decider
from app.web.main import app
from scripts import create_admin as ca
from scripts import migrate

PW = "correct horse"


@pytest.fixture()
def env(db_url):
    migrate.apply_migrations(db_url)
    with db.connect(db_url) as conn:
        conn.execute("truncate users, audit_log, alerts, series cascade")
        conn.commit()
    ca.create_admin("Admin@Example.com", "Boss", PW, db_url)
    return db_url


def client():
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def register(c, email, name="Member"):
    return c.post("/auth/register", data={"email": email, "display_name": name, "password": PW})


def login(c, email, password=PW):
    return c.post("/auth/login", data={"email": email, "password": password})


def user_id(email):
    return db.fetch_one("select id from users where email = %s", (email,))["id"]


def admin_client():
    c = client()
    assert login(c, "admin@example.com").status_code == 303
    return c


def active_member(email):
    """Register and approve a member; return a signed-in client."""
    register(client(), email)
    a = admin_client()
    assert a.post(f"/team/{user_id(email)}/approve").status_code == 303
    c = client()
    assert login(c, email).status_code == 303
    return c


def test_pending_user_cannot_sign_in(env):
    c = client()
    assert register(c, "p@x.io").status_code == 201
    r = login(c, "p@x.io")
    assert r.status_code == 403 and "waiting for the admin" in r.text
    assert c.get("/").status_code == 401


def test_wrong_password_and_unknown_email_look_the_same(env):
    c = client()
    assert login(c, "admin@example.com", "nope-nope").status_code == 401
    assert login(c, "ghost@x.io").status_code == 401


def test_approval_works_and_is_audited(env):
    register(client(), "m@x.io")
    a = admin_client()
    assert a.post(f"/team/{user_id('m@x.io')}/approve").status_code == 303
    m = client()
    assert login(m, "m@x.io").status_code == 303
    assert m.get("/").status_code == 200
    assert db.fetch_one("select 1 from audit_log where action = 'user.approve'")
    row = db.fetch_one("select status, approved_at from users where email = 'm@x.io'")
    assert row["status"] == "active" and row["approved_at"] is not None


def test_sixth_user_approval_is_refused(env):
    for i in range(4):  # admin + 4 members = 5 seats
        active_member(f"m{i}@x.io")
    register(client(), "late@x.io")
    r = admin_client().post(f"/team/{user_id('late@x.io')}/approve")
    assert r.status_code == 409 and "seats" in r.json()["detail"]
    assert db.fetch_one("select status from users where email = 'late@x.io'")["status"] == "pending"


def test_concurrent_approvals_cannot_exceed_the_cap(env):
    for i in range(3):  # 4 active so far
        active_member(f"m{i}@x.io")
    register(client(), "x1@x.io")
    register(client(), "x2@x.io")
    codes = []
    clients = [admin_client(), admin_client()]
    targets = [user_id("x1@x.io"), user_id("x2@x.io")]

    def go(c, t):
        codes.append(c.post(f"/team/{t}/approve").status_code)

    threads = [threading.Thread(target=go, args=(c, t)) for c, t in zip(clients, targets)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [303, 409]
    assert db.fetch_one("select count(*) n from users where status = 'active'")["n"] == 5


def test_member_cannot_approve_or_view_team(env):
    m = active_member("m@x.io")
    register(client(), "p@x.io")
    assert m.post(f"/team/{user_id('p@x.io')}/approve").status_code == 403
    assert m.get("/team").status_code == 403


def test_second_admin_insert_fails(env):
    with pytest.raises(RuntimeError):
        ca.create_admin("other@x.io", "Other", PW, env)
    with db.connect(env) as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("insert into users (email, display_name, password_hash, role, status) values ('z@x.io','Z','h','admin','active')")


def test_disabled_users_existing_session_is_rejected(env):
    m = active_member("m@x.io")
    assert m.get("/").status_code == 200
    assert admin_client().post(f"/team/{user_id('m@x.io')}/disable").status_code == 303
    assert m.get("/").status_code == 401
    assert login(client(), "m@x.io").status_code == 403


def test_admin_cannot_disable_self(env):
    a = admin_client()
    assert a.post(f"/team/{user_id('admin@example.com')}/disable").status_code == 400
    assert db.fetch_one("select status from users where role = 'admin'")["status"] == "active"


def test_register_rules(env):
    c = client()
    assert register(c, "Dup@X.io").status_code == 201
    assert register(c, "dup@x.io").status_code == 409  # stored lower-case
    assert db.fetch_one("select count(*) n from users where email = 'dup@x.io'")["n"] == 1
    bad = c.post("/auth/register", data={"email": "nope", "display_name": " ", "password": "short"})
    assert bad.status_code == 400 and bad.text.count('class="err"') == 3


def test_password_is_argon2id_and_cookie_holds_only_user_id(env):
    assert db.fetch_one("select password_hash h from users where role = 'admin'")["h"].startswith("$argon2id$")
    c = client()
    login(c, "admin@example.com")
    cookie = c.cookies.get("md_session")
    import base64, json
    payload = json.loads(base64.urlsafe_b64decode(cookie.split(".")[0] + "=="))
    assert list(payload) == ["user_id"]


def test_registration_alerts_the_admin(env):
    register(client(), "m@x.io", "Mia")
    row = db.fetch_one("select kind, message, link from alerts")
    assert row["kind"] == "access_request" and "Mia" in row["message"] and row["link"] == "/team"


def test_team_page_shows_seats(env):
    active_member("m@x.io")
    register(client(), "p@x.io")
    page = admin_client().get("/team")
    assert page.status_code == 200 and "2 of 5" in page.text and "p@x.io" in page.text


# require_decider: owner and admin yes, other member no
@app.get("/_test/decide/{series_id}")
def _decide(d: dict = Depends(require_decider)):
    return {"ok": True}


def make_series(owner_email):
    sid = uuid.uuid4()
    db.execute(
        "insert into series (id, owner_id, title, category, language, status, current_stage, thread_id) "
        "values (%s, %s, 't', 'standard', 'bn', 'running', 'story', %s)",
        (sid, user_id(owner_email), str(sid)),
    )
    return sid


def test_require_decider(env):
    owner = active_member("owner@x.io")
    other = active_member("other@x.io")
    sid = make_series("owner@x.io")
    assert owner.get(f"/_test/decide/{sid}").status_code == 200
    assert admin_client().get(f"/_test/decide/{sid}").status_code == 200
    assert other.get(f"/_test/decide/{sid}").status_code == 403
    assert owner.get(f"/_test/decide/{uuid.uuid4()}").status_code == 404
    assert client().get(f"/_test/decide/{sid}").status_code == 401


def test_html_requests_redirect_to_login(env):
    r = client().get("/", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"] == "/auth/login"

