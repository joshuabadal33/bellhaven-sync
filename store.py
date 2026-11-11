"""SQLite store for proposals and reviewer decisions.

This is what makes re-runs safe. Every proposal has a stable key built from
what it would change. Once a key is approved or rejected it is never proposed
again. If the underlying data changes, the key changes, and it comes back.
"""
import json
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    key           TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    account_id    TEXT,
    location_slug TEXT,
    confidence    TEXT,
    body          TEXT NOT NULL,          -- full proposal as JSON
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | failed
    created_at    TEXT NOT NULL,
    decided_at    TEXT,
    reviewer_note TEXT,
    result        TEXT                    -- what the API returned / error text
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    locations   INTEGER,
    accounts    INTEGER,
    new_proposals INTEGER,
    skipped_decided INTEGER,
    confident_matches INTEGER
);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def connect(path=config.DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def sync_proposals(conn, proposals):
    """Store this run's proposals. Returns (new, skipped_because_already_decided)."""
    decided = {r["key"] for r in conn.execute("SELECT key FROM proposals WHERE status IN ('approved','rejected')")}
    fresh = [p for p in proposals if p["key"] not in decided]

    # Pending items that this run no longer produces are stale (the data moved on).
    keep = [p["key"] for p in fresh]
    conn.execute(
        f"DELETE FROM proposals WHERE status IN ('pending','failed') AND key NOT IN ({','.join('?' * len(keep)) or 'NULL'})",
        keep,
    )

    new = 0
    for p in fresh:
        # Still-open items get this run's evidence and write-time checks.
        conn.execute("UPDATE proposals SET title=?, confidence=?, body=? WHERE key=? AND status IN ('pending','failed')",
                     (p["title"], p["confidence"], json.dumps(p), p["key"]))
        cur = conn.execute(
            """INSERT OR IGNORE INTO proposals
               (key, kind, title, account_id, location_slug, confidence, body, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (p["key"], p["kind"], p["title"], p["account_id"], p["location_slug"],
             p["confidence"], json.dumps(p), now()),
        )
        new += cur.rowcount
    conn.commit()
    return new, len(proposals) - len(fresh)


def pending(conn):
    return [_row(r) for r in conn.execute(
        "SELECT * FROM proposals WHERE status IN ('pending','failed') ORDER BY kind, title")]


def decided(conn, limit=200):
    return [_row(r) for r in conn.execute(
        "SELECT * FROM proposals WHERE status IN ('approved','rejected') ORDER BY decided_at DESC LIMIT ?", (limit,))]


def get(conn, key):
    r = conn.execute("SELECT * FROM proposals WHERE key = ?", (key,)).fetchone()
    return _row(r) if r else None


def mark(conn, key, status, reviewer_note="", result=""):
    conn.execute(
        "UPDATE proposals SET status=?, decided_at=?, reviewer_note=?, result=? WHERE key=?",
        (status, now(), reviewer_note, result, key),
    )
    conn.commit()


def log_run(conn, **stats):
    conn.execute(
        "INSERT INTO runs (started_at, locations, accounts, new_proposals, skipped_decided, confident_matches) "
        "VALUES (?,?,?,?,?,?)",
        (now(), stats["locations"], stats["accounts"], stats["new"], stats["skipped"], stats["matches"]),
    )
    conn.commit()


def last_run(conn):
    r = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def _row(r):
    d = dict(r)
    d["body"] = json.loads(d["body"])
    return d
