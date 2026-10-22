"""SQLite persistence for recordings and their transcripts. Audio lives on disk; everything else lives here."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT    NOT NULL,
    notes            TEXT    NOT NULL DEFAULT '',
    filename         TEXT    NOT NULL UNIQUE,
    file_format      TEXT    NOT NULL,
    sample_rate      INTEGER NOT NULL,
    channels         INTEGER NOT NULL,
    status           TEXT    NOT NULL CHECK (status IN ('recording', 'completed', 'interrupted')),
    started_at       TEXT    NOT NULL,
    ended_at         TEXT,
    duration_seconds REAL    NOT NULL DEFAULT 0,
    size_bytes       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_recordings_started_at ON recordings (started_at);

-- One transcript per recording. `text` is the full transcript, kept for search and copying.
CREATE TABLE IF NOT EXISTS transcripts (
    recording_id INTEGER PRIMARY KEY REFERENCES recordings (id) ON DELETE CASCADE,
    status       TEXT    NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    model        TEXT,
    language     TEXT,
    text         TEXT    NOT NULL DEFAULT '',
    error        TEXT,
    queued_at    TEXT    NOT NULL,
    started_at   TEXT,
    completed_at TEXT
);

-- Timestamped pieces of a transcript, so the UI can jump to that point in the audio.
CREATE TABLE IF NOT EXISTS transcript_segments (
    recording_id  INTEGER NOT NULL REFERENCES transcripts (recording_id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    start_seconds REAL    NOT NULL,
    end_seconds   REAL    NOT NULL,
    text          TEXT    NOT NULL,
    PRIMARY KEY (recording_id, seq)
);
"""

EDITABLE_FIELDS = {"title", "notes"}

RECORDING_SELECT = """
    SELECT r.*, t.status AS transcript_status, t.error AS transcript_error
    FROM recordings r LEFT JOIN transcripts t ON t.recording_id = r.id
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        # A fresh connection per operation keeps us safe across Flask's and the transcriber's threads.
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # --- Recordings -------------------------------------------------------

    def create_recording(self, title, filename, file_format, sample_rate, channels) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO recordings (title, filename, file_format, sample_rate, channels, status, started_at)
                   VALUES (?, ?, ?, ?, ?, 'recording', ?)""",
                (title, filename, file_format, sample_rate, channels, utc_now()),
            )
            return cur.lastrowid

    def finish_recording(self, recording_id, status, duration_seconds, size_bytes) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE recordings SET status = ?, ended_at = ?, duration_seconds = ?, size_bytes = ?
                   WHERE id = ?""",
                (status, utc_now(), duration_seconds, size_bytes, recording_id),
            )

    def unfinished_recordings(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM recordings WHERE status = 'recording'").fetchall()
        return [dict(r) for r in rows]

    def list_recordings(self, query: str | None = None) -> list[dict]:
        sql, params = RECORDING_SELECT, ()
        if query:
            sql += " WHERE r.title LIKE ?1 ESCAPE '\\' OR r.notes LIKE ?1 ESCAPE '\\' OR t.text LIKE ?1 ESCAPE '\\'"
            params = (_like_pattern(query),)
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY r.started_at DESC, r.id DESC", params).fetchall()
        return [dict(r) for r in rows]

    def get_recording(self, recording_id) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(RECORDING_SELECT + " WHERE r.id = ?", (recording_id,)).fetchone()
        return dict(row) if row else None

    def update_recording(self, recording_id, **fields) -> dict | None:
        fields = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            with self._connect() as conn:
                conn.execute(f"UPDATE recordings SET {assignments} WHERE id = ?", (*fields.values(), recording_id))
        return self.get_recording(recording_id)

    def delete_recording(self, recording_id) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))

    # --- Transcripts ------------------------------------------------------

    def queue_transcript(self, recording_id) -> None:
        """Create (or reset) the transcript row so it waits for the transcriber."""
        with self._connect() as conn:
            conn.execute("DELETE FROM transcripts WHERE recording_id = ?", (recording_id,))
            conn.execute(
                "INSERT INTO transcripts (recording_id, status, queued_at) VALUES (?, 'queued', ?)",
                (recording_id, utc_now()),
            )

    def mark_transcript_running(self, recording_id) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE transcripts SET status = 'running', started_at = ? WHERE recording_id = ?",
                (utc_now(), recording_id),
            )

    def complete_transcript(self, recording_id, model, language, segments) -> bool:
        """Store the result. Returns False (storing nothing) if the recording was deleted meanwhile."""
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE transcripts SET status = 'completed', model = ?, language = ?, text = ?, error = NULL,
                          completed_at = ?
                   WHERE recording_id = ?""",
                (model, language, " ".join(s.text for s in segments), utc_now(), recording_id),
            )
            if cur.rowcount:
                conn.execute("DELETE FROM transcript_segments WHERE recording_id = ?", (recording_id,))
                conn.executemany(
                    """INSERT INTO transcript_segments (recording_id, seq, start_seconds, end_seconds, text)
                       VALUES (?, ?, ?, ?, ?)""",
                    [(recording_id, i, s.start, s.end, s.text) for i, s in enumerate(segments)],
                )
            return cur.rowcount > 0

    def fail_transcript(self, recording_id, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE transcripts SET status = 'failed', error = ?, completed_at = ? WHERE recording_id = ?",
                (error, utc_now(), recording_id),
            )

    def get_transcript(self, recording_id) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM transcripts WHERE recording_id = ?", (recording_id,)).fetchone()
            if row is None:
                return None
            segments = conn.execute(
                """SELECT start_seconds AS start, end_seconds AS end, text FROM transcript_segments
                   WHERE recording_id = ? ORDER BY seq""",
                (recording_id,),
            ).fetchall()
        return {**dict(row), "segments": [dict(s) for s in segments]}

    def recordings_needing_transcription(self) -> list[int]:
        """Finished recordings with no transcript yet, or whose transcription never finished (newest first)."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT r.id FROM recordings r LEFT JOIN transcripts t ON t.recording_id = r.id
                   WHERE r.status != 'recording' AND (t.recording_id IS NULL OR t.status IN ('queued', 'running'))
                   ORDER BY r.started_at DESC"""
            ).fetchall()
        return [r["id"] for r in rows]
