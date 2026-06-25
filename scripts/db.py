#!/usr/bin/env python3
"""
  +==============================================================+
  |  ||  || |||||| |||||| |||||   || ||||||| ||||||| |||||||    |
  |  ||  || |||  || |||  || |||||  || |||     |||     |||       |
  |  ||||||| ||||||| |||||| ||| || || |||||   ||||||| |||||||    |
  |  |||  || |||  || |||  || |||  |||| |||       |||||     |||   |
  |  |||  || |||  || |||  || |||   ||| ||||||| ||||||| |||||||    |
  |  |||  || |||  || |||  || |||   ||| ||||||| ||||||| |||||||    |
  |                                                              |
  |     created by Kuwabara Mai & Shanon                         |
  |     Kizuna (絆) — AI Agent Quality Control System               |
  +==============================================================+

Harness Memory Database -- SQLite + FTS5.

Multi-category table management + utility scoring + strategic forgetting + full-text search.
All hook scripts read/write memory through this module.

Usage:
    from db import HarnessDB
    db = HarnessDB()
    db.upsert_memory(category='anti_pattern', title='...', content='...')
    results = db.search('diagnose OR verify')
    hot = db.get_active(limit=10)
"""

import sqlite3
import os
import time
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB_PATH = Path.home() / ".claude" / "memory" / "harness.db"


class HarnessDB:
    """Encapsulates all harness memory database operations."""

    def __init__(self, path: Path | None = None):
        self.path = path or DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ═══════════════════════════════════════════════════════════════
    # Schema
    # ═══════════════════════════════════════════════════════════════

    def init_schema(self):
        """Create tables + FTS5 + triggers. Idempotent (IF NOT EXISTS)."""
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL CHECK (category IN (
                        'anti_pattern', 'effective_pattern', 'guardrail',
                        'knowledge', 'preference', 'decision', 'session_summary'
                    )),
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT DEFAULT '',
                    utility REAL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    access_count INTEGER DEFAULT 0,
                    session_id TEXT,
                    status TEXT DEFAULT 'active'
                        CHECK (status IN ('active', 'archived', 'cold'))
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT DEFAULT '',
                    timestamp TEXT NOT NULL,
                    tool_count INTEGER DEFAULT 0,
                    edit_count INTEGER DEFAULT 0,
                    skill_worthy INTEGER DEFAULT 0,
                    reviewed INTEGER DEFAULT 0,
                    summary TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS skill_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT DEFAULT '',
                    content TEXT NOT NULL,
                    status TEXT DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'rejected')),
                    session_id TEXT,
                    proposed_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    title, content, tags,
                    tokenize='unicode61 remove_diacritics 2',
                    content='memories', content_rowid='id'
                );
            """)

            # Triggers: keep FTS5 index in sync
            self._ensure_triggers(conn)

    def _ensure_triggers(self, conn):
        """Create FTS5 sync triggers (idempotent)."""
        conn.executescript("""
            DROP TRIGGER IF EXISTS memories_ai;
            DROP TRIGGER IF EXISTS memories_ad;
            DROP TRIGGER IF EXISTS memories_au;

            CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, title, content, tags)
                VALUES (new.id, new.title, new.content, new.tags);
            END;

            CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, title, content, tags)
                VALUES ('delete', old.id, old.title, old.content, old.tags);
            END;

            CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, title, content, tags)
                VALUES ('delete', old.id, old.title, old.content, old.tags);
                INSERT INTO memories_fts(rowid, title, content, tags)
                VALUES (new.id, new.title, new.content, new.tags);
            END;
        """)

    def upsert_memory(self, category: str, title: str, content: str,
                      tags: str = "", session_id: str = "",
                      utility: float = 1.0) -> int:
        """Insert or update memory entry. Dedup by category+title."""
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM memories WHERE category=? AND title=?",
                (category, title)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE memories
                       SET content=?, tags=?, utility=?, updated_at=?,
                           session_id=?
                       WHERE id=?""",
                    (content, tags, utility, now, session_id, existing["id"]),
                )
                return existing["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO memories
                       (category, title, content, tags, utility,
                        created_at, updated_at, session_id, status)
                       VALUES (?,?,?,?,?,?,?,?,'active')""",
                    (category, title, content, tags, utility, now, now, session_id),
                )
                return cur.lastrowid

    def get_by_category(self, category: str, status: str = "active",
                        limit: int = 20) -> list[dict]:
        """Get all active entries in a category."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE category=? AND status=?
                   ORDER BY utility DESC LIMIT ?""",
                (category, status, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_active(self, categories: list[str] | None = None,
                   min_utility: float = 0.0, limit: int = 30) -> list[dict]:
        """Get high-utility active entries, optionally filtered by category."""
        with self.connect() as conn:
            if categories:
                placeholders = ",".join("?" * len(categories))
                rows = conn.execute(
                    f"""SELECT * FROM memories
                        WHERE status='active' AND category IN ({placeholders})
                        AND utility >= ?
                        ORDER BY utility DESC LIMIT ?""",
                    (*categories, min_utility, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM memories
                       WHERE status='active' AND utility >= ?
                       ORDER BY utility DESC LIMIT ?""",
                    (min_utility, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    def touch(self, memory_id: int):
        """Record an access (utility++)."""
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """UPDATE memories
                   SET access_count = access_count + 1,
                       last_accessed_at = ?,
                       utility = utility + 0.1
                   WHERE id=?""", (now, memory_id),
            )

    def search(self, query: str, categories: list[str] | None = None,
               limit: int = 20) -> list[dict]:
        """Full-text search memory. FTS5 first, LIKE fallback."""
        with self.connect() as conn:
            # Try FTS5
            try:
                rows = conn.execute(
                    """SELECT m.* FROM memories m
                       JOIN memories_fts fts ON m.id = fts.rowid
                       WHERE memories_fts MATCH ?
                       ORDER BY m.utility DESC, rank LIMIT ?""",
                    (query, limit),
                ).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass

            # LIKE fallback
            like_q = f"%{query}%"
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE status='active'
                     AND (content LIKE ? OR title LIKE ? OR tags LIKE ?)
                   ORDER BY utility DESC LIMIT ?""",
                (like_q, like_q, like_q, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ═══════════════════════════════════════════════════════════════
    # Sessions
    # ═══════════════════════════════════════════════════════════════

    def upsert_session(self, session_id: str, **kwargs):
        """Write or update session record."""
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if existing:
                sets = ", ".join(f"{k}=?" for k in kwargs)
                conn.execute(
                    f"UPDATE sessions SET {sets} WHERE id=?",
                    (*kwargs.values(), session_id),
                )
            else:
                kwargs.setdefault("timestamp", now)
                cols = "id, " + ", ".join(kwargs.keys())
                placeholders = "?, " + ", ".join("?" * len(kwargs))
                conn.execute(
                    f"INSERT INTO sessions ({cols}) VALUES ({placeholders})",
                    (session_id, *kwargs.values()),
                )

    def get_unreviewed_sessions(self, limit: int = 10) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM sessions
                   WHERE reviewed=0
                   ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_skill_worthy_sessions(self, limit: int = 5) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM sessions
                   WHERE skill_worthy=1
                   ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ═══════════════════════════════════════════════════════════════
    # Skill Proposals
    # ═══════════════════════════════════════════════════════════════

    def get_pending_skills(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_proposals WHERE status='pending'"
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_skill_proposal(self, name: str, content: str,
                              description: str = "",
                              session_id: str = ""):
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM skill_proposals WHERE name=?", (name,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE skill_proposals
                       SET content=?, description=?, proposed_at=?
                       WHERE name=?""",
                    (content, description, now, name),
                )
            else:
                conn.execute(
                    """INSERT INTO skill_proposals
                       (name, description, content, status, session_id, proposed_at)
                       VALUES (?,?,?,'pending',?,?)""",
                    (name, description, content, session_id, now),
                )

    def resolve_skill(self, name: str, approved: bool):
        now = datetime.now(timezone.utc).isoformat()
        status = "approved" if approved else "rejected"
        with self.connect() as conn:
            conn.execute(
                "UPDATE skill_proposals SET status=?, resolved_at=? WHERE name=?",
                (status, now, name),
            )

    # ═══════════════════════════════════════════════════════════════
    # Maintenance: utility decay + strategic forgetting
    # ═══════════════════════════════════════════════════════════════

    def run_maintenance(self):
        """Utility decay + archive + cold storage + delete. Called at SessionStart."""
        now = datetime.now(timezone.utc)
        with self.connect() as conn:
            # 1. Utility decay: 0.85x per week
            conn.execute(
                """UPDATE memories
                   SET utility = utility * 0.85
                   WHERE status='active'
                     AND updated_at < ?""",
                ((now - timedelta(days=7)).isoformat(),),
            )

            # 2. Archive: utility < 0.5 and 30 days unaccessed
            conn.execute(
                """UPDATE memories
                   SET status='archived'
                   WHERE status='active'
                     AND utility < 0.5
                     AND (last_accessed_at IS NULL
                          OR last_accessed_at < ?)""",
                ((now - timedelta(days=30)).isoformat(),),
            )

            # 3. Cold storage: archived 90+ days
            conn.execute(
                """UPDATE memories
                   SET status='cold'
                   WHERE status='archived'
                     AND updated_at < ?""",
                ((now - timedelta(days=90)).isoformat(),),
            )

            # 4. Delete: cold storage 180+ days (session_summary only)
            conn.execute(
                """DELETE FROM memories
                   WHERE status='cold'
                     AND category='session_summary'
                     AND updated_at < ?""",
                ((now - timedelta(days=180)).isoformat(),),
            )

            # 5. Clean old sessions
            conn.execute(
                """DELETE FROM sessions
                   WHERE reviewed=1 AND timestamp < ?""",
                ((now - timedelta(days=60)).isoformat(),),
            )

            # 6. Clean rejected skill proposals
            conn.execute(
                """DELETE FROM skill_proposals
                   WHERE status='rejected'
                     AND resolved_at < ?""",
                ((now - timedelta(days=30)).isoformat(),),
            )

    def stats(self) -> dict:
        """Return database statistics for verification."""
        with self.connect() as conn:
            cats = conn.execute(
                "SELECT category, status, count(*) as cnt FROM memories GROUP BY 1, 2"
            ).fetchall()
            sess = conn.execute(
                "SELECT count(*) as cnt, sum(tool_count) as tools FROM sessions"
            ).fetchone()
            return {
                "memories_by_category": {f"{c['category']}/{c['status']}": c["cnt"] for c in cats},
                "total_sessions": sess["cnt"],
                "total_tool_calls": sess["tools"] or 0,
            }


# ─── Singleton ──────────────────────────────────────────────────────────
_db: HarnessDB | None = None


def get_db() -> HarnessDB:
    global _db
    if _db is None:
        _db = HarnessDB()
        _db.init_schema()
    return _db
