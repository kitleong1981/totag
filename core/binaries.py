"""Locate the exiftool/ffmpeg/ffprobe CLI binaries this app shells out to.

Checks PATH first (via shutil.which), then falls back to common install
locations. The fallback matters because this app is often launched from a
LaunchAgent/LaunchDaemon or similar, which on macOS gets a minimal PATH that
excludes Homebrew — a bare shutil.which() alone would fail there even though
the binary is very much installed. See TECHNICAL_SUMMARY.md's 2026-04-03
entries for the history of that exact bug.

Still macOS/Linux-oriented (Homebrew's two install prefixes, plus common
Linux paths); a Windows build would need .exe-aware lookups added here.
Results are cached per binary name since the install location won't change
during a single run.
"""

import shutil

_FALLBACK_DIRS = (
    "/opt/homebrew/bin",   # Homebrew on Apple Silicon macOS
    "/usr/local/bin",      # Homebrew on Intel macOS; common on Linux too
    "/usr/bin",            # typical Linux package-manager install location
)

_cache: dict[str, str] = {}


def find_binary(name: str) -> str:
    """Return a usable path/command for `name`, preferring PATH resolution.

    Always returns *something* — falls back to the bare name if nothing is
    found, so the eventual subprocess call fails with a clear
    FileNotFoundError instead of this function raising one early.
    """
    if name in _cache:
        return _cache[name]

    import os
    found = shutil.which(name)
    if not found:
        for d in _FALLBACK_DIRS:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found = candidate
                break
    found = found or name
    _cache[name] = found
    return found
