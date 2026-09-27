"""Надёжное хранение: Postgres (Render) или локальный SQLite.

Таблицы:
  - corpus_image_hash — база эталонов
  - check_jobs — результаты проверок (JSON)
  - thumbnails — миниатюры картинок
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from image_pipeline.core import (
    DEFAULT_DB,
    CorpusEntry,
    phash_from_hex,
    phash_to_hex,
)

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS corpus_image_hash (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phash64 TEXT NOT NULL,
    source_task_id TEXT NOT NULL,
    source_document_id TEXT NOT NULL,
    sha256 TEXT DEFAULT '',
    byte_size INTEGER DEFAULT 0,
    thumb_path TEXT DEFAULT '',
    ocr_text TEXT DEFAULT '',
    indexed_at TEXT DEFAULT (datetime('now')),
    UNIQUE (phash64, source_document_id)
);
CREATE INDEX IF NOT EXISTS idx_corpus_phash ON corpus_image_hash(phash64);
CREATE TABLE IF NOT EXISTS check_jobs (
    job_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS thumbnails (
    thumb_key TEXT PRIMARY KEY,
    data BLOB NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

_PG_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS corpus_image_hash (
        id SERIAL PRIMARY KEY,
        phash64 TEXT NOT NULL,
        source_task_id TEXT NOT NULL,
        source_document_id TEXT NOT NULL,
        sha256 TEXT DEFAULT '',
        byte_size INTEGER DEFAULT 0,
        thumb_path TEXT DEFAULT '',
        ocr_text TEXT DEFAULT '',
        indexed_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (phash64, source_document_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_corpus_phash ON corpus_image_hash(phash64)",
    """
    CREATE TABLE IF NOT EXISTS check_jobs (
        job_id TEXT PRIMARY KEY,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS thumbnails (
        thumb_key TEXT PRIMARY KEY,
        data BYTEA NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
]


def _normalize_dsn(dsn: str) -> str:
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://") :]
    # Внешний хост Render требует SSL; внутренний — обычно без.
    if "render.com" in dsn and "sslmode=" not in dsn:
        dsn += ("&" if "?" in dsn else "?") + "sslmode=require"
    return dsn


def database_url() -> str | None:
    raw = os.environ.get("DATABASE_URL", "").strip()
    return _normalize_dsn(raw) if raw else None


class PersistentStore:
    """API как у CorpusStore + jobs/thumbs."""

    def __init__(self, db_path: Path | None = None, dsn: str | None = None):
        self.dsn = dsn if dsn is not None else database_url()
        self.db_path = Path(db_path or DEFAULT_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.backend = "postgres" if self.dsn else "sqlite"
        self._init_schema()

    def _pg(self):
        import psycopg

        return psycopg.connect(self.dsn, connect_timeout=15)

    def _init_schema(self) -> None:
        if self.backend == "postgres":
            with self._pg() as conn:
                with conn.cursor() as cur:
                    for stmt in _PG_STATEMENTS:
                        cur.execute(stmt)
                conn.commit()
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SQLITE_SCHEMA)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(corpus_image_hash)").fetchall()}
            if "ocr_text" not in cols:
                conn.execute("ALTER TABLE corpus_image_hash ADD COLUMN ocr_text TEXT DEFAULT ''")

    def add_entry(self, entry: CorpusEntry) -> None:
        vals = (
            phash_to_hex(entry.phash64),
            entry.source_task_id,
            entry.source_document_id,
            entry.sha256,
            entry.byte_size,
            entry.thumb_path,
            entry.ocr_text or "",
        )
        if self.backend == "postgres":
            with self._pg() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO corpus_image_hash
                        (phash64, source_task_id, source_document_id, sha256, byte_size, thumb_path, ocr_text)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (phash64, source_document_id) DO NOTHING
                        """,
                        vals,
                    )
                conn.commit()
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO corpus_image_hash
                (phash64, source_task_id, source_document_id, sha256, byte_size, thumb_path, ocr_text)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                vals,
            )

    def iter_entries(self, exclude_task_id: str | None = None) -> list[CorpusEntry]:
        if self.backend == "postgres":
            with self._pg() as conn:
                with conn.cursor() as cur:
                    if exclude_task_id:
                        cur.execute(
                            """
                            SELECT phash64, source_task_id, source_document_id, sha256, byte_size, thumb_path, ocr_text
                            FROM corpus_image_hash WHERE source_task_id != %s
                            """,
                            (exclude_task_id,),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT phash64, source_task_id, source_document_id, sha256, byte_size, thumb_path, ocr_text
                            FROM corpus_image_hash
                            """
                        )
                    rows = cur.fetchall()
            return [
                CorpusEntry(
                    phash64=phash_from_hex(r[0]),
                    source_task_id=r[1],
                    source_document_id=r[2],
                    sha256=r[3] or "",
                    byte_size=r[4] or 0,
                    thumb_path=r[5] or "",
                    ocr_text=r[6] or "",
                )
                for r in rows
            ]

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if exclude_task_id:
                rows = conn.execute(
                    """
                    SELECT phash64, source_task_id, source_document_id, sha256, byte_size, thumb_path, ocr_text
                    FROM corpus_image_hash WHERE source_task_id != ?
                    """,
                    (exclude_task_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT phash64, source_task_id, source_document_id, sha256, byte_size, thumb_path, ocr_text
                    FROM corpus_image_hash
                    """
                ).fetchall()
        return [
            CorpusEntry(
                phash64=phash_from_hex(r["phash64"]),
                source_task_id=r["source_task_id"],
                source_document_id=r["source_document_id"],
                sha256=r["sha256"] or "",
                byte_size=r["byte_size"] or 0,
                thumb_path=r["thumb_path"] or "",
                ocr_text=r["ocr_text"] or "",
            )
            for r in rows
        ]

    def count(self) -> int:
        if self.backend == "postgres":
            with self._pg() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM corpus_image_hash")
                    row = cur.fetchone()
            return int(row[0]) if row else 0
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM corpus_image_hash").fetchone()
        return int(row[0]) if row else 0

    def save_job(self, job_id: str, payload: dict[str, Any]) -> None:
        if self.backend == "postgres":
            with self._pg() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO check_jobs (job_id, payload)
                        VALUES (%s, %s::jsonb)
                        ON CONFLICT (job_id) DO UPDATE SET payload = EXCLUDED.payload
                        """,
                        (job_id, json.dumps(payload, ensure_ascii=False)),
                    )
                conn.commit()
            return
        blob = json.dumps(payload, ensure_ascii=False)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO check_jobs (job_id, payload) VALUES (?, ?)
                ON CONFLICT(job_id) DO UPDATE SET payload = excluded.payload
                """,
                (job_id, blob),
            )

    def load_job(self, job_id: str) -> Optional[dict[str, Any]]:
        if self.backend == "postgres":
            with self._pg() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT payload FROM check_jobs WHERE job_id = %s", (job_id,))
                    row = cur.fetchone()
            if not row:
                return None
            payload = row[0]
            return payload if isinstance(payload, dict) else json.loads(payload)

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload FROM check_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if not row:
            return None
        return json.loads(row[0])

    def save_thumb(self, thumb_key: str, data: bytes) -> str:
        key = thumb_key.replace("\\", "/").lstrip("/")
        if self.backend == "postgres":
            with self._pg() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO thumbnails (thumb_key, data)
                        VALUES (%s, %s)
                        ON CONFLICT (thumb_key) DO UPDATE SET data = EXCLUDED.data
                        """,
                        (key, data),
                    )
                conn.commit()
        else:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO thumbnails (thumb_key, data) VALUES (?, ?)
                    ON CONFLICT(thumb_key) DO UPDATE SET data = excluded.data
                    """,
                    (key, data),
                )
        return f"db:{key}"

    def get_thumb(self, thumb_key: str) -> Optional[bytes]:
        key = thumb_key.replace("\\", "/").lstrip("/")
        if key.startswith("db:"):
            key = key[3:]
        if self.backend == "postgres":
            with self._pg() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT data FROM thumbnails WHERE thumb_key = %s", (key,))
                    row = cur.fetchone()
            return bytes(row[0]) if row else None
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT data FROM thumbnails WHERE thumb_key = ?", (key,)
            ).fetchone()
        return bytes(row[0]) if row else None


def storage_info() -> dict[str, Any]:
    dsn = database_url()
    if not dsn:
        return {"backend": "sqlite", "persistent": False, "note": "локальный файл"}
    host = urlparse(dsn).hostname or ""
    return {
        "backend": "postgres",
        "persistent": True,
        "host": host,
        "note": "Render Postgres",
    }
