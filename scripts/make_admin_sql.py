#!/usr/bin/env python
"""Print ready-to-run SQL that provisions an administrator.

The operator console never registers admins: an admin is a ``users`` row whose
``role`` is ``ADMIN``, inserted directly in the database (for Supabase, paste
the output into the SQL editor).  This script generates that SQL for you —
it does **not** connect to the database.

It prints two statements so it works whether or not the account already
exists:

* an ``UPDATE`` that promotes an existing user with the given e-mail, and
* an ``INSERT`` that creates the account from scratch.

Run the one that applies.  The password is hashed with the same passlib
context the application uses, so it can be verified at login.

Usage::

    python -m scripts.make_admin_sql --name "Ana" --email ana@example.com --password 'S3nh@Forte!'

Prefer ``ADMIN_PASSWORD`` in the environment (or omit ``--password`` to be
prompted) so the password never lands in your shell history.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.security import pwd_context  # noqa: E402


def _sql_literal(value: str) -> str:
    """Quote a Python string as a PostgreSQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def build_sql(name: str, email: str, password_hash: str) -> str:
    """Return the promote-and-create SQL for one admin account."""
    name_lit = _sql_literal(name)
    email_lit = _sql_literal(email)
    hash_lit = _sql_literal(password_hash)
    return (
        "-- 1) Promote an existing account (run this if the e-mail already exists)\n"
        f"UPDATE users SET role = 'ADMIN', refresh_token_valid_after = now() WHERE email = {email_lit};\n\n"
        "-- 2) Create the account from scratch (run this if it does not)\n"
        "INSERT INTO users (full_name, email, password_hash, role)\n"
        f"VALUES ({name_lit}, {email_lit}, {hash_lit}, 'ADMIN')\n"
        "ON CONFLICT (email) DO UPDATE\n"
        "    SET role = 'ADMIN', password_hash = EXCLUDED.password_hash,\n"
        "        refresh_token_valid_after = now();\n"
    )


def main() -> int:
    """Parse arguments, hash the password and print the SQL."""
    parser = argparse.ArgumentParser(description="Generate SQL to provision an administrator.")
    parser.add_argument("--name", required=True, help="Admin display name.")
    parser.add_argument("--email", required=True, help="Admin login e-mail.")
    parser.add_argument(
        "--password",
        default=os.getenv("ADMIN_PASSWORD"),
        help="Plain-text password (defaults to $ADMIN_PASSWORD or an interactive prompt).",
    )
    args = parser.parse_args()

    password = args.password
    if not password:
        password = getpass.getpass("Password: ")
    if not password:
        print("error: a password is required", file=sys.stderr)
        return 2

    password_hash = pwd_context.hash(password)
    print(build_sql(args.name, args.email, password_hash))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
