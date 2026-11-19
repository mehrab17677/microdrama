"""Set a new password for an existing user (the admin who forgot theirs, for example). There is no web page for this.

Usage: python -m scripts.reset_password --email you@example.com
The new password is read from NEW_PASSWORD, or asked for twice. A reset also clears the sign-in lockout for that address.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import audit, db  # noqa: E402
from app.web import auth  # noqa: E402


def reset_password(email: str, password: str, url: str | None = None) -> None:
    email = auth.normalize_email(email)
    problems = [p for p in auth.validate_registration(email, "x", password) if "assword" in p]
    if problems:
        raise ValueError(" ".join(problems))
    with db.connect(url) as conn:
        row = conn.execute("select id from users where email = %s", (email,)).fetchone()
        if not row:
            raise RuntimeError("No user with that email address.")
        conn.execute("update users set password_hash = %s where email = %s", (auth.hash_password(password), email))
        audit.log(conn, None, "auth.password_reset", None, {"email": email})
        conn.commit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    args = ap.parse_args()
    pw = os.environ.get("NEW_PASSWORD")
    if not pw:
        pw = getpass.getpass("New password (at least 8 characters, nothing shows as you type): ")
        if pw != getpass.getpass("Type it again: "):
            sys.exit("error: the two passwords differ. Nothing was changed.")
    try:
        reset_password(args.email, pw)
    except (ValueError, RuntimeError) as err:
        sys.exit(f"error: {err}")
    print(f"password changed for {args.email}")
