"""SQLite database layer for ToTag Metadata Manager."""

from __future__ import annotations

import re
import sqlite3
import json
from pathlib import Path
import os
import sys
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATABASE_PATH


def get_connection() -> sqlite3.Connection:
    """Get a database connection with row factory."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database schema."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS assets (
            path TEXT UNIQUE PRIMARY KEY,
            filename TEXT,
            folder TEXT,
            media_type TEXT,
            thumb_path TEXT,
            capture_date TEXT,
            file_mtime REAL,
            title TEXT,
            description TEXT,
            keywords TEXT,
            is_dirty INTEGER DEFAULT 0,
            gps_lat REAL,
            gps_lon REAL,
            camera_make TEXT,
            camera_model TEXT,
            lens_make TEXT,
            lens_model TEXT,
            lens_id TEXT,
            focal_length_mm REAL,
            focal_length_35mm_mm REAL,
            aperture_f_number REAL,
            technical_metadata_scanned_at TEXT,
            technical_metadata_error TEXT
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_folder ON assets(folder)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_media_type ON assets(media_type)
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            status TEXT NOT NULL,
            label TEXT,
            total INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            current_label TEXT,
            payload_json TEXT,
            result_json TEXT,
            cancel_requested INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            level TEXT DEFAULT 'info',
            message TEXT NOT NULL,
            progress REAL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_events_job_seq ON job_events(job_id, seq)")

    # Migrate existing DB — add new columns if not present
    for col, defn in [
        ("gps_lat", "REAL"),
        ("gps_lon", "REAL"),
        ("camera_make", "TEXT"),
        ("camera_model", "TEXT"),
        ("lens_make", "TEXT"),
        ("lens_model", "TEXT"),
        ("lens_id", "TEXT"),
        ("focal_length_mm", "REAL"),
        ("focal_length_35mm_mm", "REAL"),
        ("aperture_f_number", "REAL"),
        ("technical_metadata_scanned_at", "TEXT"),
        ("technical_metadata_error", "TEXT"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE assets ADD COLUMN {col} {defn}")
        except Exception:
            pass  # column already exists

    conn.commit()
    conn.close()


def row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a dictionary."""
    if row is None:
        return None
    d = dict(row)
    for json_col in ("keywords",):
        if d.get(json_col):
            try:
                d[json_col] = json.loads(d[json_col])
            except (json.JSONDecodeError, TypeError):
                d[json_col] = []
        else:
            d[json_col] = []
    return d


def _utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _job_row_to_dict(row: sqlite3.Row) -> dict:
    if row is None:
        return None
    d = dict(row)
    for key in ("payload_json", "result_json"):
        raw = d.get(key)
        if raw:
            try:
                d[key.replace("_json", "")] = json.loads(raw)
            except Exception:
                d[key.replace("_json", "")] = None
        else:
            d[key.replace("_json", "")] = None
    return d


def create_job(job_id: str, job_type: str, label: str, payload: dict, total: int = 0) -> dict:
    now = _utc_now()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO jobs
        (id, type, status, label, total, done, failed, current_label, payload_json, result_json,
         cancel_requested, created_at, updated_at)
        VALUES (?, ?, 'queued', ?, ?, 0, 0, '', ?, NULL, 0, ?, ?)
    """, (job_id, job_type, label, total, json.dumps(payload), now, now))
    conn.commit()
    cursor.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    conn.close()
    return _job_row_to_dict(row)


def update_job(job_id: str, **fields):
    allowed = {
        "status", "label", "total", "done", "failed", "current_label",
        "payload_json", "result_json", "cancel_requested", "started_at", "finished_at",
    }
    updates, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        updates.append(f"{key} = ?")
        values.append(json.dumps(value) if key in ("payload_json", "result_json") and value is not None else value)
    updates.append("updated_at = ?")
    values.append(_utc_now())
    values.append(job_id)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", values)
    conn.commit()
    conn.close()


def append_paths_to_matching_job(job_type: str, paths: list[str], match_payload: dict, label_prefix: str) -> tuple[dict | None, list[str]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM jobs
        WHERE type = ?
          AND status IN ('queued', 'running')
          AND cancel_requested = 0
        ORDER BY created_at ASC
    """, (job_type,))
    rows = cursor.fetchall()
    for row in rows:
        job = _job_row_to_dict(row)
        if job.get("status") == "running" and ((job.get("done") or 0) + (job.get("failed") or 0)) >= (job.get("total") or 0):
            continue
        payload = job.get("payload") or {}
        if any(payload.get(key) != value for key, value in match_payload.items()):
            continue

        existing_paths = payload.get("paths") or []
        seen = set(existing_paths)
        appended = []
        for path in paths:
            if path and path not in seen:
                existing_paths.append(path)
                seen.add(path)
                appended.append(path)
        if not appended:
            conn.close()
            return job, []

        payload["paths"] = existing_paths
        total = len(existing_paths)
        now = _utc_now()
        cursor.execute("""
            UPDATE jobs
            SET payload_json = ?, total = ?, label = ?, updated_at = ?
            WHERE id = ?
        """, (json.dumps(payload), total, f"{label_prefix} ({total} files)", now, job["id"]))
        conn.commit()
        cursor.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],))
        updated = _job_row_to_dict(cursor.fetchone())
        conn.close()
        return updated, appended

    conn.close()
    return None, []


def add_job_event(job_id: str, message: str, level: str = "info", progress: float | None = None) -> int:
    now = _utc_now()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events WHERE job_id = ?", (job_id,))
    seq = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO job_events (job_id, seq, level, message, progress, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (job_id, seq, level, message, progress, now))
    conn.commit()
    conn.close()
    return seq


def get_job(job_id: str) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    conn.close()
    return _job_row_to_dict(row)


def get_active_jobs(include_recent: bool = True) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    if include_recent:
        cursor.execute("""
            SELECT * FROM jobs
            WHERE status IN ('queued', 'running')
               OR id IN (SELECT id FROM jobs WHERE status IN ('done', 'failed', 'cancelled') ORDER BY updated_at DESC LIMIT 5)
            ORDER BY created_at DESC
        """)
    else:
        cursor.execute("SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return [_job_row_to_dict(row) for row in rows]


def get_job_events(job_id: str, after_seq: int = 0) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM job_events
        WHERE job_id = ? AND seq > ?
        ORDER BY seq
    """, (job_id, after_seq))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def request_job_cancel(job_id: str):
    update_job(job_id, cancel_requested=1)


def is_job_cancel_requested(job_id: str) -> bool:
    job = get_job(job_id)
    return bool(job and job.get("cancel_requested"))


def mark_running_jobs_interrupted() -> int:
    now = _utc_now()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, label FROM jobs
        WHERE status IN ('queued', 'running')
    """)
    rows = cursor.fetchall()
    for row in rows:
        cursor.execute("""
            UPDATE jobs
            SET status = 'failed',
                current_label = '',
                updated_at = ?,
                finished_at = ?,
                result_json = COALESCE(result_json, ?)
            WHERE id = ?
        """, (now, now, json.dumps({"interrupted": True}), row["id"]))
        cursor.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events WHERE job_id = ?", (row["id"],))
        seq = cursor.fetchone()[0]
        cursor.execute("""
            INSERT INTO job_events (job_id, seq, level, message, progress, created_at)
            VALUES (?, ?, 'warning', 'Job interrupted because the server restarted.', NULL, ?)
        """, (row["id"], seq, now))
    conn.commit()
    conn.close()
    return len(rows)


def get_all_assets() -> list[dict]:
    """Get all assets from the database."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM assets ORDER BY folder, filename")
    rows = cursor.fetchall()
    conn.close()
    return [row_to_dict(row) for row in rows]


def _word_match(text, query: str) -> bool:
    if not text:
        return False
    return bool(re.search(r'\b' + re.escape(query) + r'\b', str(text), re.IGNORECASE))


def _search_terms(query: str) -> list[str]:
    """Split a UI query into required words; `sunrise clouds` means both."""
    return re.findall(r"[^\W_]+", query, flags=re.UNICODE)


def search_assets(query: str | None, camera: str = None, lens: str = None) -> list[dict]:
    terms = _search_terms(query or "")
    camera = (camera or "").strip()
    lens = (lens or "").strip()
    if not terms and not camera and not lens:
        return []
    conn = get_connection()
    cursor = conn.cursor()
    # Each word is an AND condition, but words may live in different fields
    # (e.g. title has "sunrise", a keyword has "clouds").
    field_match = ("(filename LIKE ? OR title LIKE ? OR keywords LIKE ? OR camera_make LIKE ? "
                   "OR camera_model LIKE ? OR lens_make LIKE ? OR lens_model LIKE ? OR lens_id LIKE ?)")
    search_sql = " AND ".join(field_match for _ in terms)
    search_params = tuple(value for term in terms for value in (f"%{term}%",) * 8)
    extra_clauses = []
    extra_params = []
    if camera:
        extra_clauses.append("(camera_make LIKE ? OR camera_model LIKE ?)")
        extra_params.extend((f"%{camera}%", f"%{camera}%"))
    if lens:
        extra_clauses.append("(lens_make LIKE ? OR lens_model LIKE ? OR lens_id LIKE ?)")
        extra_params.extend((f"%{lens}%", f"%{lens}%", f"%{lens}%"))
    where_sql = " AND ".join(part for part in (search_sql if search_sql else None, *extra_clauses) if part)
    params = search_params + tuple(extra_params)
    cursor.execute(
        f"SELECT * FROM assets WHERE {where_sql} ORDER BY folder DESC, capture_date",
        params
    )
    rows = cursor.fetchall()
    conn.close()

    results = []
    for asset in [row_to_dict(row) for row in rows]:
        filename = asset.get("filename") or ""
        searchable_text = (
            asset.get("title") or "",
            asset.get("camera_make") or "",
            asset.get("camera_model") or "",
            asset.get("lens_make") or "",
            asset.get("lens_model") or "",
            asset.get("lens_id") or "",
            *(asset.get("keywords") or []),
        )
        # Filenames commonly use underscores, which are regex word characters;
        # allow a case-insensitive substring there while preserving word-boundary
        # matching for human metadata and tags.
        if all(
            term.casefold() in filename.casefold()
            or any(_word_match(text, term) for text in searchable_text)
            for term in terms
        ):
            results.append(asset)
    return results


def get_assets_by_folder(folder: str) -> list[dict]:
    """Get assets in a specific folder."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM assets WHERE folder = ? ORDER BY filename", (folder,))
    rows = cursor.fetchall()
    conn.close()
    return [row_to_dict(row) for row in rows]


def get_assets_by_paths(paths: list[str]) -> list[dict]:
    """Get assets by their paths."""
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(paths))
    cursor.execute(f"SELECT * FROM assets WHERE path IN ({placeholders})", paths)
    rows = cursor.fetchall()
    conn.close()
    return [row_to_dict(row) for row in rows]


def get_asset_by_path(path: str) -> dict | None:
    """Get a single asset by path."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM assets WHERE path = ?", (path,))
    row = cursor.fetchone()
    conn.close()
    return row_to_dict(row)


def update_asset_gps(path: str, lat: float | None, lon: float | None):
    """Cache GPS coordinates for one asset."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE assets SET gps_lat = ?, gps_lon = ? WHERE path = ?", (lat, lon, path))
    conn.commit()
    conn.close()


def upsert_asset(data: dict):
    """Insert or update a single asset."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT OR REPLACE INTO assets
        (path, filename, folder, media_type, thumb_path, capture_date, file_mtime, title, description, keywords, is_dirty)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("path"),
        data.get("filename"),
        data.get("folder"),
        data.get("media_type"),
        data.get("thumb_path"),
        data.get("capture_date"),
        data.get("file_mtime"),
        data.get("title"),
        data.get("description"),
        data.get("keywords"),
        data.get("is_dirty", 0)
    ))

    conn.commit()
    conn.close()


def upsert_assets(assets: list[dict]):
    """Insert or update multiple assets.

    Deliberately does NOT touch is_dirty on conflict (an existing row keeps
    whatever dirty state it already had). Callers here are disk scans, which
    always pass is_dirty=0 with no idea whether the DB row has unsynced
    edits pending — overwriting it would silently discard the "this file has
    an edit not yet written to disk" flag any time a folder gets rescanned
    (Refresh Folders, adding a folder root, etc.), before the user ever gets
    a chance to Sync. is_dirty is only ever set/cleared explicitly, by
    update_metadata() and the sync functions below.
    """
    conn = get_connection()
    cursor = conn.cursor()

    for asset in assets:
        cursor.execute("""
            INSERT INTO assets
            (path, filename, folder, media_type, thumb_path, capture_date, file_mtime, title, description, keywords, is_dirty)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                filename     = excluded.filename,
                folder       = excluded.folder,
                media_type   = excluded.media_type,
                thumb_path   = COALESCE(excluded.thumb_path,   thumb_path),
                capture_date = COALESCE(excluded.capture_date, capture_date),
                file_mtime   = excluded.file_mtime,
                title        = COALESCE(excluded.title,        title),
                description  = COALESCE(excluded.description,  description),
                keywords     = COALESCE(excluded.keywords,     keywords)
        """, (
            asset.get("path"),
            asset.get("filename"),
            asset.get("folder"),
            asset.get("media_type"),
            asset.get("thumb_path"),
            asset.get("capture_date"),
            asset.get("file_mtime"),
            asset.get("title"),
            asset.get("description"),
            asset.get("keywords"),
            asset.get("is_dirty", 0)
        ))

    conn.commit()
    conn.close()


def update_metadata(paths: list[str], title: str | None, description: str | None, keywords: list[str] | None):
    """Update metadata for multiple assets and mark them as dirty."""
    conn = get_connection()
    cursor = conn.cursor()

    keywords_json = json.dumps(keywords) if keywords else None

    for path in paths:
        # Build dynamic update - only update fields that are provided
        updates = []
        values = []

        if title is not None:
            updates.append("title = ?")
            values.append(title)
        if description is not None:
            updates.append("description = ?")
            values.append(description)
        if keywords is not None:
            updates.append("keywords = ?")
            values.append(keywords_json)

        # Always mark as dirty
        updates.append("is_dirty = 1")

        values.append(path)

        if updates:
            sql = f"UPDATE assets SET {', '.join(updates)} WHERE path = ?"
            cursor.execute(sql, values)

    conn.commit()
    conn.close()


def get_dirty_assets() -> list[dict]:
    """Get all assets marked as dirty."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM assets WHERE is_dirty = 1")
    rows = cursor.fetchall()
    conn.close()
    return [row_to_dict(row) for row in rows]


def clear_dirty(paths: list[str]):
    """Clear dirty flag for specified paths."""
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(paths))
    cursor.execute(f"UPDATE assets SET is_dirty = 0 WHERE path IN ({placeholders})", paths)
    conn.commit()
    conn.close()


def clear_dirty_and_update_mtime(paths: list[str]):
    """Clear dirty flag and update file_mtime for synced files."""
    import os
    import hashlib
    import shutil
    from config import THUMB_CACHE_DIR

    conn = get_connection()
    cursor = conn.cursor()

    for path in paths:
        # Clear dirty flag
        cursor.execute("UPDATE assets SET is_dirty = 0 WHERE path = ?", (path,))

        # Update file_mtime to reflect the new modification time after exiftool write
        try:
            # Get old mtime from DB before updating, so we can carry the thumbnail over
            cursor.execute("SELECT file_mtime FROM assets WHERE path = ?", (path,))
            row = cursor.fetchone()
            old_mtime = row[0] if row else None

            new_mtime = os.path.getmtime(path)

            # Carry thumbnail over to new cache key — exiftool only changed metadata,
            # not image content, so the existing thumbnail is still valid
            if old_mtime and old_mtime != new_mtime:
                old_key = hashlib.sha1(f"{path}:{old_mtime}".encode()).hexdigest()
                new_key = hashlib.sha1(f"{path}:{new_mtime}".encode()).hexdigest()
                old_thumb = os.path.join(THUMB_CACHE_DIR, f"{old_key}.jpg")
                new_thumb = os.path.join(THUMB_CACHE_DIR, f"{new_key}.jpg")
                if os.path.exists(old_thumb) and not os.path.exists(new_thumb):
                    shutil.copy2(old_thumb, new_thumb)

            cursor.execute("UPDATE assets SET file_mtime = ? WHERE path = ?", (new_mtime, path))
        except Exception as e:
            print(f"Error updating mtime for {path}: {e}")

    conn.commit()
    conn.close()


def get_folder_tree() -> list[dict]:
    """Get folder structure with asset counts."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT folder, COUNT(*) as count, SUM(CASE WHEN is_dirty = 1 THEN 1 ELSE 0 END) as dirty_count
        FROM assets
        GROUP BY folder
        ORDER BY folder
    """)
    rows = cursor.fetchall()
    conn.close()
    return [{"folder": row["folder"], "count": row["count"], "dirty_count": row["dirty_count"] or 0} for row in rows]


def clear_folder_dirty(folder: str):
    """Clear dirty flag for all assets in a folder."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE assets SET is_dirty = 0 WHERE folder = ?", (folder,))
    conn.commit()
    conn.close()


def delete_missing_assets(existing_paths: set[str]) -> int:
    """Delete assets from DB that no longer exist on disk.

    Returns:
        Number of assets deleted
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Get all paths from DB
    cursor.execute("SELECT path FROM assets")
    db_paths = {row["path"] for row in cursor.fetchall()}

    # Find missing paths
    missing_paths = db_paths - existing_paths

    deleted_count = 0
    if missing_paths:
        placeholders = ",".join("?" * len(missing_paths))
        cursor.execute(f"DELETE FROM assets WHERE path IN ({placeholders})", list(missing_paths))
        deleted_count = cursor.rowcount
        conn.commit()

    conn.close()
    return deleted_count


def move_asset(old_path: str, new_path: str, new_folder: str):
    """Update path and folder for a moved asset."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE assets SET path=?, folder=?, filename=? WHERE path=?",
        (new_path, new_folder, os.path.basename(new_path), old_path)
    )
    conn.commit()
    conn.close()




def delete_assets(paths: list[str]) -> list[tuple[str, float]]:
    """Delete assets by path from DB. Returns list of (path, file_mtime) for thumbnail cleanup."""
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(paths))
    cursor.execute(f"SELECT path, file_mtime FROM assets WHERE path IN ({placeholders})", paths)
    rows = [(row["path"], row["file_mtime"]) for row in cursor.fetchall()]
    cursor.execute(f"DELETE FROM assets WHERE path IN ({placeholders})", paths)
    conn.commit()
    conn.close()
    return rows
