"""Microstock export module for ToTag.

Handles CSV generation (Shutterstock + Pond5) and FTP upload.
Reads metadata from the ToTag SQLite database instead of .metadata.json files.
AI categorisation uses the configured OpenAI-compatible LLM endpoint.
"""

import os
import csv
import json
import ftplib
import re
import difflib
import requests
import sqlite3
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BASE_DIR, DATABASE_PATH, FOLDER_ROOTS, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
import core.llm_config as llm_config

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# FTP/SFTP credentials per site. Never committed — see .gitignore. A site
# missing from this file is silently skipped during upload/CSV generation.
MICROSTOCK_CONFIG_PATH = Path(
    os.environ.get("TOTAG_MICROSTOCK_CONFIG_PATH")
    or os.path.join(BASE_DIR, "microstock_config.json")
)

# LLM base URL / API key are read live from core.llm_config (backed by
# os.environ) rather than cached here, so a base URL/key saved via the
# Model Settings modal at runtime takes effect immediately — including for
# jobs already using this module.
OMLX_MODEL = os.environ.get("TOTAG_LLM_MODEL", "gpt-4o-mini")

# Remote subdirectory to upload into per site (None = root)
SITE_REMOTE_DIRS = {
    "Alamy": "Stock",
}

SITE_MATRIX = {
    "images": ["Adobe Stock", "Shutterstock", "Dreamstime", "Alamy"],
    "clips":  ["Pond5", "Shutterstock", "Adobe Stock"],
}

SHUTTER_CATS = [
    "Abstract", "Animals/Wildlife", "Arts", "Backgrounds/Textures", "Beauty/Fashion",
    "Buildings/Landmarks", "Business/Finance", "Celebrities", "Editorial", "Education",
    "Food and Drink", "Healthcare/Medical", "Holidays", "Industrial", "Interiors",
    "Miscellaneous", "Nature", "Parks/Outdoor", "People", "Religion", "Science",
    "Signs/Symbols", "Sports/Recreation", "Technology", "Transportation", "Vectors", "Vintage",
]

# Shutterstock footage submissions don't accept these image-only categories
SHUTTER_CLIPS_INVALID = {
    "Abstract":      "Buildings/Landmarks",
    "Parks/Outdoor": "Nature",
    "Interiors":     "Buildings/Landmarks",
    "Vectors":       "Backgrounds/Textures",
    "Arts":          "Buildings/Landmarks",
    "Vintage":       "Buildings/Landmarks",
    "Miscellaneous": "Backgrounds/Textures",
    "Beauty/Fashion": "People",
    "Celebrities": "People",
}

# Handle common non-standard category names returned by the model and map to valid options.
SHUTTER_CATEGORY_ALIAS = {
    "Cityscape": "Buildings/Landmarks",
    "City": "Buildings/Landmarks",
    "Urban": "Buildings/Landmarks",
    "Architecture": "Buildings/Landmarks",
    "Landscapes/Nature": "Nature",
    "Landscape": "Nature",
    "Landscapes": "Nature",
    "Travel": "Nature",
    "Tourism": "Nature",
    "History": "Buildings/Landmarks",
    "Hobbies/Leisure": "Sports/Recreation",
}

CATEGORY_MAP = {
    "Nature / Landscapes":     {"shutterstock": "Nature"},
    "Animals / Wildlife":      {"shutterstock": "Animals/Wildlife"},
    "Buildings / Architecture":{"shutterstock": "Buildings/Landmarks"},
    "Business / Tech":         {"shutterstock": "Business/Finance"},
    "Food / Drink":            {"shutterstock": "Food and Drink"},
    "People / Lifestyle":      {"shutterstock": "People"},
    "Transportation":          {"shutterstock": "Transportation"},
    "Travel":                  {"shutterstock": "Nature"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UPLOAD_LOG_NAME = ".totag_uploads.json"


def _load_ftp_config() -> dict:
    if MICROSTOCK_CONFIG_PATH.exists():
        with open(MICROSTOCK_CONFIG_PATH) as f:
            return json.load(f)
    return {}


def _load_upload_log(folder: Path) -> dict:
    """Return {site: [filename, ...]} of already-uploaded files."""
    log_path = folder / UPLOAD_LOG_NAME
    if log_path.exists():
        try:
            return json.loads(log_path.read_text())
        except Exception:
            return {}
    return {}


def _save_upload_log(folder: Path, log: dict):
    (folder / UPLOAD_LOG_NAME).write_text(json.dumps(log, indent=2))


def _get_db_metadata(folder_path: str) -> dict[str, dict]:
    """Return {path: {title, description, keywords}} for all assets in folder from DB."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT path, filename, title, description, keywords FROM assets WHERE folder = ?",
        (folder_path,),
    )
    rows = cur.fetchall()
    conn.close()

    result = {}
    for row in rows:
        keywords = []
        if row["keywords"]:
            try:
                keywords = json.loads(row["keywords"])
            except (json.JSONDecodeError, TypeError):
                keywords = []
        result[row["path"]] = {
            "filename": row["filename"],
            "title": row["title"] or "",
            "description": row["description"] or "",
            "keywords": keywords,
        }
    return result


def _list_media_files(folder_path: str) -> list[Path]:
    """Return sorted list of image/video files in folder (no dotfiles)."""
    folder = Path(folder_path)
    if not folder.exists():
        return []
    files = []
    for f in sorted(folder.iterdir()):
        if f.name.startswith("."):
            continue
        if f.suffix.lower() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
            files.append(f)
    return files


def get_ai_categories(title: str, keywords: list[str], model: str = OMLX_MODEL) -> dict:
    """Ask oMLX to pick the best Shutterstock categories for one asset."""
    keyword_str = ", ".join(keywords[:15])
    prompt = (
        f"Given this microstock asset:\n"
        f"Title: {title}\n"
        f"Keywords: {keyword_str}\n\n"
        f"Select exactly TWO Shutterstock categories from this list: "
        f"{', '.join(SHUTTER_CATS)}\n"
        f"If it's a landscape/nature/animal shot, include \"Nature\" or \"Animals/Wildlife\".\n"
        f"If the asset appears editorial/release-sensitive — visible recognizable faces, "
        f"readable license plates, prominent vehicle logos, named businesses/venues, "
        f"private interiors, public art, signs, or protected landmarks as the subject — "
        f"include \"Editorial\" as one of the two categories.\n\n"
        f'Output ONLY valid JSON: {{"shutterstock": ["Cat1", "Cat2"]}}'
    )
    try:
        r = requests.post(
            llm_config.get_chat_url(),
            headers={"Authorization": f"Bearer {llm_config.get_api_key()}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 60,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=20,
        )
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"].strip()
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                result = json.loads(match.group(0))
                result["shutterstock"] = _snap_shutter_cats(result.get("shutterstock", []))
                return result
    except Exception as e:
        print(f"[microstock] AI categorisation failed: {e}")
    return {"shutterstock": ["Nature", "Miscellaneous"]}


def _snap_shutter_cats(cats: list) -> list:
    """Map AI-returned category names to the nearest valid Shutterstock category."""
    snapped = []
    for cat in cats:
        cat = str(cat).strip()
        if not cat:
            continue

        alias = SHUTTER_CATEGORY_ALIAS.get(cat)
        if alias:
            snapped.append(alias)
            continue
        # Exact match (case-insensitive)
        exact = next((c for c in SHUTTER_CATS if c.lower() == cat.lower()), None)
        if exact:
            snapped.append(exact)
            continue
        # Fuzzy match
        close = difflib.get_close_matches(cat, SHUTTER_CATS, n=1, cutoff=0.55)
        if close:
            snapped.append(close[0])
        else:
            print(f"[microstock] Unrecognised Shutterstock category '{cat}' — skipping")
    # Deduplicate, ensure at least one valid category
    seen = set()
    result = [c for c in snapped if not (c in seen or seen.add(c))]
    return result if result else ["Miscellaneous"]


def _normalize_clip_categories(cats: list[str]) -> list[str]:
    """Normalize categories for clips before writing the Shutterstock CSV."""
    remapped = []
    for c in cats:
        # Keep the current clip invalid map and apply alias/fuzzy normalization.
        remapped.append(SHUTTER_CLIPS_INVALID.get(c, c))
    return _snap_shutter_cats(remapped)


def _truncate_for_csv(value: str, max_len: int, *, word_boundary: bool = True) -> str:
    """Trim a CSV field to a marketplace character limit.

    Pond5 currently limits title to 80 characters. Prefer clean word-boundary
    truncation rather than mid-word cuts, but never exceed max_len.
    """
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= max_len:
        return text
    if word_boundary:
        cut = text[: max_len + 1]
        boundary = max(cut.rfind(" "), cut.rfind("-"))
        if boundary >= max_len * 0.65:
            text = text[:boundary].rstrip(" ,-–—")
        else:
            text = text[:max_len].rstrip(" ,-–—")
    else:
        text = text[:max_len].rstrip(" ,-–—")
    return text[:max_len]


def _shorten_pond5_title_with_llm(title: str, model: str = OMLX_MODEL) -> Optional[str]:
    """Use the configured fast LLM to rewrite a Pond5 title to <=80 chars.

    Hard truncation is a fallback only; LLM shortening keeps titles more natural
    for marketplace search/readability.
    """
    clean = " ".join(str(title or "").split()).strip()
    if len(clean) <= 80:
        return clean
    prompt = (
        "Rewrite this stock footage title to 80 characters or fewer. "
        "Keep it factual, searchable, and natural. No hype, no period, no quotes. "
        "Return ONLY the rewritten title.\n"
        f"Title: {clean}"
    )
    try:
        r = requests.post(
            llm_config.get_chat_url(),
            headers={"Authorization": f"Bearer {llm_config.get_api_key()}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 48,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=20,
        )
        if r.status_code != 200:
            return None
        text = r.json()["choices"][0]["message"]["content"].strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        text = text.splitlines()[-1].strip().strip('"\'').rstrip(".").strip()
        text = " ".join(text.split())
        if text and len(text) <= 80:
            return text
        if text:
            return _truncate_for_csv(text, 80, word_boundary=True)
    except Exception as e:
        print(f"[microstock] Pond5 title shortening failed: {e}")
    return None


def _pond5_title(title: str, model: str = OMLX_MODEL) -> tuple[str, str]:
    """Return (title, method) for Pond5 title field, max 80 characters."""
    clean = " ".join(str(title or "").split()).strip()
    if len(clean) <= 80:
        return clean, "unchanged"
    llm_title = _shorten_pond5_title_with_llm(clean, model=model)
    if llm_title:
        return llm_title, "llm"
    return _truncate_for_csv(clean, 80, word_boundary=True), "truncated"


# ---------------------------------------------------------------------------
# Folder scanning (for Export tab folder list)
# ---------------------------------------------------------------------------

def _folder_disk_info(folder: Path) -> tuple[bool, dict]:
    """Return (has_csvs, upload_log) for a folder. Safe to call in a thread."""
    try:
        shutter_csv = folder / "shutterstock_upload.csv"
        pond5_csv   = folder / "pond5_upload.csv"
        has_csvs = shutter_csv.exists() or pond5_csv.exists()
    except Exception:
        has_csvs = False
    upload_log = _load_upload_log(folder)
    return has_csvs, upload_log


def _pond5_csv_exists(folder: Path) -> bool:
    try:
        return (folder / "pond5_upload.csv").exists()
    except Exception:
        return False


def scan_export_folders() -> list[dict]:
    """Return list of exportable folders with metadata from the DB."""
    import concurrent.futures

    if not FOLDER_ROOTS:
        # No folder roots configured — nothing is in scope to export.
        return []

    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # Only scan folders under FOLDER_ROOTS
    root_filter = " OR ".join("folder LIKE ?" for _ in FOLDER_ROOTS)
    root_params = [r + "%" for r in FOLDER_ROOTS]
    cur.execute(
        "SELECT folder, COUNT(*) as count, "
        "SUM(CASE WHEN title IS NOT NULL AND title != '' THEN 1 ELSE 0 END) as with_metadata, "
        "SUM(CASE WHEN lower(substr(filename, instr(filename,'.'))) IN "
        "('.mp4','.mov','.avi','.mkv','.mts','.m4v','.wmv') THEN 1 ELSE 0 END) as clip_count, "
        "SUM(CASE WHEN lower(substr(filename, instr(filename,'.'))) IN "
        "('.jpg','.jpeg','.png','.tif','.tiff','.gif','.webp','.heic','.heif','.cr2','.cr3','.nef','.arw','.dng') THEN 1 ELSE 0 END) as image_count "
        f"FROM assets WHERE {root_filter} GROUP BY folder ORDER BY folder",
        root_params
    )
    rows = cur.fetchall()
    conn.close()

    # Run disk checks (CSV existence + upload log) in parallel with a 4s timeout per folder
    folder_paths = [row["folder"] for row in rows]
    disk_info: dict[str, tuple[bool, dict]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        future_map = {executor.submit(_folder_disk_info, Path(fp)): fp for fp in folder_paths}
        try:
            for future in concurrent.futures.as_completed(future_map, timeout=8):
                fp = future_map[future]
                try:
                    disk_info[fp] = future.result(timeout=4)
                except Exception:
                    disk_info[fp] = (False, {})
        except concurrent.futures.TimeoutError:
            # Partial results are fine — unmounted/slow volumes just show as offline
            for future, fp in future_map.items():
                if future.done():
                    try:
                        disk_info.setdefault(fp, future.result())
                    except Exception:
                        pass
    # Fill in any that didn't complete
    for fp in folder_paths:
        disk_info.setdefault(fp, (False, {}))

    folders = []
    for row in rows:
        folder_path = row["folder"]
        folder = Path(folder_path)

        # Determine type from DB counts — works even when volume is not mounted
        has_clips  = (row["clip_count"]  or 0) > 0
        has_images = (row["image_count"] or 0) > 0

        if has_clips and not has_images:
            ftype = "clips"
        elif has_images:
            ftype = "images"
        else:
            ftype = "images"  # safe default — show full site list rather than nothing

        is_editorial = "_ED" in folder.name

        sites = SITE_MATRIX.get(ftype, []).copy()
        if ftype in ("images", "mixed") and not is_editorial:
            if "Pond5" not in sites:
                sites.append("Pond5")
        if is_editorial:
            sites = [s for s in sites if s != "Adobe Stock"]

        has_csvs, upload_log = disk_info.get(folder_path, (False, {}))

        # Use media-only count (clips + images) so non-media DB entries don't inflate the total
        total_media = (row["clip_count"] or 0) + (row["image_count"] or 0)
        if total_media == 0:
            total_media = row["count"]  # fallback if extension counts unavailable
        site_upload_status = {}  # site -> "full" | "partial"
        for usite, logged_files in upload_log.items():
            if not logged_files:
                continue
            media_logged = [f for f in logged_files if not f.endswith('.csv')]
            csv_logged   = [f for f in logged_files if f.endswith('.csv')]
            if usite in ("Shutterstock", "Adobe Stock"):
                needs_csv = True
            elif usite == "Pond5":
                # Only require CSV if the pond5 CSV was actually generated
                needs_csv = any(f == "pond5_upload.csv" for f in logged_files) or _pond5_csv_exists(folder)
            else:
                needs_csv = False
            csv_ok = bool(csv_logged) if needs_csv else True
            if len(media_logged) >= total_media and csv_ok:
                site_upload_status[usite] = "full"
            else:
                site_upload_status[usite] = "partial"

        folders.append({
            "path": folder_path,
            "name": folder.name,
            "type": ftype,
            "is_editorial": is_editorial,
            "file_count": row["count"],
            "metadata_count": row["with_metadata"],
            "recommended_sites": sites,
            "has_csvs": has_csvs,
            "site_upload_status": site_upload_status,
        })

    return folders


# ---------------------------------------------------------------------------
# CSV generation (SSE generator)
# ---------------------------------------------------------------------------

def generate_csvs_gen(folder_path: str, default_category: Optional[str] = None, model: str = OMLX_MODEL):
    """
    Generator that yields SSE lines and writes CSVs to folder_path.
    Read metadata from ToTag DB; use oMLX for AI categorisation.
    """
    folder = Path(folder_path)
    if not folder.exists():
        yield f"data: ❌ Folder not found: {folder_path}\n\n"
        yield "data: [DONE]\n\n"
        return

    is_editorial = "_ED" in folder.name
    is_ed_str = "Yes" if is_editorial else "No"

    override_sh = CATEGORY_MAP.get(default_category, {}).get("shutterstock") if default_category else None

    yield f"data: 📑 Preparing CSVs for: {folder.name}\n\n"

    # Load metadata from DB
    db_meta = _get_db_metadata(folder_path)
    files   = _list_media_files(folder_path)

    if not files:
        yield "data: ❌ No media files found in folder.\n\n"
        yield "data: [DONE]\n\n"
        return

    total = len(files)
    metadata_list = []

    for i, fp in enumerate(files):
        yield f"data: 🤖 [{i+1}/{total}] {fp.name}\n\n"

        db_entry = db_meta.get(str(fp), {})
        title       = db_entry.get("title") or fp.stem.replace("_", " ").title()
        description = db_entry.get("description") or title
        keywords    = db_entry.get("keywords") or []

        if keywords:
            yield f"data:   📄 {len(keywords)} keywords from DB\n\n"
        else:
            yield f"data:   ⚠️ No keywords in DB — title fallback used\n\n"

        # AI or manual category
        if override_sh:
            ai_sh = [override_sh]
        else:
            ai = get_ai_categories(title, keywords, model=model)
            ai_sh = ai.get("shutterstock", ["Nature", "Miscellaneous"])

        metadata_list.append({
            "filename":    fp.name,
            "title":       title,
            "description": description,
            "keywords":    keywords,
            "ai_sh":       ai_sh,
        })

    # Determine folder type for category filtering
    folder_has_images = any(f.suffix.lower() in IMAGE_EXTENSIONS for f in files)

    # Write Shutterstock CSV
    shutter_path = folder / "shutterstock_upload.csv"
    with open(shutter_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(["Filename", "Description", "Keywords", "Categories", "Editorial"])
        for m in metadata_list:
            sh_cats = m["ai_sh"][:2]
            # Footage submissions don't accept certain image-only categories
            if not folder_has_images:
                sh_cats = _normalize_clip_categories(sh_cats)
            # Deduplicate after remapping
            seen_c = set()
            sh_cats = [c for c in sh_cats if not (c in seen_c or seen_c.add(c))]
            cats = ", ".join(sh_cats)
            desc = (m["description"] or m["title"])[:200]
            w.writerow([m["filename"], desc, ",".join(m["keywords"][:50]), cats, is_ed_str])

    yield f"data: ✅ Shutterstock CSV written ({len(metadata_list)} rows)\n\n"

    # Write Pond5 CSV for all clip folders (RF and ED), plus non-editorial image folders.
    pond5_name = "pond5_upload.csv"
    has_clips = any(f.suffix.lower() in VIDEO_EXTENSIONS for f in files)
    need_pond5 = has_clips or (not is_editorial and folder_has_images)
    if need_pond5:
        pond5_path = folder / pond5_name
        with open(pond5_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
            w.writerow(["originalfilename", "price", "title", "description", "keywords", "editorial"])
            p5_price = 130 if has_clips else 20
            p5_title_truncations = []
            p5_title_llm = []
            for m in metadata_list:
                desc = (m["description"] or m["title"])[:200]
                kw = ",".join(m["keywords"][:50])
                title, title_method = _pond5_title(m["title"], model=model)
                if title_method == "llm":
                    p5_title_llm.append((m["filename"], len(m["title"] or ""), title))
                elif title_method == "truncated":
                    p5_title_truncations.append((m["filename"], len(m["title"] or ""), title))
                w.writerow([m["filename"], p5_price, title, desc, kw, is_ed_str])
        if p5_title_llm:
            yield f"data: ✂️ Pond5 title limit: LLM-shortened {len(p5_title_llm)} title(s) to <=80 chars\n\n"
            for filename, old_len, title in p5_title_llm[:10]:
                yield f"data:   • {filename}: {old_len} → {len(title)} chars\n\n"
            if len(p5_title_llm) > 10:
                yield f"data:   … {len(p5_title_llm) - 10} more title(s) LLM-shortened\n\n"
        if p5_title_truncations:
            yield f"data: ⚠️ Pond5 title limit: fallback-truncated {len(p5_title_truncations)} title(s) to <=80 chars\n\n"
            for filename, old_len, title in p5_title_truncations[:10]:
                yield f"data:   • {filename}: {old_len} → {len(title)} chars\n\n"
            if len(p5_title_truncations) > 10:
                yield f"data:   … {len(p5_title_truncations) - 10} more title(s) truncated\n\n"
        yield f"data: ✅ Pond5 CSV written ({len(metadata_list)} rows)\n\n"

    # Clear CSV entries from upload log so re-upload sends the new CSV files
    upload_log = _load_upload_log(folder)
    changed = False
    csv_pairs = [("Shutterstock", shutter_path.name)]
    if need_pond5:
        csv_pairs.append(("Pond5", pond5_name))
    for site, csv_name in csv_pairs:
        if site in upload_log:
            before = len(upload_log[site])
            upload_log[site] = [f for f in upload_log[site] if f != csv_name]
            if len(upload_log[site]) != before:
                changed = True
    if changed:
        _save_upload_log(folder, upload_log)
        yield "data: ♻️ Upload log updated — CSV will be re-uploaded on next upload.\n\n"

    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# FTP Upload (SSE generator)
# ---------------------------------------------------------------------------

def upload_folder_gen(folder_path: str):
    """Generator that uploads all media files in folder_path via FTP, yielding SSE lines."""
    folder = Path(folder_path)
    if not folder.exists():
        yield f"data: ❌ Folder not found: {folder_path}\n\n"
        yield "data: [DONE]\n\n"
        return

    config = _load_ftp_config()
    if not config:
        yield "data: ❌ No FTP credentials found in microstock_config.json\n\n"
        yield "data: [DONE]\n\n"
        return

    files = _list_media_files(folder_path)
    if not files:
        yield "data: ❌ No media files to upload.\n\n"
        yield "data: [DONE]\n\n"
        return

    # Determine recommended sites for this folder
    has_images = any(f.suffix.lower() in IMAGE_EXTENSIONS for f in files)
    has_clips  = any(f.suffix.lower() in VIDEO_EXTENSIONS for f in files)
    is_editorial = "_ED" in folder.name

    if has_clips and not has_images:
        ftype = "clips"
    else:
        ftype = "images"

    sites = SITE_MATRIX.get(ftype, []).copy()
    if ftype == "images" and not is_editorial:
        if "Pond5" not in sites:
            sites.append("Pond5")
    if is_editorial:
        sites = [s for s in sites if s != "Adobe Stock"]
    upload_log = _load_upload_log(folder)
    yield f"data: 🚀 Uploading {len(files)} files from: {folder.name}\n\n"

    CHUNK = 512 * 1024  # 512 KB chunks for FTP progress reporting

    for site in sites:
        if site not in config:
            yield f"data: ⏭️ Skipping {site} (no credentials configured)\n\n"
            continue

        already_done = set(upload_log.get(site, []))
        pending = [fp for fp in files if fp.name not in already_done]

        cfg = config[site]

        # Identify the CSV for this site (if any)
        csv_for_site = {
            "Shutterstock": folder / "shutterstock_upload.csv",
            "Pond5":        folder / "pond5_upload.csv",
        }.get(site)
        csv_pending = (csv_for_site and csv_for_site.exists()
                       and csv_for_site.name not in already_done)

        if not pending and not csv_pending:
            yield f"data: ✅ {site} — already uploaded, nothing to do.\n\n"
            continue

        if already_done:
            yield f"data: ↩️ {site} — resuming ({len(already_done)} already uploaded, {len(pending)} remaining)\n\n"

        yield f"data: 📡 Connecting to {site} ({cfg['host']})...\n\n"

        try:
            use_sftp = cfg["host"].startswith("sftp.") or cfg.get("sftp", False)
            remote_dir = cfg.get("remote_dir") or SITE_REMOTE_DIRS.get(site)

            if use_sftp:
                import paramiko
                # t/sftp opened outside a `with` (paramiko doesn't support the
                # context-manager protocol on Transport the way we need here),
                # so close them in finally — otherwise any exception (or the
                # generator being closed early on job cancel) leaks the socket
                # and paramiko's background transport thread.
                t = None
                sftp = None
                try:
                    t = paramiko.Transport((cfg["host"], 22))
                    t.connect(username=cfg["user"], password=cfg["pass"])
                    sftp = paramiko.SFTPClient.from_transport(t)
                    if remote_dir:
                        sftp.chdir(remote_dir)
                    for fp in pending:
                        file_size = fp.stat().st_size
                        size_mb = file_size / (1024 * 1024)
                        yield f"data:   📤 {fp.name} ({size_mb:.1f} MB)\n\n"
                        sftp.put(str(fp), fp.name)
                        upload_log.setdefault(site, [])
                        if fp.name not in upload_log[site]:
                            upload_log[site].append(fp.name)
                        _save_upload_log(folder, upload_log)
                        yield f"data:   ✔ {fp.name} done\n\n"
                    if csv_for_site and csv_for_site.exists():
                        yield f"data:   📤 {csv_for_site.name} (CSV)\n\n"
                        sftp.put(str(csv_for_site), csv_for_site.name)
                        upload_log.setdefault(site, [])
                        if csv_for_site.name not in upload_log[site]:
                            upload_log[site].append(csv_for_site.name)
                        _save_upload_log(folder, upload_log)
                        yield f"data:   ✔ {csv_for_site.name} done\n\n"
                finally:
                    if sftp is not None:
                        sftp.close()
                    if t is not None:
                        t.close()
            else:
                use_tls = cfg.get("tls", site == "Shutterstock")
                ftp_cls = ftplib.FTP_TLS if use_tls else ftplib.FTP
                with ftp_cls(cfg["host"]) as ftp:
                    ftp.login(user=cfg["user"], passwd=cfg["pass"])
                    if use_tls:
                        ftp.prot_p()
                    ftp.voidcmd("TYPE I")  # force binary mode for all file transfers
                    if remote_dir:
                        ftp.cwd(remote_dir)
                    for fp in pending:
                        file_size = fp.stat().st_size
                        size_mb = file_size / (1024 * 1024)
                        yield f"data:   📤 {fp.name} ({size_mb:.1f} MB)\n\n"
                        uploaded = 0
                        last_pct = -1
                        conn = ftp.transfercmd(f"STOR {fp.name}")
                        try:
                            with open(fp, "rb") as f:
                                while True:
                                    block = f.read(CHUNK)
                                    if not block:
                                        break
                                    conn.sendall(block)
                                    uploaded += len(block)
                                    pct = int(uploaded / file_size * 100) if file_size else 100
                                    if pct >= last_pct + 10:
                                        last_pct = pct
                                        yield f"data:     {pct}% ({uploaded // (1024*1024)}/{int(size_mb)} MB)\n\n"
                        finally:
                            conn.close()
                            ftp.voidresp()
                        upload_log.setdefault(site, [])
                        if fp.name not in upload_log[site]:
                            upload_log[site].append(fp.name)
                        _save_upload_log(folder, upload_log)
                        yield f"data:   ✔ {fp.name} done\n\n"
                    if csv_for_site and csv_for_site.exists():
                        yield f"data:   📤 {csv_for_site.name} (CSV)\n\n"
                        with open(csv_for_site, "rb") as f:
                            ftp.storbinary(f"STOR {csv_for_site.name}", f)
                        upload_log.setdefault(site, [])
                        if csv_for_site.name not in upload_log[site]:
                            upload_log[site].append(csv_for_site.name)
                        _save_upload_log(folder, upload_log)
                        yield f"data:   ✔ {csv_for_site.name} done\n\n"

            yield f"data: ✅ {site} — all done ({len(pending)} files + CSV uploaded).\n\n"
        except Exception as e:
            yield f"data: ❌ {site} failed: {e}\n\n"
            yield f"data:   Progress saved — re-upload will resume from where it stopped.\n\n"

    yield "data: [DONE]\n\n"
