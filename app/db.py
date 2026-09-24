"""SQLite persistence layer (standard library only).

The database path comes from the DATABASE_PATH environment variable
(default: ./app.db). Schema is created idempotently at startup, so a
single command handles both dependency install and DB initialization.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .grid import format_rational

DATABASE_PATH = os.environ.get("DATABASE_PATH", "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS measures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    target_duration TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS voices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    measure_id INTEGER NOT NULL REFERENCES measures(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    position   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    voice_id     INTEGER NOT NULL REFERENCES voices(id) ON DELETE CASCADE,
    event_key    TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('sound', 'rest')),
    duration     TEXT NOT NULL,
    start_offset TEXT NOT NULL,
    end_offset   TEXT NOT NULL,
    position     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_voices_measure ON voices(measure_id);
CREATE INDEX IF NOT EXISTS idx_events_voice ON events(voice_id);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create the schema (and parent directory) if needed."""
    path = Path(DATABASE_PATH)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as conn, conn:
        conn.executescript(SCHEMA)


def insert_measure(target: str, voices: list[dict]) -> int:
    """Persist a validated measure with its voices and events.

    ``voices`` items: {"name": str, "events": [built events from
    grid.build_voice_events]}. Returns the new measure id.
    """
    with closing(connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO measures (target_duration) VALUES (?)", (target,)
        )
        measure_id = cursor.lastrowid
        for voice_position, voice in enumerate(voices):
            voice_cursor = conn.execute(
                "INSERT INTO voices (measure_id, name, position) VALUES (?, ?, ?)",
                (measure_id, voice["name"], voice_position),
            )
            voice_id = voice_cursor.lastrowid
            conn.executemany(
                """
                INSERT INTO events
                    (voice_id, event_key, kind, duration,
                     start_offset, end_offset, position)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        voice_id,
                        event["id"],
                        event["kind"],
                        format_rational(event["duration"]),
                        format_rational(event["start"]),
                        format_rational(event["end"]),
                        event["position"],
                    )
                    for event in voice["events"]
                ],
            )
        return measure_id


def _load_measure_row(conn: sqlite3.Connection, measure: sqlite3.Row) -> dict:
    """Build a measure dict (voices + events, ordered by position)."""
    voices = conn.execute(
        "SELECT id, name, position FROM voices "
        "WHERE measure_id = ? ORDER BY position",
        (measure["id"],),
    ).fetchall()
    result_voices = []
    for voice in voices:
        events = conn.execute(
            "SELECT event_key, kind, duration, start_offset, end_offset "
            "FROM events WHERE voice_id = ? ORDER BY position",
            (voice["id"],),
        ).fetchall()
        result_voices.append(
            {
                "name": voice["name"],
                "events": [
                    {
                        "id": event["event_key"],
                        "kind": event["kind"],
                        "duration": event["duration"],
                        "start": event["start_offset"],
                        "end": event["end_offset"],
                    }
                    for event in events
                ],
            }
        )
    return {
        "id": measure["id"],
        "target_duration": measure["target_duration"],
        "created_at": measure["created_at"],
        "voices": result_voices,
    }


def load_measure(measure_id: int) -> dict | None:
    """Load a measure with voices and events, ordered by position."""
    with closing(connect()) as conn:
        measure = conn.execute(
            "SELECT id, target_duration, created_at FROM measures WHERE id = ?",
            (measure_id,),
        ).fetchone()
        if measure is None:
            return None
        return _load_measure_row(conn, measure)


def load_all_measures() -> list[dict]:
    """Load every saved measure (and its voices), ordered by measure id."""
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, target_duration, created_at FROM measures ORDER BY id"
        ).fetchall()
        return [_load_measure_row(conn, row) for row in rows]
