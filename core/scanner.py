"""Folder scanner for ToTag Metadata Manager.

Walks configured folder roots and populates the database with assets.
"""

import os
import json
from pathlib import Path
from datetime import datetime
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import FOLDER_ROOTS, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from models.db import upsert_assets, get_all_assets, delete_missing_assets
from core.metadata import read_metadata


def scan_file_fast(file_path: str) -> dict | None:
    """Fast scan - just file info, no metadata reading."""
    path = Path(file_path)
    ext = path.suffix.lower()

    # Determine media type
    if ext in IMAGE_EXTENSIONS:
        media_type = "image"
    elif ext in VIDEO_EXTENSIONS:
        media_type = "video"
    else:
        return None

    # Get file stats only
    try:
        stat = path.stat()
        file_mtime = stat.st_mtime
    except OSError:
        return None

    return {
        "path": str(file_path),
        "filename": path.name,
        "folder": str(path.parent),
        "media_type": media_type,
        "thumb_path": None,
        "capture_date": None,  # Will be read on-demand
        "file_mtime": file_mtime,
        "title": None,  # Will be read on-demand
        "description": None,  # Will be read on-demand
        "keywords": None,  # Will be read on-demand
        "is_dirty": 0,
    }


def scan_file(file_path: str) -> dict | None:
    """Scan a single file and return asset data with full metadata."""
    path = Path(file_path)
    ext = path.suffix.lower()

    # Determine media type
    if ext in IMAGE_EXTENSIONS:
        media_type = "image"
    elif ext in VIDEO_EXTENSIONS:
        media_type = "video"
    else:
        return None

    # Get file stats
    try:
        stat = path.stat()
        file_mtime = stat.st_mtime
    except OSError:
        return None

    # Read metadata from file
    try:
        meta = read_metadata(file_path)
    except Exception:
        meta = {}

    # Convert keywords list to JSON string for database storage
    keywords = meta.get("keywords")
    keywords_json = json.dumps(keywords) if keywords else None

    return {
        "path": str(file_path),
        "filename": path.name,
        "folder": str(path.parent),
        "media_type": media_type,
        "thumb_path": None,  # Will be populated by thumbs module
        "capture_date": meta.get("capture_date"),
        "file_mtime": file_mtime,
        "title": meta.get("title"),
        "description": meta.get("description"),
        "keywords": keywords_json,
        "is_dirty": 0,
    }


def scan_root(root_path: str, fast: bool = False, progress_callback=None) -> list[dict]:
    """Scan a single root folder recursively.

    Args:
        root_path: Path to scan
        fast: If True, skip metadata reading (just file listing)
        progress_callback: Optional callback(current, total) for progress updates
    """
    assets = []
    root = Path(root_path)
    scan_func = scan_file_fast if fast else scan_file

    if not root.exists():
        print(f"Warning: Root path does not exist: {root_path}")
        return assets

    # Single pass: collect all files first, then process
    files_to_scan = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden directories
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for filename in filenames:
            # Skip hidden files
            if filename.startswith("."):
                continue

            ext = Path(filename).suffix.lower()
            if ext not in IMAGE_EXTENSIONS and ext not in VIDEO_EXTENSIONS:
                continue

            file_path = os.path.join(dirpath, filename)
            files_to_scan.append(file_path)

    # Sort files for consistent ordering
    files_to_scan.sort()

    total_files = len(files_to_scan)

    # Process files with progress callback
    for current, file_path in enumerate(files_to_scan, 1):
        asset = scan_func(file_path)
        if asset:
            assets.append(asset)
            if progress_callback:
                progress_callback(current, total_files)

    return assets


def scan_root_fast(root_path: str) -> list[dict]:
    """Fast scan - just file listing, no metadata."""
    # Use same sorting as full scan for consistent ordering
    return scan_root(root_path, fast=True)


def scan_all_roots() -> list[dict]:
    """Scan all configured root folders."""
    all_assets = []

    for root in FOLDER_ROOTS:
        print(f"Scanning: {root}")
        assets = scan_root(root)
        print(f"  Found {len(assets)} files")
        all_assets.extend(assets)

    return all_assets


def refresh_database():
    """Scan all roots and update the database."""
    print("Starting database refresh...")

    # Scan all roots
    assets = scan_all_roots()

    # Upsert into database
    upsert_assets(assets)

    # Clean up missing files
    existing_paths = {a["path"] for a in assets}
    deleted_count = delete_missing_assets(existing_paths)

    # Clean up orphaned thumbnails
    cleanup_orphaned_thumbnails(assets)

    print(f"Database refresh complete. Total assets: {len(assets)}")
    return len(assets)


def cleanup_orphaned_thumbnails(assets: list[dict]):
    """Remove thumbnail cache files for files that no longer exist.

    Args:
        assets: List of asset dicts from the database
    """
    import hashlib
    import glob

    print("Cleaning up orphaned thumbnails...")

    if not os.path.exists(THUMB_CACHE_DIR):
        print("  Thumbnail cache directory does not exist, skipping cleanup")
        return

    # Build set of valid cache keys from current assets
    valid_cache_keys = set()
    for asset in assets:
        cache_key = hashlib.sha1(f"{asset['path']}:{asset['file_mtime']}".encode()).hexdigest()
        valid_cache_keys.add(cache_key)

    # Get all cached thumbnail files
    thumb_files = glob.glob(os.path.join(THUMB_CACHE_DIR, "*.jpg"))
    removed_count = 0

    for thumb_path in thumb_files:
        thumb_filename = os.path.basename(thumb_path)
        cache_key = thumb_filename[:-4]  # Remove .jpg extension

        if cache_key not in valid_cache_keys:
            try:
                os.remove(thumb_path)
                removed_count += 1
                if removed_count % 100 == 0:
                    print(f"  Removed {removed_count} orphaned thumbnails...")
            except OSError as e:
                print(f"  Error removing {thumb_path}: {e}")

    print(f"  Removed {removed_count} orphaned thumbnails")


if __name__ == "__main__":
    refresh_database()
