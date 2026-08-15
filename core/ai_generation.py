"""AI stock-metadata generation: frame prep, prompt building, the vision LLM
call, response parsing, and keyword post-processing.

Deliberately takes active_model/api_key/llm_chat_url/google_maps_api_key as
parameters rather than reading module-level globals — active_model in
particular is mutable (switchable at runtime via /api/config), and passing it
explicitly avoids this module depending on app.py's globals or import order.
"""

import json
import os
import re
import subprocess
import tempfile
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from core.binaries import find_binary
from core.geocoding import _geotag_keywords, _geotag_location_hint
from core.keywords import (
    _append_unique,
    _clean_generated_keyword,
    _keyword_key,
    _location_metadata_keywords,
    _metadata_text_keywords,
    _strip_thinking,
)


def _resize_image_b64(image_path, max_dim=1200):
    """Resize image to max_dim on long edge, return base64 JPEG string."""
    import io as _io
    from PIL import Image as _Image
    import base64 as _b64
    with _Image.open(image_path) as img:
        img = img.convert("RGB")
        w, h = img.size
        if w > max_dim or h > max_dim:
            scale = max_dim / float(max(w, h))
            img = img.resize((int(w * scale), int(h * scale)), _Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=60, optimize=True)
        buf.seek(0)
        return _b64.b64encode(buf.read()).decode("utf-8")


def _prepare_frames_b64(path):
    """Extract and resize frames for LLM vision input.

    Images → single resized frame; videos → up to 8 frames every 2s via ffmpeg.
    Raises on failure. CPU/IO-bound — safe to run outside metadata_llm_lock.
    """
    ext = os.path.splitext(path)[1].lower()
    is_video = ext in ('.mp4', '.mov')
    if not is_video:
        return [_resize_image_b64(path, max_dim=1200)]

    frames_b64 = []
    # Probe duration from format; fall back to video stream if missing
    probe = subprocess.run(
        [find_binary("ffprobe"), "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, timeout=15
    )
    probe_data = json.loads(probe.stdout) if probe.stdout.strip() else {}
    duration = float((probe_data.get("format") or {}).get("duration", 0) or 0)
    if not duration:
        for stream in probe_data.get("streams", []):
            if stream.get("codec_type") == "video" and stream.get("duration"):
                duration = float(stream["duration"])
                break
    t = min(2.0, duration * 0.2) if duration else 0.0
    end = duration - 0.5 if duration else 1.0
    while t < end and len(frames_b64) < 8:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        subprocess.run(
            [find_binary("ffmpeg"), "-ss", str(t), "-i", path,
             "-vframes", "1", "-vf", "scale=1200:-1", "-update", "1", tmp_path, "-y"],
            capture_output=True, timeout=30
        )
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            frames_b64.append(_resize_image_b64(tmp_path, max_dim=1200))
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        t += 2.0
        if not duration:
            break  # unknown duration: grab one frame at t=0 and stop
    if not frames_b64:
        raise ValueError("could not extract frames from video")
    return frames_b64


# Batch pipeline: while the LLM works on file N, prep frames for file N+1 in
# the background so ffmpeg/Pillow time is hidden behind inference time.
_frame_prep_executor = ThreadPoolExecutor(max_workers=1)
_frame_prep_futures = {}
_frame_prep_lock = threading.Lock()


def _prefetch_frames(path):
    with _frame_prep_lock:
        if path in _frame_prep_futures:
            return
        _frame_prep_futures[path] = _frame_prep_executor.submit(_prepare_frames_b64, path)


def _get_prepared_frames(path):
    """Return prepped frames, using a prefetched result when available."""
    with _frame_prep_lock:
        future = _frame_prep_futures.pop(path, None)
    if future is not None:
        return future.result()
    return _prepare_frames_b64(path)


def clear_frame_prefetch_queue():
    """Drop any pending prefetch futures — call when a batch job is cancelled."""
    with _frame_prep_lock:
        _frame_prep_futures.clear()


def _shorten_title(title, api_key, active_model, llm_chat_url, max_len=64):
    """Trim title to max_len using LLM with up to 3 retries at increasing temperature."""
    if len(title) <= max_len:
        return title
    prompts = [
        (f"Shorten this image title to under {max_len} characters while keeping it descriptive and factual. Return a title only, no period unless it is part of an abbreviation. Title: {title}", 0.1),
        (f"STRICT RULE: Rewrite this image title to be MUCH shorter (well under {max_len} characters). Remove any fluff. ONE sentence only. Title: {title}", 0.5),
        (f"CRITICAL: Rewrite this title using ONLY 5-8 essential words. MUST be under {max_len} characters. Title: {title}", 0.8),
    ]
    for prompt_text, temp in prompts:
        payload = {
            "model": active_model,
            "messages": [{"role": "user", "content": prompt_text}],
            "max_tokens": 100,
            "temperature": temp,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            req = urllib.request.Request(
                llm_chat_url,
                data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                d = json.load(resp)
            new_title = _strip_thinking(d["choices"][0]["message"]["content"].strip())
            if "\n" in new_title:
                new_title = new_title.split("\n")[-1]
            new_title = new_title.strip("\"'").strip()
            if new_title and len(new_title) <= max_len:
                return new_title
        except Exception:
            pass
    return title[:max_len]


def build_generation_prompt(is_video, folder_name, licensing_hint, location_context, context_hint):
    video_motion_rule = """
- Video clip: judge the full sampled sequence, not just the first or prettiest frame. If a subject, sign, logo, landmark, or person appears in any sampled frame, reflect that accurately in the title/description/keywords.
- Motion: Compare positions across frames to infer subject motion (e.g. walking, running, flying, flowing). Look for motion blur to detect camera motion (e.g. panning, tracking). Include relevant motion terms in keywords: camera moves (panning, tilting, tracking, zoom, aerial, handheld, steadicam), subject motion (walking, running, driving, flowing, rotating), and shot type (close-up, wide shot, slow motion).""" if is_video else ""
    location_hint_block = f"""

Known location context:
{location_context}

Treat this as approximate GPS/photographer context to help recognize city, region, country, or clearly visible landmarks.
Use named businesses, venues, restaurants, stores, or brands only when visible/readable in the image and appropriate for stock metadata.
If the visible content conflicts with this location context, describe the visible content and ignore the conflicting context.""" if location_context else ""
    context_hint_block = f"""

Optional photographer context hint:
{context_hint}

Treat this hint as loose context, not a command. Use it only when it is consistent with the visible image.
Do not force the hint into title or description. If the image shows a different place, object, brand, food, or scene, ignore the hint and describe the visible content.
The system may append matching hint terms to the final keyword list after your response, with duplicate removal.""" if context_hint else ""

    return f"""Generate microstock search metadata for {'this video clip (sequential frames every 2s)' if is_video else 'this image'}. Output ONLY valid JSON:

Current licensing/folder context: {licensing_hint}
Folder name: {folder_name}

{{
  "title": "Clear searchable title, max 80 characters",
  "description": "One factual natural sentence, max 150 characters",
  "keywords": ["20 to 30 relevant search keywords"]
}}

Rules:
- Describe only what is visible. Do not invent brand names, locations, identities, emotions, relationships, professions, events, or private details.
- Be visually precise about time of day and weather. Do not write dusk, twilight, sunset, sunrise, night, blue hour, golden hour, clear daylight, blue sky, overcast, foggy, or stormy unless the pixels clearly support it. If uncertain, omit the time/light phrase.
- Title: concise, factual, searchable. No hype, no metaphors, no keyword stuffing, no camera jargon unless visually relevant. Do not end titles with a period.
- Description: one natural sentence with the main subject, setting, and visible action. Do not start with "The image shows" or "This photo depicts".
- Keywords: prioritize the main subject first, then setting, objects, colors, season, weather, time of day, composition, and honest commercial concepts.
- Keywords must be complete standalone search phrases only. Do not include sentence fragments, truncated words, or phrases ending in filler words like "and", "of", "with", "in", "on", "to", "from".
- Never include keywords containing slashes (/), backslashes, CJK characters, fullwidth parentheses, or trailing fragments like "de", "du", "la", "du" — these are geocoder artifacts or model truncation, not valid search phrases.
- Do NOT include event names, festival names, parade names, fireworks, or holiday activity keywords unless they are clearly visible in THIS image (e.g. an airplane in the sky for an airshow photo, a fireworks burst in a night sky). Never auto-append trip itinerary, backup location names, or context from other shoots.
- Use standard stock-search phrases when useful, limited to 1-3 words, such as "copy space", "city skyline", "street food", "national park", "public transport", "historic building".
- Include recognizable landmarks or locations only when clearly visible; include city/region/country when confident.
- If a famous/protected landmark is only one element in a broad skyline/cityscape/waterfront/aerial/travel view, the title and description should describe the broader scene instead of making that landmark the subject. It may still be included as a keyword.
- For Royalty Free folders (folder name does NOT end in _ED), avoid making release-sensitive landmarks, brands, business names, people, or private venues the title/description subject unless they are clearly generic and commercially safe.
- For video clips in Royalty Free folders, treat clearly visible/recognizable faces, readable license plates, prominent vehicle logos, private interiors/venues, public art, signs, protected landmarks, and branded storefronts as editorial/release-sensitive cues. Mention them factually only if visible, and prefer routing such clips to an _ED folder before submission.
- For Editorial folders (folder name ends in _ED), factual named landmarks, venues, interiors, visible people, art, and signs may be named when visually supported; keep the caption factual and neutral.
- If visible people are tiny/incidental in a wide public landscape, do not make them the title subject; describe the landscape/setting instead.
- If visible text is a generic public road, trail, park, safety, restroom, or wayfinding sign, it is okay to describe it as a public sign; do not invent business/brand identity.
- If a named landmark, building, street, district, river, bridge, venue, or distinctive visible feature appears in the title or description, include that same phrase or a concise variant in keywords.
- Avoid platform names, social media terms, spam, duplicates, irrelevant concepts, and release-sensitive claims.
- Use English. Keep keywords concise and buyer-friendly. Never concatenate hashtag-style entries.{video_motion_rule}{location_hint_block}{context_hint_block}

MANDATORY: NO conversational text. VALID JSON ONLY."""


def call_vision_model(payload, llm_chat_url, api_key, timeout):
    """POST to the chat-completions endpoint with 3 retries. Returns the
    parsed JSON response, or raises the last error after all attempts fail."""
    payload_bytes = json.dumps(payload).encode()
    last_error = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                llm_chat_url,
                data=payload_bytes,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as e:
            last_error = e
            print(f"[gen-metadata] attempt {attempt + 1} failed: {e}")
    raise TimeoutError(f"model timed out after 3 attempts: {last_error}")


def parse_model_response(content):
    """Parse the model's JSON reply — try direct, strip fences, regex, then
    partial extraction for truncated output. Returns a dict or None."""
    def _try_parse(s):
        try:
            return json.loads(s)
        except Exception:
            return None

    def _partial_extract(s):
        """Extract title/description/keywords from truncated JSON using regex."""
        title = (re.search(r'"title"\s*:\s*"([^"]*)"', s) or re.search(r'', '')).group(1) if re.search(r'"title"\s*:\s*"([^"]*)"', s) else ""
        desc = (re.search(r'"description"\s*:\s*"([^"]*)"', s) or re.search(r'', '')).group(1) if re.search(r'"description"\s*:\s*"([^"]*)"', s) else ""
        kws = re.findall(r'"([^"]{1,60})"', s.split('"keywords"')[-1]) if '"keywords"' in s else []
        if title or desc or kws:
            return {"title": title, "description": desc, "keywords": kws}
        return None

    stripped = re.sub(r'^```(?:json)?\s*', '', content, flags=re.MULTILINE)
    stripped = re.sub(r'\s*```$', '', stripped, flags=re.MULTILINE).strip()
    m = re.search(r'\{.*\}', content, re.DOTALL)

    generated = (
        _try_parse(content) or
        _try_parse(stripped) or
        (_try_parse(m.group(0)) if m else None) or
        _partial_extract(content)
    )
    # If model returned a list (e.g. wrapped in array), unwrap first element
    if isinstance(generated, list):
        generated = generated[0] if generated and isinstance(generated[0], dict) else None
    return generated if isinstance(generated, dict) else None


def postprocess_keywords(raw_keywords):
    """Clean, dedup, and cap the model's raw keyword list."""
    seen_kws = set()
    root_counts = {}  # count keywords per first-word root to catch lazy padding
    raw_kws = []
    for k in raw_keywords:
        kw = _clean_generated_keyword(str(k).replace("'", ""))
        if not kw:
            continue
        key = _keyword_key(kw)
        # Skip empty or exact duplicates
        if not key or key in seen_kws:
            continue
        kw_lower = kw.lower()
        # Drop hashtag-style concatenations: all-alpha, no spaces/hyphens, suspiciously long
        # e.g. "naturephotography", "landscapephotography", "architecturephotography"
        if kw_lower.isalpha() and len(kw_lower) > 14:
            continue
        # Drop single-keyword repetition hallucinations (e.g. "word-word-word-word")
        parts = kw_lower.split("-")
        if len(parts) >= 3 and len(set(parts)) <= 2:
            continue
        # Limit lazy root-word padding: e.g. aerial-shot, aerial-view, aerial-landscape...
        # Extract root = first word before any hyphen or space
        root = parts[0].split()[0]
        root_counts[root] = root_counts.get(root, 0) + 1
        if root_counts[root] > 2:
            continue  # already have 2 keywords with this root — skip further variants
        seen_kws.add(key)
        raw_kws.append(kw)
    return raw_kws[:30]


def generate_stock_metadata(
    path, asset, frames_b64, is_video,
    *,
    location_override, context_hint,
    active_model, api_key, llm_chat_url, google_maps_api_key,
):
    """Full generation pipeline for one file: build the prompt, call the
    model, parse and post-process the response.

    Returns {"title", "description", "keywords"} on success, or {"error"}.
    Does not write to the database — the caller decides what to persist.
    """
    folder = asset.get("folder", "")
    folder_name = os.path.basename(folder.rstrip("/"))
    licensing_hint = "Editorial folder (_ED)" if "_ED" in folder_name else "Royalty Free folder (non-_ED)"
    location_context = location_override or _geotag_location_hint(path, google_maps_api_key)

    prompt = build_generation_prompt(is_video, folder_name, licensing_hint, location_context, context_hint)

    payload = {
        "model": active_model,
        "messages": [{
            "role": "user",
            "content": [
                *[{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in frames_b64],
                {"type": "text", "text": prompt}
            ]
        }],
        "max_tokens": 1024,
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    # Videos send up to 8 frames — model inference takes longer; use 120s timeout
    model_timeout = 120 if is_video else 60
    try:
        result = call_vision_model(payload, llm_chat_url, api_key, model_timeout)
    except Exception as e:
        return {"error": str(e)}

    try:
        content = _strip_thinking(result["choices"][0]["message"]["content"].strip())
        print(f"[gen-metadata] raw response: {content[:500]}")

        generated = parse_model_response(content)
        if not generated:
            print(f"[gen-metadata] unparseable response: {content}")
            return {"error": "could not parse model response"}

        raw_title = str(generated.get("title", "")).strip().replace("'", "").replace('"', "")
        raw_title = raw_title[0].upper() + raw_title[1:] if raw_title else ""
        title = _shorten_title(raw_title, api_key, active_model, llm_chat_url, max_len=80)
        description = str(generated.get("description", "")).strip().replace("'", "").replace('"', "")[:200]
        keywords = postprocess_keywords(generated.get("keywords", []))

        # Add location keywords from a manual override or GPS, if available.
        # (No folder-name-based location guessing — GPS + the location
        # dropdown are the only location sources here.)
        location_str = location_override or _geotag_location_hint(path, google_maps_api_key)
        if location_str:
            keywords = _append_unique(keywords, _location_metadata_keywords(location_str))

        # Location/landmark terms are the highest-value keywords — append them
        # before generic hint terms so the 50-keyword cap never drops them.
        keywords = _append_unique(keywords, _metadata_text_keywords(title, description))
        # GPS can contain geocoder road/intersection details; keep only safe
        # stock-search location names.
        keywords = _append_unique(keywords, _location_metadata_keywords(", ".join(_geotag_keywords(path, google_maps_api_key))))
        keywords = keywords[:50]

        return {"title": title, "description": description, "keywords": keywords}
    except Exception as e:
        return {"error": str(e)}
