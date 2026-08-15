"""Metadata read/write wrapper using exiftool.

Handles both images (XMP-dc/IPTC) and videos (XMP-dc/QuickTime).
"""

import os
import subprocess
import json
from pathlib import Path
from typing import Optional

from core.binaries import find_binary


def read_metadata(file_path: str) -> dict:
    """Read metadata from a file using exiftool.

    Returns dict with title, description, keywords, capture_date.
    """
    try:
        # Run exiftool with JSON output - only get common user fields
        result = subprocess.run(
            [find_binary("exiftool"), "-json",
             "-XMP-dc:title", "-XMP-dc:description", "-XMP-dc:subject",
             "-IPTC:ObjectName", "-IPTC:Caption-Abstract",
             "-QuickTime:Title", "-QuickTime:Description", "-QuickTime:Keywords",
             "-DateTimeOriginal", "-CreateDate", "-ContentCreateDate",
             file_path],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            return {}

        data = json.loads(result.stdout)
        if not data:
            return {}

        meta = data[0]

        # When exiftool returns specific fields, it uses short names (Title, Description, Subject)
        # not the prefixed names (XMP-dc:title, QuickTime:Title, etc.)
        title = meta.get("Title")

        description = meta.get("Description")

        # Extract keywords - Subject (XMP) or Keywords (QuickTime/IPTC)
        keywords_raw = meta.get("Subject") or meta.get("Keywords")
        keywords = []
        if keywords_raw:
            if isinstance(keywords_raw, list):
                keywords = keywords_raw
            elif isinstance(keywords_raw, str):
                # exiftool may return semicolon or comma separated
                if ";" in keywords_raw:
                    keywords = [k.strip() for k in keywords_raw.split(";") if k.strip()]
                elif "," in keywords_raw:
                    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
                else:
                    keywords = [keywords_raw] if keywords_raw else []

        # Extract capture date
        capture_date = (meta.get("DateTimeOriginal") or
                        meta.get("CreateDate") or
                        meta.get("ContentCreateDate"))

        return {
            "title": title,
            "description": description,
            "keywords": keywords,
            "capture_date": capture_date,
        }

    except Exception as e:
        print(f"Error reading metadata from {file_path}: {e}")
        return {}


def write_metadata(file_path: str, title: Optional[str] = None,
                   description: Optional[str] = None,
                   keywords: Optional[list[str]] = None) -> bool:
    """Write metadata to a file using exiftool.

    Returns True on success, False on failure.
    """
    try:
        # Build exiftool arguments
        args = [find_binary("exiftool"), "-overwrite_original"]

        if title is not None:
            # Write to both XMP-dc:title and IPTC:ObjectName for compatibility
            args.append(f"-XMP-dc:title={title}")
            args.append(f"-IPTC:ObjectName={title}")

        if description is not None:
            # Write to both XMP-dc:description and IPTC:Caption-Abstract
            args.append(f"-XMP-dc:description={description}")
            args.append(f"-IPTC:Caption-Abstract={description}")

        if keywords is not None:
            # Use -sep to split a comma-joined string into proper XMP/IPTC list entries.
            # Passing each keyword as one semicolon-joined string writes a single entry;
            # -sep "," tells exiftool to treat the string as multiple values.
            args.extend(["-sep", ","])
            kw_string = ",".join(keywords) if keywords else ""
            args.append(f"-XMP-dc:subject={kw_string}")
            args.append(f"-IPTC:Keywords={kw_string}")

        args.append(file_path)

        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            # Auto-clean stale _exiftool_tmp file and retry once
            if "_exiftool_tmp" in result.stderr and "already exists" in result.stderr:
                tmp_path = file_path + "_exiftool_tmp"
                try:
                    os.remove(tmp_path)
                    print(f"Removed stale tmp file, retrying: {tmp_path}")
                    result = subprocess.run(args, capture_output=True, text=True, check=False)
                except OSError as e:
                    print(f"Could not remove stale tmp file {tmp_path}: {e}")

        if result.returncode != 0:
            print(f"exiftool FAILED (code {result.returncode}) for: {file_path}")
            print(f"  stdout: {result.stdout.strip()}")
            print(f"  stderr: {result.stderr.strip()}")

        return result.returncode == 0

    except Exception as e:
        print(f"Error writing metadata to {file_path}: {e}")
        return False


def write_metadata_batch(files: list[str], title: Optional[str] = None,
                         description: Optional[str] = None,
                         keywords: Optional[list[str]] = None) -> int:
    """Write metadata to multiple files.

    Returns count of successfully updated files.
    """
    success_count = 0

    for file_path in files:
        if write_metadata(file_path, title, description, keywords):
            success_count += 1

    return success_count


if __name__ == "__main__":
    # Test with a sample file
    import sys
    if len(sys.argv) > 1:
        test_file = sys.argv[1]
        print(f"Reading metadata from: {test_file}")
        meta = read_metadata(test_file)
        print(f"Title: {meta.get('title')}")
        print(f"Description: {meta.get('description')}")
        print(f"Keywords: {meta.get('keywords')}")
        print(f"Capture Date: {meta.get('capture_date')}")
