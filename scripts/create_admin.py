"""Create the single admin account. There is no web page for this (guide 12.1).

Usage: python scripts/create_admin.py --email you@example.com --name "Your Name"
The password is read from ADMIN_PASSWORD or prompted for.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from app import db  # noqa: E402
from app.web import auth  # noqa: E402


def create_admin(email: str, display_name: str, password: str, url: str | None = None) -> None:
    email = auth.normalize_email(email)
    problems = auth.validate_registration(email, display_name, password)
    if problems:
        raise ValueError(" ".join(problems))
    with db.connect(url) as conn:
        try:
            conn.execute(
                "insert into users (email, display_name, password_hash, role, status, approved_at) "
                "values (%s, %s, %s, 'admin', 'active', now())",
                (email, display_name.strip(), auth.hash_password(password)),
            )
        except psycopg.errors.UniqueViolation as exc:
            conn.rollback()
            raise RuntimeError("An admin or a user with this email already exists.") from exc
        conn.commit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", required=True)
    args = ap.parse_args()
    pw = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
    try:
        create_admin(args.email, args.name, pw)
    except (ValueError, RuntimeError) as err:
        sys.exit(f"error: {err}")
    print(f"admin created: {args.email}")
