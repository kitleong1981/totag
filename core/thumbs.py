"""Thumbnail generator for ToTag Metadata Manager.

Generates and caches 300px thumbnails for images and videos.
"""

import os
import sys
import json
import hashlib
import subprocess
from pathlib import Path
from PIL import Image

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import THUMB_CACHE_DIR, THUMB_SIZE, IMAGE_EXTENSIONS
from core.binaries import find_binary


def get_cache_key(file_path: str, file_mtime: float) -> str:
    """Generate a cache key based on path and modification time."""
    key_string = f"{file_path}:{file_mtime}"
    return hashlib.sha1(key_string.encode()).hexdigest()


def get_thumb_path(file_path: str, file_mtime: float) -> str:
    """Get the cached thumbnail path for a file."""
    cache_key = get_cache_key(file_path, file_mtime)
    return os.path.join(THUMB_CACHE_DIR, f"{cache_key}.jpg")


def thumbnail_exists(file_path: str, file_mtime: float) -> bool:
    """Check if a valid thumbnail exists in cache."""
    thumb_path = get_thumb_path(file_path, file_mtime)
    return os.path.exists(thumb_path)


def generate_image_thumb(file_path: str, output_path: str) -> bool:
    """Generate a thumbnail for an image file."""
    try:
        with Image.open(file_path) as img:
            # Handle EXIF orientation
            try:
                from PIL import ImageOps
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass

            # Convert to RGB if necessary (for PNG with alpha, etc.)
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")

            # Resize maintaining aspect ratio
            img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)

            # Save as JPEG
            img.save(output_path, "JPEG", quality=85)

        return True
    except Exception as e:
        print(f"Error generating image thumbnail for {file_path}: {e}")
        return False


def generate_video_thumb(file_path: str, output_path: str) -> bool:
    """Generate a thumbnail for a video file using ffmpeg subprocess.

    Tries to capture frame at 10% duration, falls back to first frame.
    """
    import subprocess

    try:
        # First, try to get video duration and capture at 10%
        probe_result = subprocess.run(
            [find_binary("ffprobe"), "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", file_path],
            capture_output=True, text=True, timeout=30
        )
        probe = json.loads(probe_result.stdout)

        format_info = probe.get("format", {})
        duration = float(format_info.get("duration", 0))

        # Calculate capture time (10% into video, but at least 1 second)
        capture_time = max(1.0, duration * 0.1)

        # Extract frame at capture time
        result = subprocess.run(
            [find_binary("ffmpeg"), "-ss", str(capture_time), "-i", file_path,
             "-vframes", "1", "-vf", f"scale={THUMB_SIZE}:-1",
             "-update", "1", output_path, "-y"],
            capture_output=True, timeout=30
        )

        if result.returncode == 0 and os.path.exists(output_path):
            return True
        raise Exception("ffmpeg failed")

    except Exception as e:
        # Fallback: try first frame
        print(f"Video thumb at 10% failed for {file_path}, trying first frame: {e}")
        try:
            result = subprocess.run(
                [find_binary("ffmpeg"), "-ss", "0", "-i", file_path,
                 "-vframes", "1", "-vf", f"scale={THUMB_SIZE}:-1",
                 "-update", "1", output_path, "-y"],
                capture_output=True, timeout=30
            )
            if result.returncode == 0 and os.path.exists(output_path):
                return True
            raise Exception("ffmpeg failed")
        except Exception as e2:
            print(f"Error generating video thumbnail for {file_path}: {e2}")
            return False


def generate_thumbnail(file_path: str, file_mtime: float) -> str | None:
    """Generate a thumbnail for a file.

    Returns the thumbnail path on success, None on failure.
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    # Check if it's a supported media type
    if ext not in IMAGE_EXTENSIONS and ext not in {".mp4", ".mov"}:
        return None

    # Get output path
    output_path = get_thumb_path(file_path, file_mtime)

    # Check if thumbnail already exists
    if os.path.exists(output_path):
        return output_path

    # Generate thumbnail
    if ext in IMAGE_EXTENSIONS:
        success = generate_image_thumb(file_path, output_path)
    else:
        success = generate_video_thumb(file_path, output_path)

    return output_path if success else None


def get_or_generate_thumbnail(file_path: str, file_mtime: float) -> str | None:
    """Get existing thumbnail or generate a new one."""
    thumb_path = get_thumb_path(file_path, file_mtime)

    if os.path.exists(thumb_path):
        return thumb_path

    return generate_thumbnail(file_path, file_mtime)


if __name__ == "__main__":
    # Test with a sample file
    import sys
    if len(sys.argv) > 1:
        test_file = sys.argv[1]
        import time
        mtime = time.time()
        print(f"Generating thumbnail for: {test_file}")
        thumb = generate_thumbnail(test_file, mtime)
        if thumb:
            print(f"Thumbnail created: {thumb}")
        else:
            print("Failed to generate thumbnail")
