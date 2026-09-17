#!/usr/bin/env python3
"""Seed a deterministic AstrBot OpenAPI key into the local test DB.

AstrBot >= 4.28 stores OpenAPI keys in the ``api_keys`` SQLite table as a
PBKDF2 hash (``astrbot.dashboard.services.api_key_service``). There is no
``dashboard.api_key`` config field anymore, so ``docker/astrbot_init.sh``
starts this script in the background after AstrBot boots: it waits for the
table to exist, then upserts a key whose raw value is ``ASTRBOT_API_KEY``
(default ``admin``) so ``docker/e2e_verify.sh`` can authenticate.

Idempotent: re-running repairs scopes / revoked state instead of duplicating.

Pitfall fixed here: the ``scopes`` column is a JSON column. Storing the raw
string ``*`` makes SQLAlchemy fail on read with
``Expecting value: line 1 column 1 (char 0)`` for every authenticated route.
We always store a valid JSON array (``["*"]``).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone

DB_PATH = os.environ.get("ASTRBOT_DB_PATH", "/AstrBot/data/data_v4.db")
RAW_KEY = os.environ.get("ASTRBOT_API_KEY", "admin").strip()
KEY_NAME = os.environ.get("ASTRBOT_API_KEY_NAME", "tmemory-e2e")
SCOPES = ["*"]
WAIT_SECONDS = int(os.environ.get("ASTRBOT_API_KEY_SEED_TIMEOUT", "180"))


def hash_key(raw: str) -> str:
    """Return the PBKDF2 hash AstrBot uses for OpenAPI keys."""
    try:
        sys.path.insert(0, "/AstrBot")
        from astrbot.dashboard.services.api_key_service import ApiKeyService

        return ApiKeyService.hash_key(raw)
    except Exception:
        return hashlib.pbkdf2_hmac(
            "sha256",
            raw.encode("utf-8"),
            b"astrbot_api_key",
            100_000,
        ).hex()


def _table_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='api_keys'"
    ).fetchone()
    return row is not None


def wait_for_table() -> sqlite3.Connection:
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        if os.path.exists(DB_PATH):
            try:
                conn = sqlite3.connect(DB_PATH, timeout=10)
                conn.execute("PRAGMA busy_timeout=30000")
                if _table_ready(conn):
                    return conn
                conn.close()
            except sqlite3.Error:
                pass
        time.sleep(2)
    raise SystemExit(f"[seed] timeout waiting for api_keys table in {DB_PATH}")


def main() -> int:
    if not RAW_KEY:
        print("[seed] ASTRBOT_API_KEY is empty; skipping")
        return 0

    conn = wait_for_table()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    key_hash = hash_key(RAW_KEY)
    scopes_json = json.dumps(SCOPES)

    row = conn.execute(
        "SELECT key_id, scopes, revoked_at FROM api_keys WHERE key_hash=?",
        (key_hash,),
    ).fetchone()
    if row:
        key_id, stored_scopes, revoked_at = row
        if stored_scopes == scopes_json and revoked_at is None:
            print(f"[seed] ✅ OpenAPI key already present (key_id={key_id})")
        else:
            conn.execute(
                "UPDATE api_keys SET scopes=?, revoked_at=NULL, expires_at=NULL, "
                "updated_at=? WHERE key_id=?",
                (scopes_json, now, key_id),
            )
            print(f"[seed] ✅ OpenAPI key repaired (key_id={key_id})")
    else:
        key_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO api_keys (created_at, updated_at, key_id, name, key_hash, "
            "key_prefix, scopes, created_by, last_used_at, expires_at, revoked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                now,
                now,
                key_id,
                KEY_NAME,
                key_hash,
                RAW_KEY[:12],
                scopes_json,
                "tmemory-init",
                None,
                None,
                None,
            ),
        )
        print(f"[seed] ✅ OpenAPI key seeded (name={KEY_NAME}, key_id={key_id})")

    conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
