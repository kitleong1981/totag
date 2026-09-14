"""Flask application for ToTag Metadata Manager."""

import hashlib
import csv
import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from flask import Flask, render_template, jsonify, request, Response, stream_with_context, g
from concurrent.futures import ThreadPoolExecutor

import config
from config import FOLDER_ROOTS, THUMB_CACHE_DIR, IMAGE_EXTENSIONS
from models.db import (
    init_db,
    get_all_assets,
    get_assets_by_folder,
    get_assets_by_paths,
    get_asset_by_path,
    update_metadata,
    get_dirty_assets,
    clear_dirty_and_update_mtime,
    get_folder_tree,
    delete_assets,
    move_asset,
    search_assets,
    create_job,
    update_job,
    append_paths_to_matching_job,
    add_job_event,
    get_job,
    get_connection,
    get_active_jobs,
    get_job_events,
    request_job_cancel,
    is_job_cancel_requested,
    mark_running_jobs_interrupted,
    update_asset_gps,
)
from core.metadata import write_metadata, write_metadata_batch
from core.thumbs import get_or_generate_thumbnail, generate_thumbnail
from core.scanner import refresh_database
from core.binaries import find_binary
from core.keywords import _strip_thinking, _merge_hint_texts
from core.geocoding import _gps_for_asset, _geotag_location_hint
from core.ai_generation import (
    _resize_image_b64,
    _get_prepared_frames,
    _prefetch_frames,
    clear_frame_prefetch_queue,
    generate_stock_metadata,
)
import core.llm_config as llm_config

app = Flask(__name__)


def _path_under_roots(path: str) -> bool:
    """True if `path` resolves to somewhere inside one of FOLDER_ROOTS.

    Used to keep file-serving/delete/move/finder routes from being usable as
    an arbitrary-file read/write primitive by anyone who can reach this
    server — see api_preview, api_delete_files, api_move_files,
    api_open_finder. Resolves symlinks (realpath) on both sides so a symlink
    inside a watched folder can't be used to escape it.
    """
    if not path:
        return False
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    for root in FOLDER_ROOTS:
        real_root = os.path.realpath(root)
        if real == real_root or real.startswith(real_root + os.sep):
            return True
    return False


FOLDERS_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "folders.json")
LOCATIONS_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locations.json")
MODELS_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")
QUICK_SEARCHES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quick_searches.json")
LLM_CONFIG_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_config.json")
# Restores a previously saved base URL / API key (from the Model Settings
# modal) over whatever TOTAG_LLM_BASE_URL/OMLX_API_KEY were seeded from the
# environment — see core/llm_config.py.
llm_config.load_saved_config(LLM_CONFIG_FILE)
DEFAULT_LLM_MODEL  = "gpt-4o-mini"
GOOGLE_MAPS_API_KEY = (
    os.environ.get("GOOGLE_MAPS_API_KEY")
    or os.environ.get("GOOGLE_API_KEY")
)
GEOCODE_CACHE = {}
GEOCODE_CONTEXT_CACHE = {}

# Background thread pool for thumbnail/background work.
executor = ThreadPoolExecutor(max_workers=4)
# Metadata/art generation jobs must be whole-job FIFO. A shared LLM lock only
# serializes individual model calls; a single-worker executor prevents a later
# small job from interleaving between files of an older large job.
metadata_job_executor = ThreadPoolExecutor(max_workers=1)
metadata_llm_lock = threading.RLock()
# Upload has no equivalent shared resource forcing serialization — unlike
# generation, each job here just streams a different folder's own files
# over its own FTP/SFTP connection to its own per-folder upload-log file,
# so uploading two different folders at the same time is safe to actually
# run in parallel instead of queuing one behind the other. Capped at 3
# (not unbounded) so a burst of clicks doesn't open more concurrent FTP/SFTP
# connections than is reasonable. _folder_has_active_export_job() below
# still prevents two jobs from targeting the SAME folder at once — that
# race (two jobs read-modify-writing one folder's upload-log JSON
# concurrently) was harmless before only because max_workers=1 made it
# physically impossible; raising this needed that guard added first.
export_job_executor = ThreadPoolExecutor(max_workers=3)

# Active AI model for metadata/location generation (switchable via /api/config)
def _load_models_file():
    try:
        with open(MODELS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data, None  # old format — no default stored
        return data.get("models", []), data.get("default")
    except Exception:
        return [], None


def _load_folders_file():
    """Return the saved folder-roots list, or None if never saved (first run —
    the TOTAG_FOLDER_ROOTS env-var seed in config.py is still in effect)."""
    try:
        with open(FOLDERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else None
    except Exception:
        return None


def _save_folders_file(paths: list[str]):
    with open(FOLDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(paths, f, indent=2, ensure_ascii=False)


# If the in-app folder settings have been saved before, they take over from
# the .env seed so edits survive a restart.
_saved_folders = _load_folders_file()
if _saved_folders is not None:
    config.set_folder_roots(_saved_folders)


_models_list, _stored_default = _load_models_file()
active_model = _stored_default or (_models_list[0]["value"] if _models_list else DEFAULT_LLM_MODEL)
AVAILABLE_MODELS = [m["value"] for m in _models_list] or [DEFAULT_LLM_MODEL]

def _load_secret_from_file(path: str) -> str:
    try:
        p = os.path.expanduser(path)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""

if not GOOGLE_MAPS_API_KEY:
    GOOGLE_MAPS_API_KEY = _load_secret_from_file("~/.google_maps_api_key")

GOOGLE_MAPS_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "google_maps_key.json")


def _load_google_maps_key_file() -> str:
    try:
        with open(GOOGLE_MAPS_KEY_FILE, encoding="utf-8") as f:
            return json.load(f).get("api_key") or ""
    except Exception:
        return ""


def _save_google_maps_key_file(api_key: str):
    with open(GOOGLE_MAPS_KEY_FILE, "w", encoding="utf-8") as f:
        json.dump({"api_key": api_key}, f, indent=2)


# A previously saved key (from the Model Settings modal) takes over from the
# GOOGLE_MAPS_API_KEY/~/.google_maps_api_key env-var seed above, same
# override pattern as folders.json/llm_config.json elsewhere in this file.
_saved_google_maps_key = _load_google_maps_key_file()
if _saved_google_maps_key:
    GOOGLE_MAPS_API_KEY = _saved_google_maps_key

# Track pending metadata loads to avoid redundant work
# Key: folder_path, Value: future
pending_metadata_loads = {}

# Track refresh progress for re-read metadata operation
# Key: folder_path, Value: {'processed': int, 'total': int}
refresh_progress = {}

# Track background thumbnail generation status
# {'active': bool, 'count': int, 'folder': str}
thumb_generation_status = {'active': False, 'count': 0, 'folder': ''}
# Guards the "completed" counter in api_files()/api_load_folder()'s
# per-folder thumbnail callbacks — `completed[0] += 1` is a
# read-modify-write across up to 4 worker threads and isn't atomic even
# under the GIL, so without a lock two threads can race and drop an
# increment, leaving thumb_generation_status['active'] stuck True forever.
_thumb_progress_lock = threading.Lock()

# Global "load all missing metadata" worker state
# Runs independently of per-folder loads — not cancelled on folder switch
bulk_meta_status = {
    'active': False,
    'done': 0,
    'total': 0,
    'current_folder': '',
}
_bulk_meta_lock = threading.Lock()

# Global bulk thumbnail generation state (separate from per-folder thumb_generation_status)
bulk_thumb_status = {
    'active': False,
    'done': 0,
    'total': 0,
}

def _now_iso():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _job_snapshot(job_id: str) -> dict | None:
    job = get_job(job_id)
    if not job:
        return None
    total = job.get("total") or 0
    done = job.get("done") or 0
    failed = job.get("failed") or 0
    job["progress"] = round((done + failed) / total * 100, 1) if total else None
    return job


def _emit_job(job_id: str, message: str, level: str = "info", progress: float | None = None):
    add_job_event(job_id, message, level=level, progress=progress)


def _llm_health_status(timeout: float = 3.0) -> dict:
    """Check whether the configured OpenAI-compatible LLM server is reachable."""
    import urllib.error
    import urllib.request
    base_url = llm_config.get_base_url()
    url = f"{base_url.rstrip('/')}/v1/models"
    api_key = llm_config.get_api_key()
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {
                "ok": 200 <= resp.status < 300,
                "status": resp.status,
                "base_url": base_url,
                "model": active_model,
                "message": "LLM server is reachable",
            }
    except urllib.error.HTTPError as e:
        return {
            "ok": False,
            "status": e.code,
            "base_url": base_url,
            "model": active_model,
            "message": f"LLM server responded with HTTP {e.code}",
        }
    except Exception as e:
        return {
            "ok": False,
            "status": None,
            "base_url": base_url,
            "model": active_model,
            "message": f"LLM server is not reachable: {e}",
        }


def _run_metadata_generate_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return
    payload = job.get("payload") or {}
    location_override = (payload.get("location_override") or "").strip()
    context_hint = (payload.get("context_hint") or "").strip()
    total = len(payload.get("paths") or [])
    done = 0
    failed = 0
    generated_paths = []

    update_job(job_id, status="running", total=total, started_at=_now_iso())
    _emit_job(job_id, f"Started metadata generation for {total} file(s)")
    health = _llm_health_status()
    if not health.get("ok"):
        update_job(job_id, status="failed", done=0, failed=total, current_label="", finished_at=_now_iso(), result_json={"error": health.get("message"), "health": health})
        _emit_job(job_id, f"LLM health check failed: {health.get('message')} ({health.get('base_url')})", level="error")
        return

    while True:
        job = get_job(job_id)
        payload = (job or {}).get("payload") or {}
        paths = payload.get("paths") or []
        total = len(paths)
        index = done + failed
        if index >= total:
            break
        path = paths[index]
        filename = os.path.basename(path)
        if is_job_cancel_requested(job_id):
            clear_frame_prefetch_queue()
            update_job(job_id, status="cancelled", done=done, failed=failed, current_label="", finished_at=_now_iso())
            _emit_job(job_id, f"Cancelled after {done} generated, {failed} failed", level="warning")
            return

        update_job(job_id, current_label=filename, done=done, failed=failed)
        _emit_job(job_id, f"Generating {filename}", progress=((done + failed) / total * 100) if total else None)

        # Prep next file's frames in the background while this one is on the LLM
        if index + 1 < total:
            try:
                _prefetch_frames(paths[index + 1])
            except Exception:
                pass

        try:
            started_at = time.monotonic()
            with metadata_llm_lock:
                with app.test_request_context(
                    "/api/generate-metadata",
                    method="POST",
                    json={"path": path, "location_override": location_override, "context_hint": context_hint},
                ):
                    response = api_generate_metadata()
            status_code, data = _unpack_flask_response(response)
            if status_code >= 400 or (data and data.get("error")):
                failed += 1
                _emit_job(job_id, f"Failed {filename}: {(data or {}).get('error', 'unknown error')}", level="error")
            else:
                done += 1
                generated_paths.append(path)
                update_job(job_id, result_json={"done": done, "failed": failed, "generated_paths": generated_paths})
                elapsed = time.monotonic() - started_at
                _emit_job(job_id, f"✓ Generated {filename} ({elapsed:.1f}s)", progress=((done + failed) / total * 100) if total else None)
        except Exception as e:
            failed += 1
            _emit_job(job_id, f"Error {filename}: {e}", level="error")

        update_job(job_id, done=done, failed=failed)

    status = "done" if failed == 0 else "failed"
    update_job(
        job_id,
        status=status,
        done=done,
        failed=failed,
        current_label="",
        result_json={"done": done, "failed": failed, "generated_paths": generated_paths},
        finished_at=_now_iso(),
    )
    if failed:
        _emit_job(job_id, f"Metadata generation finished: {done} generated, {failed} failed", level="warning")
    else:
        _emit_job(job_id, f"Metadata generation complete: {done} file(s)")




def _unpack_flask_response(response):
    """Normalize a Flask view's return value into (status_code, json_dict).

    Route handlers here return either `jsonify(x)` (a Response, status 200)
    or `jsonify(x), 500` (a (Response, int) tuple) — the batch job runner
    calls a route function directly (not through the WSGI stack) to reuse
    its logic, so it has to unpack that itself. The previous version only
    checked hasattr(response, "status_code"), which is False for the tuple
    case, silently treating every error response as a 200/success.
    """
    if isinstance(response, tuple):
        body, status_code = response[0], response[1]
    else:
        body, status_code = response, getattr(response, "status_code", 200)
    data = body.get_json(silent=True) if hasattr(body, "get_json") else {}
    return status_code, (data or {})


def _strip_sse_data(line: str) -> str:
    text = str(line or "").strip()
    if text.startswith("data:"):
        text = text[5:].strip()
    return text


def _run_export_csv_job(job_id: str):
    from core.microstock import generate_csvs_gen

    job = get_job(job_id)
    if not job:
        return
    payload = job.get("payload") or {}
    folder_path = payload.get("folder_path") or ""
    category = payload.get("category") or None
    folder_name = os.path.basename(folder_path.rstrip(os.sep)) or "folder"
    done = 0
    failed = 0
    total = job.get("total") or 0

    update_job(job_id, status="running", started_at=_now_iso(), current_label=folder_name)
    _emit_job(job_id, f"Started CSV generation for {folder_name}")
    if not (category and category != "AI_AUTO"):
        _emit_job(job_id, f"Using CSV category model: {active_model}")

    try:
        for raw_line in generate_csvs_gen(folder_path, category if category and category != "AI_AUTO" else None, model=active_model):
            if is_job_cancel_requested(job_id):
                update_job(job_id, status="cancelled", done=done, failed=failed, current_label="", finished_at=_now_iso())
                _emit_job(job_id, f"Cancelled CSV generation for {folder_name}", level="warning")
                return

            message = _strip_sse_data(raw_line)
            if not message:
                continue
            if message == "[DONE]":
                break

            match = re.search(r"\[(\d+)/(\d+)\]", message)
            if match:
                done = max(done, int(match.group(1)) - 1)
                total = int(match.group(2))
                update_job(job_id, total=total, done=done, failed=failed)
                progress = (done / total * 100) if total else None
            else:
                progress = (done / total * 100) if total else None

            level = "error" if message.startswith("❌") else "warning" if "⚠️" in message else "info"
            _emit_job(job_id, message, level=level, progress=progress)
            if level == "error":
                failed += 1

        done = total or done
        status = "failed" if failed else "done"
        update_job(
            job_id,
            status=status,
            done=done,
            failed=failed,
            current_label="",
            result_json={"folder_path": folder_path, "category": category, "failed": failed},
            finished_at=_now_iso(),
        )
        if failed:
            _emit_job(job_id, f"CSV generation finished with {failed} error(s)", level="warning", progress=100 if total else None)
        else:
            _emit_job(job_id, f"CSV generation complete for {folder_name}", progress=100 if total else None)
    except Exception as e:
        failed = max(1, failed)
        update_job(
            job_id,
            status="failed",
            done=done,
            failed=failed,
            current_label="",
            result_json={"folder_path": folder_path, "category": category, "error": str(e)},
            finished_at=_now_iso(),
        )
        _emit_job(job_id, f"CSV generation failed: {e}", level="error")


def _estimate_export_upload_total(folder_path: str) -> int:
    from pathlib import Path
    from core.microstock import (
        SITE_MATRIX,
        _list_media_files,
        _load_ftp_config,
        _load_upload_log,
    )
    from config import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

    folder = Path(folder_path)
    files = _list_media_files(folder_path)
    if not files:
        return 0

    config = _load_ftp_config()
    has_images = any(f.suffix.lower() in IMAGE_EXTENSIONS for f in files)
    has_clips = any(f.suffix.lower() in VIDEO_EXTENSIONS for f in files)
    is_editorial = "_ED" in folder.name
    ftype = "clips" if has_clips and not has_images else "images"
    sites = SITE_MATRIX.get(ftype, []).copy()
    if ftype == "images" and not is_editorial and "Pond5" not in sites:
        sites.append("Pond5")
    if is_editorial:
        sites = [s for s in sites if s != "Adobe Stock"]

    upload_log = _load_upload_log(folder)
    total = 0
    for site in sites:
        if site not in config:
            continue
        already_done = set(upload_log.get(site, []))
        total += sum(1 for fp in files if fp.name not in already_done)
        csv_for_site = {
            "Shutterstock": folder / "shutterstock_upload.csv",
            "Pond5": folder / "pond5_upload.csv",
        }.get(site)
        if csv_for_site and csv_for_site.exists() and csv_for_site.name not in already_done:
            total += 1
    return total


def _run_export_upload_job(job_id: str):
    from core.microstock import upload_folder_gen

    job = get_job(job_id)
    if not job:
        return
    payload = job.get("payload") or {}
    folder_path = payload.get("folder_path") or ""
    folder_name = os.path.basename(folder_path.rstrip(os.sep)) or "folder"
    done = 0
    failed = 0
    total = _estimate_export_upload_total(folder_path)

    update_job(job_id, status="running", total=total, started_at=_now_iso(), current_label=folder_name)
    _emit_job(job_id, f"Started upload for {folder_name}", progress=0 if total else None)

    try:
        for raw_line in upload_folder_gen(folder_path):
            if is_job_cancel_requested(job_id):
                update_job(job_id, status="cancelled", done=done, failed=failed, current_label="", finished_at=_now_iso())
                _emit_job(job_id, f"Cancelled upload for {folder_name}", level="warning")
                return

            message = _strip_sse_data(raw_line)
            if not message:
                continue
            if message == "[DONE]":
                break

            upload_match = re.search(r"📤\s+(.+?)(?:\s+\(|\s+\(CSV\)|$)", message)
            if upload_match:
                update_job(job_id, current_label=upload_match.group(1).strip())

            level = "error" if message.startswith("❌") else "warning" if "⚠️" in message else "info"
            if message.strip().startswith("✔") or " ✔ " in message:
                done += 1
                update_job(job_id, done=done, failed=failed)
            if level == "error":
                failed += 1
                update_job(job_id, failed=failed)

            progress = (done / total * 100) if total else None
            _emit_job(job_id, message, level=level, progress=progress)

        status = "failed" if failed else "done"
        update_job(
            job_id,
            status=status,
            done=done,
            failed=failed,
            current_label="",
            result_json={"folder_path": folder_path, "failed": failed},
            finished_at=_now_iso(),
        )
        if failed:
            _emit_job(job_id, f"Upload finished with {failed} error(s). Resume data was saved.", level="warning", progress=100 if total else None)
        else:
            _emit_job(job_id, f"Upload complete for {folder_name}", progress=100 if total else None)
    except Exception as e:
        failed = max(1, failed)
        update_job(
            job_id,
            status="failed",
            done=done,
            failed=failed,
            current_label="",
            result_json={"folder_path": folder_path, "error": str(e)},
            finished_at=_now_iso(),
        )
        _emit_job(job_id, f"Upload failed: {e}", level="error")


@app.route("/")
def index():
    """Serve the main application page."""
    return render_template("index.html")


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """Get or set runtime configuration (active model)."""
    global active_model
    if request.method == "POST":
        data = request.get_json() or {}
        model = (data.get("model") or "").strip()
        if model in AVAILABLE_MODELS:
            active_model = model
    return jsonify({"model": active_model, "available_models": AVAILABLE_MODELS})


@app.route("/api/llm-health", methods=["GET"])
def api_llm_health():
    """Return whether the configured LLM endpoint is reachable."""
    health = _llm_health_status()
    return jsonify(health), 200 if health.get("ok") else 503

@app.route("/api/folder-roots", methods=["GET"])
def api_folder_roots_get():
    """Return the current folder roots, each flagged with whether it's
    reachable right now (e.g. an external drive can be unplugged)."""
    return jsonify({
        "folders": [
            {"path": p, "exists": os.path.isdir(p)}
            for p in config.FOLDER_ROOTS
        ],
    })


@app.route("/api/pick-folder", methods=["POST"])
def api_pick_folder():
    """Open the native macOS folder picker and return the chosen path.

    Browsers deliberately don't expose real filesystem paths from a web
    <input type="file">, even with webkitdirectory — since this app only
    ever runs on the same machine as the browser hitting it, the backend can
    just ask the OS directly instead of asking the user to paste a path.
    macOS only (osascript); there's no equivalent one-liner on Linux/Windows.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose a folder for ToTag to watch:")'],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Folder picker timed out"}), 504
    except FileNotFoundError:
        return jsonify({"error": "osascript not available (macOS only)"}), 501

    if result.returncode != 0:
        # returncode 1 with "User canceled" in stderr is the normal Cancel path
        if "User canceled" in (result.stderr or ""):
            return jsonify({"cancelled": True})
        return jsonify({"error": (result.stderr or "folder picker failed").strip()}), 500

    path = result.stdout.strip().rstrip("/")
    return jsonify({"path": path})


@app.route("/api/folder-roots", methods=["POST"])
def api_folder_roots_post():
    """Save the folder roots list. Newly added, reachable folders are scanned
    immediately (fast file listing only) so they show up without a separate
    manual refresh step."""
    data = request.get_json() or {}
    raw_paths = data.get("folders") or []
    if not isinstance(raw_paths, list):
        return jsonify({"error": "expected a list of paths"}), 400

    new_paths = []
    seen = set()
    for p in raw_paths:
        p = str(p or "").strip().rstrip("/")
        if p and p not in seen:
            seen.add(p)
            new_paths.append(p)

    previous = set(config.FOLDER_ROOTS)
    added = [p for p in new_paths if p not in previous]

    from core.scanner import scan_root_fast
    from models.db import upsert_assets
    scanned = {}
    for folder_path in added:
        if not os.path.isdir(folder_path):
            continue
        assets = scan_root_fast(folder_path)
        for i in range(0, len(assets), 100):
            upsert_assets(assets[i:i + 100])
        scanned[folder_path] = len(assets)

    config.set_folder_roots(new_paths)
    _save_folders_file(new_paths)

    return jsonify({
        "success": True,
        "folders": [{"path": p, "exists": os.path.isdir(p)} for p in new_paths],
        "scanned": scanned,
    })


@app.route("/api/locations", methods=["GET"])
def api_locations_get():
    """Return the editable location list."""
    try:
        with open(LOCATIONS_FILE, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify([])


@app.route("/api/locations", methods=["POST"])
def api_locations_post():
    """Save the editable location list."""
    data = request.get_json()
    if not isinstance(data, list):
        return jsonify({"error": "expected a list"}), 400
    with open(LOCATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return jsonify({"success": True})


@app.route("/api/quick-searches", methods=["GET"])
def api_quick_searches_get():
    try:
        with open(QUICK_SEARCHES_FILE, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify([])

@app.route("/api/quick-searches", methods=["POST"])
def api_quick_searches_post():
    data = request.get_json()
    if not isinstance(data, list):
        return jsonify({"error": "expected a list"}), 400
    with open(QUICK_SEARCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return jsonify({"success": True})


@app.route("/api/llm-config", methods=["GET"])
def api_llm_config_get():
    """Return the AI endpoint's base URL and whether a key is set — never
    the key itself, same write-only pattern as /api/microstock-credentials."""
    return jsonify({
        "base_url": llm_config.get_base_url(),
        "default_base_url": llm_config.DEFAULT_BASE_URL,
        "has_api_key": bool(llm_config.get_api_key()),
    })


@app.route("/api/llm-config", methods=["POST"])
def api_llm_config_post():
    """Save the AI endpoint's base URL / API key and apply it immediately.

    A blank api_key means "keep the existing key" (it's never sent back to
    the browser to display, so there's nothing to compare against). A blank
    base_url resets to the default (OpenAI itself).
    """
    data = request.get_json() or {}
    base_url = str(data.get("base_url") or "").strip()
    api_key = str(data.get("api_key") or "").strip()

    llm_config.set_llm_config(base_url, api_key)
    llm_config.save_config(LLM_CONFIG_FILE, llm_config.get_base_url(), llm_config.get_api_key())

    return jsonify({
        "success": True,
        "base_url": llm_config.get_base_url(),
        "has_api_key": bool(llm_config.get_api_key()),
    })


@app.route("/api/google-maps-key", methods=["GET"])
def api_google_maps_key_get():
    """Return whether a Google Maps API key is configured — never the key
    itself, same write-only pattern as /api/llm-config."""
    return jsonify({"has_api_key": bool(GOOGLE_MAPS_API_KEY)})


@app.route("/api/google-maps-key", methods=["POST"])
def api_google_maps_key_post():
    """Save (or clear) the Google Maps API key used for GPS reverse-geocoding
    and Places nearby-search. Applies immediately, no restart needed.

    A blank api_key with clear=false means "keep the existing key" (same
    write-only pattern as the other credential endpoints — it's never sent
    back to the browser, so there's nothing to compare a blank field
    against). Pass clear=true to actually remove a previously saved key —
    OSM-based location keywords (no key required) keep working either way.
    """
    global GOOGLE_MAPS_API_KEY
    data = request.get_json() or {}
    api_key = str(data.get("api_key") or "").strip()
    clear = bool(data.get("clear"))

    if clear:
        GOOGLE_MAPS_API_KEY = ""
        _save_google_maps_key_file("")
    elif api_key:
        GOOGLE_MAPS_API_KEY = api_key
        _save_google_maps_key_file(api_key)

    return jsonify({"success": True, "has_api_key": bool(GOOGLE_MAPS_API_KEY)})


@app.route("/api/models", methods=["GET"])
def api_models_get():
    """Return the editable model list plus the saved default."""
    models_list, default_model = _load_models_file()
    return jsonify({"models": models_list, "default": default_model or active_model})


@app.route("/api/models", methods=["POST"])
def api_models_post():
    """Save the editable model list and update in-memory available models.

    If the resulting default/active model isn't actually in the new list
    (e.g. the model that used to be active was just deleted), fall back to
    the first model in the list instead of silently keeping a phantom
    active_model that no longer appears anywhere in the UI — that state is
    invisible to the user (the dropdown just shows the first option as if
    it were selected) but every generation call keeps using the stale,
    no-longer-listed model underneath.
    """
    global AVAILABLE_MODELS, active_model
    data = request.get_json() or {}
    models_list = data.get("models") if isinstance(data, dict) else data
    if not isinstance(models_list, list):
        return jsonify({"error": "expected {models: [...]}"}), 400
    valid_values = {m.get("value") for m in models_list if m.get("value")}
    _, existing_default = _load_models_file()
    default = data.get("default", existing_default)
    if default not in valid_values:
        default = next(iter(valid_values), None)
    with open(MODELS_FILE, "w", encoding="utf-8") as f:
        json.dump({"models": models_list, "default": default}, f, indent=2, ensure_ascii=False)
    AVAILABLE_MODELS = [m["value"] for m in models_list]
    if active_model not in valid_values:
        active_model = default
    return jsonify({"success": True, "default": default, "active_model": active_model})


@app.route("/api/models/default", methods=["POST"])
def api_models_set_default():
    """Persist a model as the startup default."""
    global active_model
    model_id = (request.get_json() or {}).get("model")
    if not model_id:
        return jsonify({"error": "model required"}), 400
    models_list, _ = _load_models_file()
    with open(MODELS_FILE, "w", encoding="utf-8") as f:
        json.dump({"models": models_list, "default": model_id}, f, indent=2, ensure_ascii=False)
    active_model = model_id
    return jsonify({"success": True})


@app.route("/api/jobs/metadata-generate", methods=["POST"])
def api_jobs_metadata_generate():
    data = request.get_json() or {}
    paths = data.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    paths = [p for p in paths if p]
    if not paths:
        return jsonify({"error": "paths required"}), 400

    location_override = (data.get("location_override") or "").strip()
    context_hint = (data.get("context_hint") or "").strip()
    match_payload = {"location_override": location_override, "context_hint": context_hint}
    appended_job, appended_paths = append_paths_to_matching_job(
        "metadata_generate",
        paths,
        match_payload,
        "Metadata generation",
    )
    if appended_job:
        if appended_paths:
            _emit_job(appended_job["id"], f"Appended {len(appended_paths)} file(s) to metadata generation queue")
        return jsonify({"job_id": appended_job["id"], "job": _job_snapshot(appended_job["id"]) or appended_job, "appended": len(appended_paths)})

    job_id = uuid.uuid4().hex
    label = f"Metadata generation ({len(paths)} file{'s' if len(paths) != 1 else ''})"
    job = create_job(
        job_id,
        "metadata_generate",
        label,
        {
            "paths": paths,
            "location_override": location_override,
            "context_hint": context_hint,
        },
        total=len(paths),
    )
    _emit_job(job_id, "Queued metadata generation")
    metadata_job_executor.submit(_run_metadata_generate_job, job_id)
    return jsonify({"job_id": job_id, "job": job})



@app.route("/api/jobs/export-csv", methods=["POST"])
def api_jobs_export_csv():
    data = request.get_json() or {}
    folder_path = (data.get("folder_path") or data.get("folder") or "").strip()
    category = (data.get("category") or "").strip()
    if not folder_path:
        return jsonify({"error": "folder_path required"}), 400
    if not os.path.isdir(folder_path):
        return jsonify({"error": f"folder not found: {folder_path}"}), 400

    folder_name = os.path.basename(folder_path.rstrip(os.sep)) or "folder"
    job_id = uuid.uuid4().hex
    job = create_job(
        job_id,
        "export_csv",
        f"CSV generation ({folder_name})",
        {
            "folder_path": folder_path,
            "category": category if category and category != "AI_AUTO" else None,
        },
        total=0,
    )
    _emit_job(job_id, "Queued CSV generation")
    metadata_job_executor.submit(_run_export_csv_job, job_id)
    return jsonify({"job_id": job_id, "job": job})


def _folder_has_active_export_job(folder_path: str) -> str | None:
    """Return the job type ('export_upload') already running/queued against
    this exact folder, or None. The frontend already disables a folder's row
    while its own job is active, but that's a UI convenience, not a
    guarantee (a second tab, or a click landing just before the row
    disables) — now that export_job_executor actually runs jobs in
    parallel, two jobs racing on the same folder's upload-log JSON (read,
    modify, write) could silently drop one of their entries."""
    for job in get_active_jobs(include_recent=False):
        if job.get("type") != "export_upload":
            continue
        if (job.get("payload") or {}).get("folder_path") == folder_path:
            return job.get("type")
    return None


@app.route("/api/jobs/export-upload", methods=["POST"])
def api_jobs_export_upload():
    data = request.get_json() or {}
    folder_path = (data.get("folder_path") or data.get("folder") or "").strip()
    if not folder_path:
        return jsonify({"error": "folder_path required"}), 400
    if not os.path.isdir(folder_path):
        return jsonify({"error": f"folder not found: {folder_path}"}), 400
    if _folder_has_active_export_job(folder_path):
        return jsonify({"error": "Upload already running for this folder"}), 409

    folder_name = os.path.basename(folder_path.rstrip(os.sep)) or "folder"
    total = _estimate_export_upload_total(folder_path)
    job_id = uuid.uuid4().hex
    job = create_job(
        job_id,
        "export_upload",
        f"Upload ({folder_name})",
        {"folder_path": folder_path},
        total=total,
    )
    _emit_job(job_id, "Queued upload")
    export_job_executor.submit(_run_export_upload_job, job_id)
    return jsonify({"job_id": job_id, "job": job})


@app.route("/api/jobs/active")
def api_jobs_active():
    jobs = get_active_jobs(include_recent=True)
    for job in jobs:
        total = job.get("total") or 0
        done = job.get("done") or 0
        failed = job.get("failed") or 0
        job["progress"] = round((done + failed) / total * 100, 1) if total else None
    return jsonify({"jobs": jobs})


@app.route("/api/jobs/<job_id>")
def api_jobs_get(job_id):
    job = _job_snapshot(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"job": job})


@app.route("/api/jobs/<job_id>/events")
def api_jobs_events(job_id):
    if not get_job(job_id):
        return jsonify({"error": "job not found"}), 404
    after = int(request.args.get("after", 0) or 0)
    return jsonify({"events": get_job_events(job_id, after)})


@app.route("/api/jobs/<job_id>/stream")
def api_jobs_stream(job_id):
    if not get_job(job_id):
        return jsonify({"error": "job not found"}), 404
    after = int(request.args.get("after", 0) or 0)

    def generate():
        nonlocal after
        while True:
            events = get_job_events(job_id, after)
            for event in events:
                after = max(after, event["seq"])
                payload = {"event": event, "job": _job_snapshot(job_id)}
                yield f"data: {json.dumps(payload)}\n\n"

            job = get_job(job_id)
            if job and job.get("status") in ("done", "failed", "cancelled"):
                yield "data: [DONE]\n\n"
                break
            time.sleep(1)

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def api_jobs_cancel(job_id):
    if not get_job(job_id):
        return jsonify({"error": "job not found"}), 404
    request_job_cancel(job_id)
    _emit_job(job_id, "Cancel requested", level="warning")
    return jsonify({"success": True})


@app.route("/api/delete-files", methods=["POST"])
def api_delete_files():
    """Delete files from disk, DB, and thumbnail cache."""
    from core.thumbs import get_thumb_path
    data = request.get_json() or {}
    paths = data.get("paths", [])
    if not paths:
        return jsonify({"error": "paths required"}), 400
    paths = [p for p in paths if _path_under_roots(p)]
    if not paths:
        return jsonify({"error": "no paths under a watched folder"}), 400

    deleted, failed = [], []
    rows = delete_assets(paths)
    for path, file_mtime in rows:
        try:
            # Delete thumbnail
            thumb = get_thumb_path(path, file_mtime)
            if os.path.exists(thumb):
                os.remove(thumb)
            # Delete source file
            if os.path.exists(path):
                os.remove(path)
            deleted.append(path)
        except Exception as e:
            failed.append({"path": path, "error": str(e)})

    return jsonify({"deleted": deleted, "failed": failed})


@app.route("/api/folders")
def api_folders():
    """Get folder tree with asset counts."""
    folders = get_folder_tree()

    # Organize by root - always include all roots even if empty
    result = []
    for root in FOLDER_ROOTS:
        root_folders = [f for f in folders if f["folder"].startswith(root)]
        # Always include the root, even if it has no folders
        result.append({
            "root": root,
            "folders": root_folders,
            "total": sum(f["count"] for f in root_folders),
        })

    return jsonify(result)



@app.route("/api/files")
def api_files():
    """Get files in a folder with thumbnail URLs."""
    folder = request.args.get("folder", "")

    if not folder:
        return jsonify({"error": "folder parameter required"}), 400

    assets = get_assets_by_folder(folder)

    # Start background thumbnail generation for all assets
    print(f"Starting background thumbnail generation for {len(assets)} files in {folder}")
    thumb_generation_status['active'] = True
    thumb_generation_status['count'] = len(assets)
    thumb_generation_status['folder'] = folder

    # Track completion. Runs from up to 4 concurrent executor workers, so
    # the increment+check has to be under a lock — see _thumb_progress_lock.
    completed = [0]
    total = len(assets)

    def generate_and_track(file_path, file_mtime):
        try:
            generate_thumbnail(file_path, file_mtime)
        finally:
            with _thumb_progress_lock:
                completed[0] += 1
                done = completed[0] >= total
            if done:
                thumb_generation_status['active'] = False
                thumb_generation_status['count'] = 0
                thumb_generation_status['folder'] = ''
                print(f"Thumbnail generation complete for {folder}")

    for asset in assets:
        executor.submit(generate_and_track, asset["path"], asset["file_mtime"])

    # Build response with thumbnail URLs (only if already exist)
    files = []
    for asset in assets:
        import hashlib
        cache_key = hashlib.sha1(f"{asset['path']}:{asset['file_mtime']}".encode()).hexdigest()
        thumb_path = os.path.join(THUMB_CACHE_DIR, f"{cache_key}.jpg")

        thumb_url = None
        if os.path.exists(thumb_path):
            thumb_url = f"/thumbs/{cache_key}.jpg"

        files.append({
            "path": asset["path"],
            "filename": asset["filename"],
            "media_type": asset["media_type"],
            "thumb_url": thumb_url,
            "capture_date": asset["capture_date"],
            "is_dirty": asset["is_dirty"],
            "has_metadata": asset.get("title") is not None or asset.get("description") is not None,
            "missing_metadata": not asset.get("title") or not asset.get("description") or not asset.get("keywords"),
            "title": asset.get("title") or "",
            "keywords": asset.get("keywords") or [],
        })

    return jsonify(files)


@app.route("/api/metadata", methods=["GET"])
def api_metadata_get():
    """Get metadata for selected files."""
    paths = request.args.getlist("paths")

    if not paths:
        return jsonify({"error": "paths parameter required"}), 400

    assets = get_assets_by_paths(paths)

    # Check if all values match for multi-select
    if len(assets) > 1:
        titles = set(a["title"] for a in assets if a["title"])
        descriptions = set(a["description"] for a in assets if a["description"])

        # Convert keyword lists to tuples for set comparison
        keyword_sets = set()
        for a in assets:
            if a["keywords"]:
                keyword_sets.add(tuple(sorted(a["keywords"])))

        # Check if file_mtime values match
        mod_times = set(a["file_mtime"] for a in assets if a.get("file_mtime"))
        mod_times_mixed = len(mod_times) > 1

        result = {
            "paths": paths,
            "count": len(assets),
            "title": assets[0]["title"] if len(titles) == 1 else None,
            "title_mixed": len(titles) > 1,
            "description": assets[0]["description"] if len(descriptions) == 1 else None,
            "description_mixed": len(descriptions) > 1,
            "keywords": assets[0]["keywords"] if len(keyword_sets) == 1 else [],
            "keywords_mixed": len(keyword_sets) > 1,
            "file_mtime": assets[0]["file_mtime"] if len(mod_times) == 1 else None,
            "file_mtime_mixed": mod_times_mixed,
            "is_dirty": any(a.get("is_dirty", 0) for a in assets),
        }
    else:
        asset = assets[0] if assets else None
        result = {
            "paths": paths,
            "count": len(assets) if assets else 0,
            "title": asset["title"] if asset else None,
            "title_mixed": False,
            "description": asset["description"] if asset else None,
            "description_mixed": False,
            "keywords": asset["keywords"] if asset else [],
            "keywords_mixed": False,
            "file_mtime": asset.get("file_mtime") if asset else None,
            "file_mtime_mixed": False,
            "capture_date": asset.get("capture_date") if asset else None,
            "gps": _gps_for_asset(asset),
            "is_dirty": asset.get("is_dirty", 0) if asset else False,
        }

    return jsonify(result)


@app.route("/api/metadata/summaries", methods=["POST"])
def api_metadata_summaries():
    """Return per-file metadata summaries for keeping the thumbnail list fresh."""
    data = request.get_json() or {}
    paths = data.get("paths") or []
    if not paths:
        return jsonify({"files": []})

    assets = get_assets_by_paths(paths)
    by_path = {a["path"]: a for a in assets}
    files = []
    for path in paths:
        asset = by_path.get(path)
        if not asset:
            continue
        keywords = asset.get("keywords") or []
        files.append({
            "path": path,
            "title": asset.get("title") or "",
            "description": asset.get("description") or "",
            "keywords": keywords,
            "missing_metadata": not asset.get("title") or not asset.get("description") or not keywords,
            "is_dirty": asset.get("is_dirty", 0),
        })
    return jsonify({"files": files})


@app.route("/api/metadata", methods=["PUT"])
def api_metadata_put():
    """Update metadata for selected files."""
    data = request.get_json() or {}

    paths = data.get("paths", [])
    title = data.get("title")
    description = data.get("description")
    keywords = data.get("keywords")

    if not paths:
        return jsonify({"error": "paths required"}), 400

    # Update database
    update_metadata(paths, title, description, keywords)

    return jsonify({"success": True, "updated": len(paths)})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Write dirty files to disk via exiftool."""
    data = request.get_json() or {}
    paths = data.get("paths")

    if paths:
        # Sync specific files
        assets = get_assets_by_paths(paths)
    else:
        # Sync all dirty files
        assets = get_dirty_assets()

    if not assets:
        return jsonify({"success": True, "synced": 0, "message": "No files to sync"})

    # Group by common metadata for batch efficiency
    success_count = 0
    synced_paths = []

    for asset in assets:
        # Always sync - even if metadata is empty (to clear metadata)
        success = write_metadata(
            asset["path"],
            asset["title"],
            asset["description"],
            asset["keywords"]
        )
        if success:
            success_count += 1
            synced_paths.append(asset["path"])

    # Clear dirty flag and update file_mtime for synced files
    if synced_paths:
        clear_dirty_and_update_mtime(synced_paths)

    return jsonify({
        "success": success_count == len(assets),
        "synced": success_count,
        "failed": len(assets) - success_count,
    })



@app.route("/api/guess-landmarks", methods=["POST"])
def api_guess_landmarks():
    """Identify landmarks/locations in an image or video using the vision model."""
    import json as _json, urllib.request as _urllib, tempfile, subprocess as _sp
    data = request.get_json() or {}
    path = data.get("path")
    location_override = (data.get("location_override") or "").strip()
    if not path:
        return jsonify({"error": "path required"}), 400

    ext = os.path.splitext(path)[1].lower()
    is_video = ext in ('.mp4', '.mov')

    try:
        frames_b64 = []
        if is_video:
            probe = _sp.run(
                [find_binary("ffprobe"), "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", path],
                capture_output=True, text=True, timeout=15
            )
            probe_data = _json.loads(probe.stdout) if probe.stdout.strip() else {}
            duration = float((probe_data.get("format") or {}).get("duration", 0) or 0)
            if not duration:
                for stream in probe_data.get("streams", []):
                    if stream.get("codec_type") == "video" and stream.get("duration"):
                        duration = float(stream["duration"]); break
            t = min(2.0, duration * 0.2) if duration else 0.0
            end = duration - 0.5 if duration else 1.0
            while t < end and len(frames_b64) < 3:
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp_path = tmp.name
                _sp.run([find_binary("ffmpeg"), "-ss", str(t), "-i", path,
                         "-vframes", "1", "-vf", "scale=1200:-1", "-update", "1", tmp_path, "-y"],
                        capture_output=True, timeout=30)
                if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                    frames_b64.append(_resize_image_b64(tmp_path, max_dim=1200))
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                t += 2.0
                if not duration: break
            if not frames_b64:
                return jsonify({"error": "could not extract frames from video"}), 500
        else:
            frames_b64 = [_resize_image_b64(path, max_dim=1200)]
    except Exception as e:
        return jsonify({"error": f"media load failed: {e}"}), 500

    # Auto mode: fall back to GPS-derived location context when no manual override
    if not location_override:
        location_override = _geotag_location_hint(path, GOOGLE_MAPS_API_KEY)

    location_hint = f" in {location_override}" if location_override else ""
    media_type = "video clip" if is_video else "photo"
    prompt = (
        f"This {media_type} was taken{location_hint}. "
        "Identify any specific named landmarks, buildings, streets, districts, parks, or locations visible. "
        "Return ONLY a JSON array of proper nouns — official names suitable as stock photo keywords. "
        'Example: ["Longshan Temple", "Wanhua District", "Taipei", "Taiwan"]. '
        "If uncertain about a name include it with a ? suffix. "
        "If nothing identifiable is visible return []."
    )
    payload = _json.dumps({
        "model": active_model,
        "max_tokens": 256,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": [
            *[{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in frames_b64],
            {"type": "text", "text": prompt},
        ]}],
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    api_key = llm_config.get_api_key()
    try:
        req = _urllib.Request(llm_config.get_chat_url(), data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        with _urllib.urlopen(req, timeout=90) as resp:
            raw = _strip_thinking(_json.loads(resp.read())["choices"][0]["message"]["content"].strip())
        raw = raw.strip('`').strip()
        if raw.startswith('json'): raw = raw[4:].strip()
        landmarks = _json.loads(raw)
        if not isinstance(landmarks, list): landmarks = []
        return jsonify({"landmarks": [str(x) for x in landmarks if x]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dirty")
def api_dirty():
    """Get list of dirty files."""
    assets = get_dirty_assets()

    import hashlib
    files = []
    for asset in assets:
        cache_key = hashlib.sha1(f"{asset['path']}:{asset['file_mtime']}".encode()).hexdigest()
        thumb_path = os.path.join(THUMB_CACHE_DIR, f"{cache_key}.jpg")
        files.append({
            "path": asset["path"],
            "filename": asset["filename"],
            "folder": asset["folder"],
            "thumb_url": f"/thumbs/{cache_key}.jpg" if os.path.exists(thumb_path) else None,
            "is_dirty": 1,
            "title": asset["title"],
            "description": asset["description"],
            "keywords": asset["keywords"],
        })

    return jsonify(files)


@app.route("/api/generate-metadata", methods=["POST"])
def api_generate_metadata():
    """Generate title, description, keywords for an image using the configured vision model."""
    if not getattr(g, "metadata_llm_locked", False):
        with metadata_llm_lock:
            g.metadata_llm_locked = True
            try:
                return api_generate_metadata()
            finally:
                g.metadata_llm_locked = False

    data = request.get_json() or {}
    path = data.get("path")
    location_override = (data.get("location_override") or "").strip()
    manual_context_hint = (data.get("context_hint") or "").strip()[:1000]
    if not path:
        return jsonify({"error": "path required"}), 400

    asset = get_asset_by_path(path)
    if not asset:
        return jsonify({"error": "asset not found"}), 404

    try:
        is_video = os.path.splitext(path)[1].lower() in ('.mp4', '.mov')
        frames_b64 = _get_prepared_frames(path)
    except Exception as e:
        return jsonify({"error": f"image load failed: {e}"}), 500

    context_hint = _merge_hint_texts(manual_context_hint)

    result = generate_stock_metadata(
        path, asset, frames_b64, is_video,
        location_override=location_override,
        context_hint=context_hint,
        active_model=active_model,
        api_key=llm_config.get_api_key(),
        llm_chat_url=llm_config.get_chat_url(),
        google_maps_api_key=GOOGLE_MAPS_API_KEY,
    )
    if "error" in result:
        return jsonify(result), 500

    update_metadata([path], result["title"], result["description"], result["keywords"])
    print(f"[gen-metadata] saved {os.path.basename(path)}: {result['title'][:120]}")
    return jsonify({"success": True, **result})


@app.route("/api/load-folders", methods=["POST"])
def api_load_folders():
    """Load combined files from multiple folders (multi-folder selection)."""
    import hashlib
    from models.db import get_assets_by_folder

    data = request.get_json() or {}
    folder_paths = data.get("folders", [])
    if not folder_paths:
        return jsonify({"error": "folders required"}), 400

    all_assets = []
    for folder_path in folder_paths:
        assets = get_assets_by_folder(folder_path)
        all_assets.extend(assets)

    all_assets.sort(key=lambda a: a.get("capture_date") or "9999")

    files = []
    for asset in all_assets:
        cache_key = hashlib.sha1(f"{asset['path']}:{asset['file_mtime']}".encode()).hexdigest()
        thumb_path = os.path.join(THUMB_CACHE_DIR, f"{cache_key}.jpg")
        files.append({
            "path": asset["path"],
            "filename": asset["filename"],
            "media_type": asset["media_type"],
            "thumb_url": f"/thumbs/{cache_key}.jpg" if os.path.exists(thumb_path) else None,
            "capture_date": asset["capture_date"],
            "is_dirty": asset.get("is_dirty", 0),
            "has_metadata": asset.get("title") is not None or asset.get("description") is not None,
            "missing_metadata": not asset.get("title") or not asset.get("description") or not asset.get("keywords"),
            "title": asset.get("title") or "",
            "keywords": asset.get("keywords") or [],
        })

    return jsonify({"files": files, "metadata_loading": False, "thumbs_loading": False})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Refresh database by scanning all roots."""
    count = refresh_database()
    return jsonify({"success": True, "count": count})


@app.route("/api/refresh-progress", methods=["GET"])
def api_refresh_progress():
    """Get progress of ongoing refresh operation."""
    folder = request.args.get("folder", "")

    if not folder:
        return jsonify({"error": "folder parameter required"}), 400

    progress = refresh_progress.get(folder, {"processed": 0, "total": 0})
    return jsonify(progress)


@app.route("/api/background-status", methods=["GET"])
def api_background_status():
    """Get status of all background tasks (metadata + thumbnails)."""
    # Check if metadata is loading
    metadata_loading = len(pending_metadata_loads) > 0
    metadata_folder = list(pending_metadata_loads.keys())[0] if metadata_loading else ""

    # Get thumbnail generation status
    thumbs_loading = thumb_generation_status['active']
    thumbs_count = thumb_generation_status['count']
    thumbs_folder = thumb_generation_status['folder']

    # Bulk metadata loader status
    bulk_active = bulk_meta_status['active']
    bulk_done   = bulk_meta_status['done']
    bulk_total  = bulk_meta_status['total']

    # Bulk thumbnail status
    bthumb_active = bulk_thumb_status['active']
    bthumb_done   = bulk_thumb_status['done']
    bthumb_total  = bulk_thumb_status['total']

    # Build status message
    status_parts = []
    if bulk_active:
        status_parts.append(f"Metadata: {bulk_done}/{bulk_total} files" if bulk_total > 0 else "Metadata: scanning...")
    elif metadata_loading:
        progress = refresh_progress.get(metadata_folder, {"processed": 0, "total": 0})
        status_parts.append(f"Metadata: {progress.get('processed', 0)}/{progress.get('total', 0)}")
    if bthumb_active:
        status_parts.append(f"Thumbnails: {bthumb_done}/{bthumb_total}")
    elif thumbs_loading:
        status_parts.append(f"Thumbnails: {thumbs_count} files")

    return jsonify({
        "metadata_loading": metadata_loading or bulk_active,
        "metadata_folder": bulk_meta_status['current_folder'] if bulk_active else metadata_folder,
        "metadata_progress": {"processed": bulk_done, "total": bulk_total} if bulk_active else
                             refresh_progress.get(metadata_folder, {"processed": 0, "total": 0}) if metadata_loading else None,
        "thumbs_loading": thumbs_loading,
        "thumbs_count": thumbs_count,
        "thumbs_folder": thumbs_folder,
        "bulk_meta_active": bulk_active,
        "bulk_meta_done": bulk_done,
        "bulk_meta_total": bulk_total,
        "bulk_thumb_active": bthumb_active,
        "bulk_thumb_done": bthumb_done,
        "bulk_thumb_total": bthumb_total,
        "status_text": " | ".join(status_parts) if status_parts else "Idle",
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Scan a specific folder (fast scan - just file listing)."""
    from core.scanner import scan_root_fast
    from models.db import upsert_assets

    data = request.get_json() or {}
    folder_path = data.get("folder")

    if not folder_path:
        return jsonify({"error": "folder parameter required"}), 400

    # Fast scan - just file listing
    assets = scan_root_fast(folder_path)

    # Upsert into database in batches
    batch_size = 100
    for i in range(0, len(assets), batch_size):
        batch = assets[i:i + batch_size]
        upsert_assets(batch)

    return jsonify({
        "success": True,
        "count": len(assets),
        "folder": folder_path,
    })



@app.route("/api/load-folder", methods=["POST"])
def api_load_folder():
    """Load metadata and thumbnails for a specific folder on-demand."""
    from core.scanner import scan_root, scan_root_fast
    from core.thumbs import get_or_generate_thumbnail
    from models.db import get_assets_by_folder, upsert_assets, clear_folder_dirty

    data = request.get_json() or {}
    folder_path = data.get("folder")
    refresh = data.get("refresh", False)

    if not folder_path:
        return jsonify({"error": "folder parameter required"}), 400

    # Always re-scan with metadata if refresh is true
    if refresh:
        # Cancel any pending metadata load for this folder
        if folder_path in pending_metadata_loads:
            cancel_pending_metadata_load(folder_path)

        # Fast scan first to get file count immediately
        assets = scan_root_fast(folder_path)
        total_files = len(assets)

        # Check which thumbnails are missing to generate in background
        thumbs_to_generate = []
        for asset in assets:
            import hashlib
            cache_key = hashlib.sha1(f"{asset['path']}:{asset['file_mtime']}".encode()).hexdigest()
            thumb_path = os.path.join(THUMB_CACHE_DIR, f"{cache_key}.jpg")
            if not os.path.exists(thumb_path):
                thumbs_to_generate.append((asset["path"], asset["file_mtime"]))

        # Start full metadata scan in background
        try:
            future = executor.submit(_submit_metadata_load, folder_path, is_refresh=True)
            pending_metadata_loads[folder_path] = future
            print(f"Started background metadata refresh for: {folder_path}")
        except Exception as e:
            print(f"Error starting background metadata refresh: {e}")
            # Fallback to synchronous if background fails
            assets = scan_root(folder_path)
            upsert_assets(assets)
            clear_folder_dirty(folder_path)

        # Upsert fast scan results and clear dirty flags
        upsert_assets(assets)
        clear_folder_dirty(folder_path)

        # Generate missing thumbnails in background
        thumbs_loading = len(thumbs_to_generate) > 0
        if thumbs_loading:
            print(f"Starting background thumbnail generation for {len(thumbs_to_generate)} files")
            thumb_generation_status['active'] = True
            thumb_generation_status['count'] = len(thumbs_to_generate)
            thumb_generation_status['folder'] = folder_path
            try:
                def generate_thumbs():
                    for idx, (path, mtime) in enumerate(thumbs_to_generate):
                        try:
                            get_or_generate_thumbnail(path, mtime)
                            if (idx + 1) % 10 == 0:
                                print(f"Generated {idx + 1}/{len(thumbs_to_generate)} thumbnails")
                        except Exception as e:
                            print(f"Error generating thumbnail for {path}: {e}")
                    thumb_generation_status['active'] = False
                    thumb_generation_status['count'] = 0
                    thumb_generation_status['folder'] = ''
                    print(f"Thumbnail generation complete for {folder_path}")
                executor.submit(generate_thumbs)
            except Exception as e:
                print(f"Error submitting thumbnail generation task: {e}")
                thumb_generation_status['active'] = False

        # Build response with metadata_loading flag
        assets.sort(key=lambda a: a.get("capture_date") or "9999")
        files = []
        for asset in assets:
            import hashlib
            cache_key = hashlib.sha1(f"{asset['path']}:{asset['file_mtime']}".encode()).hexdigest()
            thumb_path = os.path.join(THUMB_CACHE_DIR, f"{cache_key}.jpg")

            thumb_url = None
            if os.path.exists(thumb_path):
                thumb_url = f"/thumbs/{cache_key}.jpg"

            files.append({
                "path": asset["path"],
                "filename": asset["filename"],
                "media_type": asset["media_type"],
                "thumb_url": thumb_url,
                "capture_date": asset["capture_date"],
                "is_dirty": asset.get("is_dirty", 0),
                "has_metadata": asset.get("title") is not None or asset.get("description") is not None,
                "missing_metadata": not asset.get("title") or not asset.get("description") or not asset.get("keywords"),
            "title": asset.get("title") or "",
            "keywords": asset.get("keywords") or [],
            })

        return jsonify({
            "files": files,
            "metadata_loading": True,
            "thumbs_loading": thumbs_loading,
            "total_files": total_files,
        })
    else:
        # First check if folder already has assets in DB
        existing_assets = get_assets_by_folder(folder_path)

        # If no assets exist, do a fast scan first (no metadata)
        if not existing_assets:
            # Cancel all pending metadata loads - user switched folders
            # This ensures the new folder gets priority
            cancel_all_pending_metadata_loads()

            # Fast scan - just file listing
            assets = scan_root_fast(folder_path)
            upsert_assets(assets)

            # Load metadata in background — skip if bulk loader already handling it
            if not bulk_meta_status['active']:
                try:
                    future = executor.submit(_submit_metadata_load, folder_path)
                    pending_metadata_loads[folder_path] = future
                    print(f"[BACKGROUND] Started metadata load for: {folder_path}")
                except Exception as e:
                    print(f"Error starting background metadata load: {e}")
                    pass
            else:
                print(f"[BACKGROUND] Skipping per-folder load for {folder_path} — bulk loader active")
        else:
            # Assets exist - count how many have never been read (all three NULL = never processed)
            # Empty string means "exiftool ran, found nothing" — don't re-trigger
            files_without_metadata = sum(
                1 for a in existing_assets
                if a.get("title") is None and a.get("description") is None and a.get("keywords") is None
            )
            files_with_metadata = len(existing_assets) - files_without_metadata

            print(f"[BACKGROUND] Folder {folder_path}: {files_with_metadata}/{len(existing_assets)} files have metadata")

            # If ANY files lack metadata, load in background — but not if bulk loader is handling it
            if files_without_metadata > 0 and not bulk_meta_status['active']:
                if folder_path in pending_metadata_loads:
                    # Already loading this folder — don't cancel and restart (prefetch re-trigger guard)
                    print(f"[BACKGROUND] Already loading {folder_path}, skipping re-trigger")
                else:
                    cancel_all_pending_metadata_loads()
                    try:
                        future = executor.submit(_submit_metadata_load, folder_path)
                        pending_metadata_loads[folder_path] = future
                        print(f"[BACKGROUND] Started metadata load for: {folder_path} ({files_without_metadata} files)")
                    except Exception as e:
                        print(f"Error starting background metadata load: {e}")

            # Assets exist - return them as-is
            assets = existing_assets

    # Build response - thumbnails will be loaded on-demand via /thumbs/ endpoint
    assets.sort(key=lambda a: a.get("capture_date") or "9999")
    files = []
    metadata_loading = False
    thumbs_loading = False
    thumbs_to_generate = []

    for asset in assets:
        # Build thumb_url using the hash-based naming from get_thumb_path
        import hashlib
        cache_key = hashlib.sha1(f"{asset['path']}:{asset['file_mtime']}".encode()).hexdigest()
        thumb_path = os.path.join(THUMB_CACHE_DIR, f"{cache_key}.jpg")

        # Only include thumb_url if thumbnail exists RIGHT NOW
        thumb_url = None
        if os.path.exists(thumb_path):
            thumb_url = f"/thumbs/{cache_key}.jpg"
        else:
            # Collect thumbnails to generate
            thumbs_to_generate.append((asset["path"], asset["file_mtime"]))

        # Check if this is a fast-scan result (no metadata)
        if not asset.get("title") and not asset.get("description"):
            # Metadata will be loaded in background
            metadata_loading = True

        files.append({
            "path": asset["path"],
            "filename": asset["filename"],
            "media_type": asset["media_type"],
            "thumb_url": thumb_url,
            "capture_date": asset["capture_date"],
            "is_dirty": asset.get("is_dirty", 0),
            "has_metadata": asset.get("title") is not None or asset.get("description") is not None,
            "missing_metadata": not asset.get("title") or not asset.get("description") or not asset.get("keywords"),
            "title": asset.get("title") or "",
            "keywords": asset.get("keywords") or [],
        })

    # Start background thumbnail generation if needed
    if thumbs_to_generate:
        thumbs_loading = True
        print(f"[BACKGROUND] Starting thumbnail generation for {len(thumbs_to_generate)} files in {folder_path}")
        thumb_generation_status['active'] = True
        thumb_generation_status['count'] = len(thumbs_to_generate)
        thumb_generation_status['folder'] = folder_path

        def generate_thumbs():
            completed = 0
            for idx, (path, mtime) in enumerate(thumbs_to_generate):
                try:
                    get_or_generate_thumbnail(path, mtime)
                    completed += 1
                    if completed % 10 == 0:
                        print(f"[BACKGROUND] Generated {completed}/{len(thumbs_to_generate)} thumbnails")
                except Exception as e:
                    print(f"[BACKGROUND] Error generating thumbnail for {path}: {e}")
            thumb_generation_status['active'] = False
            thumb_generation_status['count'] = 0
            thumb_generation_status['folder'] = ''
            print(f"[BACKGROUND] Thumbnail generation complete for {folder_path}")

        try:
            executor.submit(generate_thumbs)
        except Exception as e:
            print(f"[BACKGROUND] Error submitting thumbnail task: {e}")
            thumb_generation_status['active'] = False

    return jsonify({
        "files": files,
        "metadata_loading": metadata_loading,  # Only true if metadata is being loaded
        "thumbs_loading": thumbs_loading  # True if thumbnails are being generated
    })


def _submit_metadata_load(folder_path: str, is_refresh: bool = False):
    """Submit a metadata load task for a folder."""
    _load_metadata_background(folder_path, is_refresh)


def _load_metadata_background(folder_path: str, is_refresh: bool = False):
    """Load metadata in background (called via executor).

    Args:
        folder_path: Path to folder
        is_refresh: If True, also clear dirty flags
    """
    from core.scanner import scan_root
    from models.db import upsert_assets, clear_folder_dirty

    print(f"Background metadata loading for: {folder_path} (refresh={is_refresh})")

    # Track last processed count to ensure monotonic progress
    last_processed = [0]

    # Progress callback for tracking (ensure monotonic increase)
    def progress_callback(current, total):
        # Only update if current is greater than last recorded
        if current > last_processed[0]:
            last_processed[0] = current
            refresh_progress[folder_path] = {"processed": current, "total": total}

    # Scan with progress tracking. try/finally below ensures the folder is
    # always removed from pending_metadata_loads even if scan_root or the DB
    # write raises (e.g. a volume unmounts mid-scan) — otherwise the folder
    # gets stuck "loading" forever: the status bar keeps showing it as
    # in-progress, and the guard at the /api/load-folder call site refuses
    # to retry a folder already in pending_metadata_loads, so it never
    # recovers without a server restart.
    try:
        import json as _json
        assets = scan_root(folder_path, progress_callback=progress_callback)

        # Normalize None → '' for metadata fields so files are marked as "exiftool ran,
        # found nothing" rather than "never processed" — prevents infinite re-trigger loop.
        for asset in assets:
            if asset.get("title") is None:
                asset["title"] = ""
            if asset.get("description") is None:
                asset["description"] = ""
            if asset.get("keywords") is None:
                asset["keywords"] = _json.dumps([])

        upsert_assets(assets)

        # Clear dirty flags if this is a refresh operation
        if is_refresh:
            clear_folder_dirty(folder_path)
            print(f"Background metadata refresh complete for: {folder_path}")
        else:
            print(f"Background metadata load complete for: {folder_path}")
    except Exception as e:
        print(f"Error during background metadata load for {folder_path}: {e}")
    finally:
        # Remove from pending and progress tracking when done (or failed)
        if folder_path in pending_metadata_loads:
            del pending_metadata_loads[folder_path]
        if folder_path in refresh_progress:
            del refresh_progress[folder_path]


def cancel_pending_metadata_load(folder_path: str):
    """Cancel a pending metadata load for a folder."""
    if folder_path in pending_metadata_loads:
        future = pending_metadata_loads[folder_path]
        # Cancel if not yet started
        cancelled = future.cancel()
        if cancelled:
            del pending_metadata_loads[folder_path]
            print(f"Cancelled metadata load for: {folder_path}")


def cancel_all_pending_metadata_loads():
    """Cancel all pending metadata loads (used when switching folders)."""
    for folder_path in list(pending_metadata_loads.keys()):
        cancel_pending_metadata_load(folder_path)



@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    camera = request.args.get("camera", "").strip()
    lens = request.args.get("lens", "").strip()
    if len(q) < 2 and not camera and not lens:
        return jsonify({"files": [], "count": 0})
    assets = search_assets(q, camera=camera, lens=lens)
    files = []
    for asset in assets:
        cache_key = hashlib.sha1(f"{asset['path']}:{asset['file_mtime']}".encode()).hexdigest()
        thumb_path = os.path.join(THUMB_CACHE_DIR, f"{cache_key}.jpg")
        thumb_exists = os.path.exists(thumb_path)
        if not thumb_exists:
            executor.submit(generate_thumbnail, asset["path"], asset["file_mtime"])
        files.append({
            "path": asset["path"],
            "filename": asset["filename"],
            "folder": asset["folder"],
            "media_type": asset["media_type"],
            "thumb_url": f"/thumbs/{cache_key}.jpg" if thumb_exists else None,
            "capture_date": asset["capture_date"],
            "is_dirty": asset.get("is_dirty", 0),
            "camera_make": asset.get("camera_make") or "",
            "camera_model": asset.get("camera_model") or "",
            "lens_make": asset.get("lens_make") or "",
            "lens_model": asset.get("lens_model") or "",
            "lens_id": asset.get("lens_id") or "",
            "focal_length_mm": asset.get("focal_length_mm"),
            "focal_length_35mm_mm": asset.get("focal_length_35mm_mm"),
            "aperture_f_number": asset.get("aperture_f_number"),
            "has_metadata": asset.get("title") is not None or asset.get("description") is not None,
            "missing_metadata": not asset.get("title") or not asset.get("description") or not asset.get("keywords"),
            "title": asset.get("title") or "",
            "keywords": asset.get("keywords") or [],
        })
    return jsonify({"files": files, "count": len(files)})



@app.route("/api/move-files", methods=["POST"])
def api_move_files():
    """Move files to a different folder on disk and update DB."""
    import hashlib, shutil
    data = request.get_json() or {}
    paths = data.get("paths", [])
    target_folder = data.get("target_folder", "").rstrip("/")
    if not paths or not target_folder:
        return jsonify({"error": "paths and target_folder required"}), 400
    if not os.path.isdir(target_folder):
        return jsonify({"error": f"Target folder does not exist: {target_folder}"}), 400
    if not _path_under_roots(target_folder):
        return jsonify({"error": "target_folder is not under a watched folder"}), 400
    paths = [p for p in paths if _path_under_roots(p)]
    if not paths:
        return jsonify({"error": "no paths under a watched folder"}), 400

    moved, failed = [], []
    for path in paths:
        filename = os.path.basename(path)
        new_path = os.path.join(target_folder, filename)
        if os.path.exists(new_path):
            failed.append({"path": path, "error": "file already exists at destination"})
            continue
        try:
            # Get mtime before move (for thumbnail carry-over)
            old_mtime = os.path.getmtime(path) if os.path.exists(path) else None
            os.rename(path, new_path)
            new_mtime = os.path.getmtime(new_path)

            # Carry thumbnail to new cache key
            if old_mtime:
                old_key = hashlib.sha1(f"{path}:{old_mtime}".encode()).hexdigest()
                new_key = hashlib.sha1(f"{new_path}:{new_mtime}".encode()).hexdigest()
                old_thumb = os.path.join(THUMB_CACHE_DIR, f"{old_key}.jpg")
                new_thumb = os.path.join(THUMB_CACHE_DIR, f"{new_key}.jpg")
                if os.path.exists(old_thumb) and not os.path.exists(new_thumb):
                    shutil.copy2(old_thumb, new_thumb)

            move_asset(path, new_path, target_folder)
            moved.append({"old_path": path, "new_path": new_path})
        except Exception as e:
            failed.append({"path": path, "error": str(e)})

    return jsonify({"moved": moved, "failed": failed})


@app.route("/api/open-finder", methods=["POST"])
def api_open_finder():
    """Open file in Finder (macOS)."""
    data = request.get_json() or {}
    path = data.get("path")

    # Handle case where path might be nested dict
    if isinstance(path, dict):
        path = path.get("path")

    if not path:
        return jsonify({"error": "path required"}), 400
    if not _path_under_roots(path):
        return jsonify({"error": "path is not under a watched folder"}), 400

    try:
        subprocess.run(["open", "-R", path], check=True)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/thumbs/<filename>")
def serve_thumbnail(filename):
    """Serve a thumbnail from the cache, generating if needed."""
    from flask import send_from_directory
    from core.thumbs import get_or_generate_thumbnail

    # The filename is {hash}.jpg - we need to find the original file
    # Since we can't reverse the hash, we'll just try to serve from cache
    # If it doesn't exist, generate all thumbnails in background and return 404

    # First try to serve from cache
    if os.path.exists(os.path.join(THUMB_CACHE_DIR, filename)):
        return send_from_directory(THUMB_CACHE_DIR, filename)

    # Thumbnail doesn't exist - trigger background generation for all pending
    # and return a 404 for now (browser will retry or show broken image)
    # The background worker from /api/load-folder should have already started
    # generating, so this is just a fallback

    return "", 404


@app.route("/api/preview")
def api_preview():
    """Serve original image file for single-file preview in metadata panel."""
    from flask import send_file, abort
    path = request.args.get("path", "")
    if not path or not os.path.isfile(path) or not _path_under_roots(path):
        abort(404)
    return send_file(path)


# ---------------------------------------------------------------------------
# Bulk background metadata loader
# ---------------------------------------------------------------------------

def _run_bulk_metadata_load():
    """Load metadata for every file in the DB that has no title/description/keywords.

    Works file-by-file using paths already known to the DB — no filesystem walk needed.
    Skips files that don't exist (e.g. volume not mounted) gracefully.
    Not cancelled on folder switch — runs to completion independently.
    """
    import sqlite3
    import json as _json
    from core.metadata import read_metadata
    from config import DATABASE_PATH

    try:
        # Fetch all assets with no metadata directly from DB
        # Use timeout to avoid hanging if DB is locked by another writer
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # NULL means "never read by exiftool" — empty string means "read, found nothing".
        # Only pick up files that have never been read (all three are NULL).
        cur.execute(
            "SELECT path FROM assets WHERE "
            "title IS NULL AND description IS NULL AND keywords IS NULL "
            "ORDER BY folder, path"
        )
        rows = cur.fetchall()

        # Also backfill capture_date for files that have AI metadata but missing date
        cur.execute(
            "SELECT path FROM assets WHERE capture_date IS NULL "
            "AND (title IS NOT NULL OR description IS NOT NULL OR keywords IS NOT NULL) "
            "ORDER BY folder, path"
        )
        date_rows = cur.fetchall()
        conn.close()

        # Hoisted out of the comprehension: building this set once instead of
        # once per date_rows entry turns an O(n*m) rebuild into O(n+m) — with
        # a few thousand rows in each list this was measurably slow (~1.4s
        # for 8k rows) blocking the bulk loader's startup for no reason.
        rows_paths = {r['path'] for r in rows}
        date_only_paths = [r['path'] for r in date_rows if r['path'] not in rows_paths]

        if not rows and not date_only_paths:
            print("[bulk-meta] All files already have metadata.")
            return

        paths = [r['path'] for r in rows]
        bulk_meta_status['total'] = len(paths) + len(date_only_paths)
        bulk_meta_status['done'] = 0
        if date_only_paths:
            print(f"[bulk-meta] Backfilling capture_date for {len(date_only_paths)} files")
        print(f"[bulk-meta] Starting: {len(paths)} files need metadata")

        # Use WAL mode + timeout so concurrent writers don't cause lock errors
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()

        for path in paths:
            bulk_meta_status['current_folder'] = os.path.dirname(path)

            if not os.path.exists(path):
                bulk_meta_status['done'] += 1
                continue  # Volume not mounted — skip silently

            try:
                # Re-check DB: another worker may have already populated this file
                cur.execute(
                    "SELECT title, description, keywords FROM assets WHERE path=?", (path,)
                )
                row = cur.fetchone()
                if row and (row[0] is not None or row[1] is not None or row[2] is not None):
                    bulk_meta_status['done'] += 1
                    continue  # No longer NULL — skip

                meta = read_metadata(path)
                # Always write back even if empty — converts NULL→'' so this file
                # won't appear in the "never read" list on the next startup.
                cur.execute(
                    "UPDATE assets SET title=?, description=?, keywords=?, capture_date=COALESCE(capture_date, ?) WHERE path=?",
                    (
                        meta.get('title') or '',
                        meta.get('description') or '',
                        _json.dumps(meta.get('keywords') or []),
                        meta.get('capture_date'),
                        path,
                    ),
                )
                conn.commit()
            except Exception as e:
                print(f"[bulk-meta] Error processing {path}: {e}")

            bulk_meta_status['done'] += 1

        # Backfill capture_date for files that already have AI metadata but missing date
        if date_only_paths:
            import datetime as _dt
            conn2 = sqlite3.connect(DATABASE_PATH, timeout=10)
            conn2.execute("PRAGMA journal_mode=WAL")
            cur2 = conn2.cursor()
            for path in date_only_paths:
                try:
                    date_to_store = None
                    if os.path.exists(path):
                        meta = read_metadata(path)
                        date_to_store = meta.get('capture_date')
                    if not date_to_store:
                        # Fall back to file_mtime so this file stops re-appearing every startup
                        cur2.execute("SELECT file_mtime FROM assets WHERE path=?", (path,))
                        mrow = cur2.fetchone()
                        if mrow and mrow[0]:
                            date_to_store = _dt.datetime.fromtimestamp(float(mrow[0])).strftime('%Y:%m:%d %H:%M:%S')
                    cur2.execute(
                        "UPDATE assets SET capture_date=? WHERE path=? AND capture_date IS NULL",
                        (date_to_store or '', path)
                    )
                    conn2.commit()
                except Exception as e:
                    print(f"[bulk-meta] capture_date backfill error for {path}: {e}")
                finally:
                    bulk_meta_status['done'] += 1
            conn2.close()
            print(f"[bulk-meta] capture_date backfill complete for {len(date_only_paths)} files")

        conn.close()
        print(f"[bulk-meta] Complete: {bulk_meta_status['done']}/{bulk_meta_status['total']} files processed")

    except Exception as e:
        print(f"[bulk-meta] Fatal error, aborting: {e}")
    finally:
        bulk_meta_status['active'] = False
        bulk_meta_status['current_folder'] = ''


def _run_bulk_thumbnail_generation():
    """Generate thumbnails for every file in the DB that doesn't have one cached yet.

    Runs as a single independent background job alongside the metadata loader.
    Skips files that don't exist (volume not mounted) or already have thumbnails.
    """
    import hashlib
    import sqlite3
    from core.thumbs import get_or_generate_thumbnail
    from config import DATABASE_PATH, THUMB_CACHE_DIR

    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT path, file_mtime FROM assets ORDER BY folder, path")
        rows = cur.fetchall()
        conn.close()

        # Filter to only those missing a thumbnail
        missing = []
        for row in rows:
            path, mtime = row['path'], row['file_mtime']
            if mtime is None:
                continue
            cache_key = hashlib.sha1(f"{path}:{mtime}".encode()).hexdigest()
            if not os.path.exists(os.path.join(THUMB_CACHE_DIR, f"{cache_key}.jpg")):
                missing.append((path, mtime))

        if not missing:
            print("[bulk-thumb] All thumbnails already generated.")
            return

        bulk_thumb_status['total'] = len(missing)
        bulk_thumb_status['done'] = 0
        print(f"[bulk-thumb] Starting: {len(missing)} thumbnails to generate")

        for path, mtime in missing:
            if not os.path.exists(path):
                bulk_thumb_status['done'] += 1
                continue
            try:
                get_or_generate_thumbnail(path, mtime)
            except Exception as e:
                print(f"[bulk-thumb] Error generating thumbnail for {path}: {e}")
            bulk_thumb_status['done'] += 1

        print(f"[bulk-thumb] Complete: {bulk_thumb_status['done']}/{bulk_thumb_status['total']} thumbnails generated")

    except Exception as e:
        print(f"[bulk-thumb] Fatal error, aborting: {e}")
    finally:
        bulk_thumb_status['active'] = False


@app.route("/api/thumbnails/generate-all-missing", methods=["POST"])
def api_generate_all_missing_thumbnails():
    """Start bulk thumbnail generation (idempotent — ignores if already running)."""
    with _bulk_meta_lock:
        if bulk_thumb_status['active']:
            return jsonify({
                "started": False,
                "message": "Already running",
                "done": bulk_thumb_status['done'],
                "total": bulk_thumb_status['total'],
            })
        bulk_thumb_status['active'] = True
        bulk_thumb_status['done'] = 0
        bulk_thumb_status['total'] = 0
    executor.submit(_run_bulk_thumbnail_generation)
    return jsonify({"started": True})


@app.route("/api/metadata/load-all-missing", methods=["POST"])
def api_load_all_missing():
    """Start the bulk background metadata loader (idempotent — ignores if already running)."""
    with _bulk_meta_lock:
        if bulk_meta_status['active']:
            return jsonify({
                "started": False,
                "message": "Already running",
                "done": bulk_meta_status['done'],
                "total": bulk_meta_status['total'],
            })
        bulk_meta_status['active'] = True
        bulk_meta_status['done'] = 0
        bulk_meta_status['total'] = 0
    executor.submit(_run_bulk_metadata_load)
    return jsonify({"started": True})


# ---------------------------------------------------------------------------
# Export routes (CSV generation + FTP upload)
# ---------------------------------------------------------------------------

@app.route("/api/microstock-credentials", methods=["GET"])
def api_microstock_credentials_get():
    """Return stock-site FTP/SFTP credentials, minus the actual passwords.

    A site with no host/user/password saved is simply omitted from upload —
    see the "skip if no credentials" logic in core/microstock.py.
    """
    from core.microstock import MICROSTOCK_CONFIG_PATH, SITE_MATRIX
    sites_known = sorted({s for group in SITE_MATRIX.values() for s in group})
    try:
        with open(MICROSTOCK_CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}

    sites = {}
    for site in sites_known:
        cfg = raw.get(site) or {}
        sites[site] = {
            "host": cfg.get("host", ""),
            "user": cfg.get("user", ""),
            "has_pass": bool(cfg.get("pass")),
            "sftp": bool(cfg.get("sftp", False)),
            "tls": cfg.get("tls"),  # null = use the site's default
            "remote_dir": cfg.get("remote_dir", ""),
        }
    return jsonify({"sites": sites, "path": str(MICROSTOCK_CONFIG_PATH)})


@app.route("/api/microstock-credentials", methods=["POST"])
def api_microstock_credentials_post():
    """Save stock-site FTP/SFTP credentials.

    A blank password field means "keep the existing password" (it's never
    sent back to the browser to display, so there's nothing to compare it
    against) — clear host+user+password together to fully remove a site.
    """
    from core.microstock import MICROSTOCK_CONFIG_PATH, SITE_MATRIX
    sites_known = sorted({s for group in SITE_MATRIX.values() for s in group})

    data = request.get_json() or {}
    sites_in = data.get("sites") or {}
    if not isinstance(sites_in, dict):
        return jsonify({"error": "expected sites to be an object"}), 400

    try:
        with open(MICROSTOCK_CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}

    for site in sites_known:
        incoming = sites_in.get(site)
        if incoming is None:
            continue
        host = str(incoming.get("host") or "").strip()
        user = str(incoming.get("user") or "").strip()
        password = str(incoming.get("pass") or "").strip()
        remote_dir = str(incoming.get("remote_dir") or "").strip()
        sftp = bool(incoming.get("sftp", False))
        tls = incoming.get("tls")

        if not host and not user and not password:
            raw.pop(site, None)
            continue

        existing = raw.get(site) or {}
        entry = {
            "host": host,
            "user": user,
            "pass": password or existing.get("pass", ""),
        }
        if sftp:
            entry["sftp"] = True
        if tls is not None:
            entry["tls"] = bool(tls)
        if remote_dir:
            entry["remote_dir"] = remote_dir
        raw[site] = entry

    MICROSTOCK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MICROSTOCK_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)

    return jsonify({
        "success": True,
        "configured_sites": [s for s in sites_known if s in raw],
    })


@app.route("/api/export/folders")
def api_export_folders():
    """List all folders with export-relevant metadata from the DB."""
    from core.microstock import scan_export_folders, CATEGORY_MAP, _load_ftp_config
    folders = scan_export_folders()
    config = _load_ftp_config()
    return jsonify({
        "folders": folders,
        "categories": list(CATEGORY_MAP.keys()),
        "configured_sites": [k for k in config.keys() if not k.startswith('_')],
        "network_drives": config.get("_network_drives", []),
    })


@app.route("/api/export/mount-drives", methods=["POST"])
def api_mount_drives():
    """Mount all configured network drives via osascript."""
    import subprocess
    from core.microstock import _load_ftp_config
    config = _load_ftp_config()
    drives = config.get("_network_drives", [])
    results = []
    for drive in drives:
        try:
            subprocess.run(
                ["osascript", "-e", f'mount volume "{drive}"'],
                timeout=15, capture_output=True
            )
            results.append({"drive": drive, "ok": True})
        except Exception as e:
            results.append({"drive": drive, "ok": False, "error": str(e)})
    return jsonify({"results": results})


@app.route("/api/export/generate-csv")
def api_export_generate_csv():
    """SSE stream: generate Shutterstock + Adobe Stock CSVs for a folder."""
    from flask import Response
    from core.microstock import generate_csvs_gen

    folder_path = request.args.get("folder")
    category    = request.args.get("category")  # None = AI Auto

    if not folder_path:
        return jsonify({"error": "folder parameter required"}), 400

    def generate():
        for line in generate_csvs_gen(folder_path, category if category and category != "AI_AUTO" else None, model=active_model):
            yield line

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/export/download")
def api_export_download():
    """Download a generated CSV file for a folder."""
    from flask import send_file
    from pathlib import Path

    folder_path = request.args.get("folder")
    site        = request.args.get("site")  # "shutterstock" or "pond5"

    if not folder_path or not site:
        return jsonify({"error": "folder and site parameters required"}), 400

    folder = Path(folder_path)
    csv_names = {"shutterstock": "shutterstock_upload.csv", "pond5": "pond5_upload.csv"}
    if site not in csv_names:
        return jsonify({"error": f"unknown site: {site}"}), 400
    csv_path = folder / csv_names[site]

    if not csv_path.exists():
        return f"CSV not found. Generate it first.", 404

    return send_file(csv_path, as_attachment=True)


@app.route("/api/export/upload")
def api_export_upload():
    """SSE stream: FTP upload all media files in a folder."""
    from flask import Response
    from core.microstock import upload_folder_gen

    folder_path = request.args.get("folder")
    if not folder_path:
        return jsonify({"error": "folder parameter required"}), 400

    def generate():
        for line in upload_folder_gen(folder_path):
            yield line

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    # Initialize database
    init_db()
    interrupted = mark_running_jobs_interrupted()
    if interrupted:
        print(f"[jobs] Marked {interrupted} interrupted job(s) after server startup")

    # Binds to localhost only and runs without the interactive debugger by
    # default — several routes serve/delete/move files by path (scoped to
    # FOLDER_ROOTS, see _path_under_roots above, but still), and this app has
    # no auth in front of it. Set TOTAG_HOST to opt into binding elsewhere
    # (e.g. a Tailscale IP) if you specifically want LAN/remote access, and
    # TOTAG_DEBUG=1 for the reloader/debugger during development.
    # threaded=True: the folder picker (/api/pick-folder) blocks on a native
    # dialog until the user responds, which would otherwise stall every other
    # request (background polling, etc.) on the dev server's single thread.
    app.run(
        debug=os.environ.get("TOTAG_DEBUG", "").lower() in ("1", "true", "yes"),
        host=os.environ.get("TOTAG_HOST", "127.0.0.1"),
        port=int(os.environ.get("TOTAG_PORT", "5001")),
        threaded=True,
    )
