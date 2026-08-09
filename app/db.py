"""Persistance SQLite : source de verite du pipeline.

Chaque video est une ligne avec un etat. Toute etape lit les lignes dans l'etat
N et les fait passer a N+1 en committant immediatement. Consequence : un crash,
un `Ctrl+C` ou un redemarrage du serveur ne perd rien et ne fait rien repayer.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .config import settings
from .models import JobStatus, VideoState

_lock = threading.RLock()
_local = threading.local()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    status            TEXT NOT NULL,
    accounts_json     TEXT NOT NULL,
    scrape_json       TEXT NOT NULL,
    generation_json   TEXT,
    max_spend_usd     REAL NOT NULL DEFAULT 50,
    spent_usd         REAL NOT NULL DEFAULT 0,
    error             TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    id                TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    platform          TEXT NOT NULL,
    account           TEXT NOT NULL,
    external_id       TEXT NOT NULL,
    post_url          TEXT,
    source_url        TEXT,
    caption           TEXT,
    thumbnail_url     TEXT,
    view_count        INTEGER DEFAULT 0,
    like_count        INTEGER DEFAULT 0,
    posted_at         TEXT,

    duration_s        REAL,
    width             INTEGER,
    height            INTEGER,

    local_path        TEXT,
    frame_path        TEXT,
    edited_path       TEXT,
    output_path       TEXT,

    kling_task_id     TEXT,

    state             TEXT NOT NULL,
    selected          INTEGER NOT NULL DEFAULT 1,
    attempts          INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    error_kind        TEXT,
    cost_usd          REAL NOT NULL DEFAULT 0,

    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,

    UNIQUE (job_id, platform, external_id)
);

CREATE INDEX IF NOT EXISTS idx_videos_job_state ON videos(job_id, state);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT,
    video_id  TEXT,
    level     TEXT NOT NULL,
    message   TEXT NOT NULL,
    ts        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);

CREATE TABLE IF NOT EXISTS references_img (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    path        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    data_json   TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

-- Batchs Gemini. Un batch soumis est deja facture : on persiste son nom pour
-- reprendre le polling apres un redemarrage au lieu de resoumettre.
CREATE TABLE IF NOT EXISTS batches (
    id             TEXT PRIMARY KEY,
    job_id         TEXT NOT NULL,
    provider_name  TEXT,
    state          TEXT NOT NULL,
    video_ids_json TEXT NOT NULL,
    cost_usd       REAL NOT NULL DEFAULT 0,
    error          TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_job ON batches(job_id, state);
"""


def _conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init() -> None:
    with _lock:
        _conn().executescript(SCHEMA)
        _conn().commit()


def _now() -> float:
    return time.time()


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def create_job(name: str, accounts: list[str], scrape: dict, max_spend: float) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _conn().execute(
            """INSERT INTO jobs (id, name, status, accounts_json, scrape_json,
                                 max_spend_usd, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                job_id,
                name,
                JobStatus.DRAFT,
                json.dumps(accounts),
                json.dumps(scrape),
                max_spend,
                _now(),
                _now(),
            ),
        )
        _conn().commit()
    return job_id


def get_job(job_id: str) -> dict | None:
    cur = _conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,))
    row = cur.fetchone()
    if not row:
        return None
    job = dict(row)
    job["accounts"] = json.loads(job.pop("accounts_json"))
    job["scrape"] = json.loads(job.pop("scrape_json"))
    gen = job.pop("generation_json")
    job["generation"] = json.loads(gen) if gen else None
    return job


def list_jobs() -> list[dict]:
    cur = _conn().execute(
        """SELECT j.*,
                  (SELECT COUNT(*) FROM videos v WHERE v.job_id=j.id) AS n_videos,
                  (SELECT COUNT(*) FROM videos v WHERE v.job_id=j.id AND v.state='done')
                      AS n_done
           FROM jobs j ORDER BY j.created_at DESC"""
    )
    out = []
    for row in _rows(cur):
        row.pop("accounts_json", None)
        row.pop("scrape_json", None)
        row.pop("generation_json", None)
        out.append(row)
    return out


def update_job(job_id: str, **fields) -> None:
    if not fields:
        return
    if "generation" in fields:
        fields["generation_json"] = json.dumps(fields.pop("generation"))
    sets = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _conn().execute(
            f"UPDATE jobs SET {sets}, updated_at=? WHERE id=?",
            (*fields.values(), _now(), job_id),
        )
        _conn().commit()


def delete_job(job_id: str) -> None:
    with _lock:
        _conn().execute("DELETE FROM videos WHERE job_id=?", (job_id,))
        _conn().execute("DELETE FROM events WHERE job_id=?", (job_id,))
        _conn().execute("DELETE FROM jobs WHERE id=?", (job_id,))
        _conn().commit()


def add_spend(job_id: str, amount: float) -> float:
    """Incremente la depense et renvoie le total. Atomique."""
    with _lock:
        _conn().execute(
            "UPDATE jobs SET spent_usd = spent_usd + ?, updated_at=? WHERE id=?",
            (amount, _now(), job_id),
        )
        _conn().commit()
        cur = _conn().execute("SELECT spent_usd FROM jobs WHERE id=?", (job_id,))
        row = cur.fetchone()
        return row["spent_usd"] if row else 0.0


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------

_VIDEO_FIELDS = (
    "platform account external_id post_url source_url caption thumbnail_url "
    "view_count like_count posted_at duration_s width height"
).split()


def upsert_video(job_id: str, data: dict) -> str | None:
    """Insere une video decouverte. Renvoie None si deja presente (dedup)."""
    vid = uuid.uuid4().hex[:12]
    cols = ["id", "job_id", "state", "created_at", "updated_at"] + _VIDEO_FIELDS
    vals = [vid, job_id, VideoState.DISCOVERED, _now(), _now()] + [
        data.get(f) for f in _VIDEO_FIELDS
    ]
    placeholders = ",".join("?" * len(cols))
    with _lock:
        try:
            _conn().execute(
                f"INSERT INTO videos ({','.join(cols)}) VALUES ({placeholders})", vals
            )
            _conn().commit()
            return vid
        except sqlite3.IntegrityError:
            return None  # deja scrape lors d'un run precedent


def get_video(video_id: str) -> dict | None:
    cur = _conn().execute("SELECT * FROM videos WHERE id=?", (video_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def list_videos(
    job_id: str,
    states: Iterable[str] | None = None,
    selected_only: bool = False,
) -> list[dict]:
    sql = "SELECT * FROM videos WHERE job_id=?"
    params: list[Any] = [job_id]
    if states:
        states = list(states)
        sql += f" AND state IN ({','.join('?' * len(states))})"
        params += states
    if selected_only:
        sql += " AND selected=1"
    sql += " ORDER BY view_count DESC, created_at ASC"
    return _rows(_conn().execute(sql, params))


def update_video(video_id: str, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _conn().execute(
            f"UPDATE videos SET {sets}, updated_at=? WHERE id=?",
            (*fields.values(), _now(), video_id),
        )
        _conn().commit()


def set_state(video_id: str, state: str, **fields) -> None:
    update_video(video_id, state=state, **fields)


def set_selection(job_id: str, video_ids: list[str]) -> None:
    """Marque la selection de l'utilisateur ; le reste passe en SKIPPED."""
    with _lock:
        _conn().execute(
            "UPDATE videos SET selected=0, updated_at=? WHERE job_id=?",
            (_now(), job_id),
        )
        if video_ids:
            marks = ",".join("?" * len(video_ids))
            _conn().execute(
                f"UPDATE videos SET selected=1, updated_at=? "
                f"WHERE job_id=? AND id IN ({marks})",
                (_now(), job_id, *video_ids),
            )
        # Les non-selectionnes encore en attente sont ecartes definitivement.
        _conn().execute(
            "UPDATE videos SET state=?, updated_at=? "
            "WHERE job_id=? AND selected=0 AND state=?",
            (VideoState.SKIPPED, _now(), job_id, VideoState.DISCOVERED),
        )
        _conn().commit()


def bump_attempts(video_id: str) -> int:
    with _lock:
        _conn().execute(
            "UPDATE videos SET attempts = attempts + 1, updated_at=? WHERE id=?",
            (_now(), video_id),
        )
        _conn().commit()
        cur = _conn().execute("SELECT attempts FROM videos WHERE id=?", (video_id,))
        row = cur.fetchone()
        return row["attempts"] if row else 0


def job_stats(job_id: str) -> dict[str, int]:
    cur = _conn().execute(
        "SELECT state, COUNT(*) AS n FROM videos WHERE job_id=? GROUP BY state",
        (job_id,),
    )
    stats = {r["state"]: r["n"] for r in cur.fetchall()}
    cur = _conn().execute(
        "SELECT COUNT(*) AS n FROM videos WHERE job_id=? AND selected=1", (job_id,)
    )
    stats["selected"] = cur.fetchone()["n"]
    return stats


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


def log(job_id: str | None, message: str, level: str = "info",
        video_id: str | None = None) -> None:
    with _lock:
        _conn().execute(
            "INSERT INTO events (job_id, video_id, level, message, ts) VALUES (?,?,?,?,?)",
            (job_id, video_id, level, message, _now()),
        )
        _conn().commit()


def list_events(job_id: str, after_id: int = 0, limit: int = 200) -> list[dict]:
    return _rows(
        _conn().execute(
            "SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id ASC LIMIT ?",
            (job_id, after_id, limit),
        )
    )


# ---------------------------------------------------------------------------
# Images de reference
# ---------------------------------------------------------------------------


def add_reference(filename: str, path: Path) -> str:
    ref_id = uuid.uuid4().hex[:12]
    with _lock:
        _conn().execute(
            "INSERT INTO references_img (id, filename, path, created_at) VALUES (?,?,?,?)",
            (ref_id, filename, str(path), _now()),
        )
        _conn().commit()
    return ref_id


def set_reference_path(ref_id: str, path: Path) -> None:
    with _lock:
        _conn().execute(
            "UPDATE references_img SET path=? WHERE id=?", (str(path), ref_id)
        )
        _conn().commit()


def get_reference(ref_id: str) -> dict | None:
    cur = _conn().execute("SELECT * FROM references_img WHERE id=?", (ref_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def list_references() -> list[dict]:
    return _rows(
        _conn().execute("SELECT * FROM references_img ORDER BY created_at DESC")
    )


def delete_reference(ref_id: str) -> None:
    with _lock:
        _conn().execute("DELETE FROM references_img WHERE id=?", (ref_id,))
        _conn().commit()


# ---------------------------------------------------------------------------
# Preferences (parametres par defaut, reutilises a chaque nouveau job)
# ---------------------------------------------------------------------------


def get_preferences() -> dict:
    cur = _conn().execute("SELECT data_json FROM preferences WHERE id=1")
    row = cur.fetchone()
    return json.loads(row["data_json"]) if row else {}


def create_batch(job_id: str, video_ids: list[str], cost: float) -> str:
    batch_id = uuid.uuid4().hex[:12]
    with _lock:
        _conn().execute(
            """INSERT INTO batches (id, job_id, provider_name, state, video_ids_json,
                                    cost_usd, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (batch_id, job_id, None, "pending", json.dumps(video_ids), cost,
             _now(), _now()),
        )
        _conn().commit()
    return batch_id


def update_batch(batch_id: str, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _conn().execute(
            f"UPDATE batches SET {sets}, updated_at=? WHERE id=?",
            (*fields.values(), _now(), batch_id),
        )
        _conn().commit()


def get_batch(batch_id: str) -> dict | None:
    cur = _conn().execute("SELECT * FROM batches WHERE id=?", (batch_id,))
    row = cur.fetchone()
    if not row:
        return None
    batch = dict(row)
    batch["video_ids"] = json.loads(batch.pop("video_ids_json"))
    return batch


def open_batches(job_id: str) -> list[dict]:
    """Batchs encore en vol : a reprendre plutot qu'a resoumettre."""
    cur = _conn().execute(
        "SELECT * FROM batches WHERE job_id=? AND state IN ('pending','submitted') "
        "ORDER BY created_at ASC",
        (job_id,),
    )
    out = []
    for row in _rows(cur):
        row["video_ids"] = json.loads(row.pop("video_ids_json"))
        out.append(row)
    return out


def set_preferences(data: dict) -> None:
    with _lock:
        _conn().execute(
            "INSERT INTO preferences (id, data_json, updated_at) VALUES (1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json, "
            "updated_at=excluded.updated_at",
            (json.dumps(data), _now()),
        )
        _conn().commit()
