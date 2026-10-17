from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  initialized INTEGER NOT NULL DEFAULT 0,
  last_polled_at INTEGER,
  last_video_id TEXT,
  authorization_note TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_videos (
  video_id TEXT PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
  seen_at INTEGER NOT NULL,
  is_baseline INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'both',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  source_channel_id TEXT NOT NULL,
  source_title TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL UNIQUE,
  source_url TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',
  approved INTEGER NOT NULL DEFAULT 0,
  progress INTEGER NOT NULL DEFAULT 0,
  stage TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER,
  error TEXT,
  provider_job_id TEXT,
  upload_session_enc TEXT,
  upload_started_at INTEGER,
  media_path TEXT,
  thumbnail_path TEXT,
  youtube_id TEXT,
  job_type TEXT NOT NULL DEFAULT 'standard',
  clip_start REAL,
  clip_end REAL,
  parent_video_id TEXT,
  hook_summary TEXT,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  render_options TEXT DEFAULT '{}',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  is_secret INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
  id INTEGER PRIMARY KEY CHECK (id=1),
  credentials_enc TEXT NOT NULL,
  connected_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS user_oauth_tokens (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  credentials_enc TEXT NOT NULL,
  channel_title TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  connected_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  severity TEXT NOT NULL DEFAULT 'info',
  message TEXT NOT NULL,
  source_id INTEGER,
  job_id INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def initialize(self) -> None:
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Step 1: Small forward-compatible migrations for existing databases
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if columns:
                for name, declaration in (("source_id", "INTEGER REFERENCES sources(id) ON DELETE SET NULL"),
                                          ("source_title", "TEXT NOT NULL DEFAULT ''"),
                                          ("approved", "INTEGER NOT NULL DEFAULT 0"),
                                          ("stage", "TEXT NOT NULL DEFAULT 'queued'"),
                                          ("upload_session_enc", "TEXT"),
                                          ("upload_started_at", "INTEGER"),
                                          ("next_attempt_at", "INTEGER"),
                                          ("media_path", "TEXT"),
                                          ("thumbnail_path", "TEXT"),
                                          ("job_type", "TEXT NOT NULL DEFAULT 'standard'"),
                                          ("clip_start", "REAL"),
                                          ("clip_end", "REAL"),
                                          ("parent_video_id", "TEXT"),
                                          ("hook_summary", "TEXT"),
                                          ("user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
                                          ("render_options", "TEXT DEFAULT '{}'")):
                    if name not in columns:
                        conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
            
            source_columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)").fetchall()}
            if source_columns:
                for name, declaration in (("initialized", "INTEGER NOT NULL DEFAULT 0"),
                                          ("last_video_id", "TEXT"),
                                          ("authorization_note", "TEXT NOT NULL DEFAULT ''")):
                    if name not in source_columns:
                        conn.execute(f"ALTER TABLE sources ADD COLUMN {name} {declaration}")

            # Step 2: Create any missing tables (users, settings, oauth_tokens, user_oauth_tokens, events)
            conn.executescript(SCHEMA)

            # Step 3: Now that all columns are guaranteed to exist, create indexes on jobs
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(job_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id)")
        finally:
            conn.close()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.transaction(immediate=True) as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    def event(self, message: str, severity: str = "info", source_id: int | None = None,
              job_id: int | None = None) -> None:
        self.execute("INSERT INTO events(severity,message,source_id,job_id,created_at) VALUES(?,?,?,?,?)",
                     (severity, message[:2000], source_id, job_id, int(time.time())))

    def get_setting(self, key: str) -> tuple[str | None, bool]:
        row = self.one("SELECT value,is_secret FROM settings WHERE key=?", (key,))
        return (row["value"], bool(row["is_secret"])) if row else (None, False)

    def set_setting(self, key: str, value: str, is_secret: bool = False) -> None:
        now = int(time.time())
        self.execute("INSERT INTO settings(key,value,is_secret,updated_at) VALUES(?,?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value,is_secret=excluded.is_secret,updated_at=excluded.updated_at",
                     (key, value, int(is_secret), now))

    def claim_next_job(self) -> dict[str, Any] | None:
        """Claim exactly one approved queued/retry job under an IMMEDIATE transaction."""
        with self.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE approved=1 AND (status='queued' OR (status='retry' AND COALESCE(next_attempt_at,0)<=?)) "
                               "ORDER BY created_at,id LIMIT 1", (int(time.time()),)).fetchone()
            if not row:
                return None
            now = int(time.time())
            conn.execute("UPDATE jobs SET status='downloading',stage='downloading',attempts=attempts+1,next_attempt_at=NULL,updated_at=? WHERE id=?",
                         (now, row["id"]))
            return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM users WHERE username=?", (username,))

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM users WHERE id=?", (user_id,))

    def get_all_users(self) -> list[dict[str, Any]]:
        return self.all(
            "SELECT u.id, u.username, u.role, u.created_at, u.updated_at, "
            "t.channel_title, t.channel_id, CASE WHEN t.user_id IS NOT NULL THEN 1 ELSE 0 END AS has_channel "
            "FROM users u LEFT JOIN user_oauth_tokens t ON t.user_id=u.id ORDER BY u.created_at ASC"
        )

    def get_user_oauth(self, user_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM user_oauth_tokens WHERE user_id=?", (user_id,))

    def set_user_oauth(self, user_id: int, credentials_enc: str, channel_title: str = "", channel_id: str = "") -> None:
        now = int(time.time())
        self.execute(
            "INSERT INTO user_oauth_tokens(user_id, credentials_enc, channel_title, channel_id, connected_at, updated_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET credentials_enc=excluded.credentials_enc, "
            "channel_title=excluded.channel_title, channel_id=excluded.channel_id, updated_at=excluded.updated_at",
            (user_id, credentials_enc, channel_title.strip()[:200], channel_id.strip()[:100], now, now)
        )

    def delete_user_oauth(self, user_id: int) -> None:
        self.execute("DELETE FROM user_oauth_tokens WHERE user_id=?", (user_id,))

    def create_user(self, username: str, password_hash: str, role: str = "both") -> int:
        now = int(time.time())
        return self.execute("INSERT INTO users(username, password_hash, role, created_at, updated_at) VALUES(?,?,?,?,?)",
                            (username.strip(), password_hash, role, now, now))

    def delete_user(self, user_id: int) -> bool:
        return bool(self.execute("DELETE FROM users WHERE id=?", (user_id,)))

    def update_user_password(self, user_id: int, password_hash: str) -> None:
        now = int(time.time())
        self.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?", (password_hash, now, user_id))

    def update_user_role(self, user_id: int, role: str) -> None:
        now = int(time.time())
        self.execute("UPDATE users SET role=?, updated_at=? WHERE id=?", (role, now, user_id))


def encode_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
