"""Configuration for ToTag Metadata Manager.

All machine-specific paths come from environment variables (see .env.example).
Nothing personal is hardcoded here — this file is safe to publish as-is.
"""

import os

# Base directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _split_paths(value: str) -> list[str]:
    """Split an env var of paths separated by os.pathsep (':' on macOS/Linux)."""
    return [p.strip() for p in (value or "").split(os.pathsep) if p.strip()]


# Folders to scan for images/videos. This is only the *initial* seed, read
# once at startup from TOTAG_FOLDER_ROOTS (paths separated by ':'), e.g.:
#   export TOTAG_FOLDER_ROOTS="/Users/you/Photos/Stock:/Volumes/Backup/Clips"
# After startup, the in-app folder settings (gear icon next to the folder
# list) persist changes to folders.json and update this list in place — see
# set_folder_roots() below. Every module that does `from config import
# FOLDER_ROOTS` shares this same list object, so mutating it in place (never
# rebinding the name) is what makes those live updates visible everywhere
# without re-importing.
FOLDER_ROOTS = _split_paths(os.environ.get("TOTAG_FOLDER_ROOTS", ""))


def set_folder_roots(paths: list[str]) -> None:
    """Replace the contents of FOLDER_ROOTS in place (see note above)."""
    FOLDER_ROOTS[:] = [p.strip() for p in paths if p and p.strip()]

# File extensions to scan
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".cr2", ".nef", ".dng"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}
ALL_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# Thumbnail cache settings
THUMB_CACHE_DIR = os.path.join(BASE_DIR, "thumbs")
THUMB_SIZE = 300  # Max dimension

# Database path
DATABASE_PATH = os.path.join(BASE_DIR, "totag.db")

# Ensure cache directory exists
os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
