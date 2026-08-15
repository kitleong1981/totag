"""Text/keyword utilities shared by AI metadata generation and geocoding.

Pure functions only — no Flask, no network, no shared mutable state — so
they're safe to import from anywhere and easy to unit test in isolation.
"""

import re


def _truncate_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].strip()
    return cut or text[:limit].strip()


def _strip_thinking(text: str) -> str:
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def _keyword_key(value: str) -> str:
    """Normalize a keyword for duplicate detection (case/plural-insensitive)."""
    text = re.sub(r"[^a-z0-9\s-]", " ", str(value).lower())
    words = []
    for word in re.sub(r"[-_]+", " ", text).split():
        if len(word) > 3 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("es") and not word.endswith(("ses", "xes")):
            word = word[:-2]
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.append(word)
    return " ".join(words)


def _append_unique(items: list[str], additions: list[str]) -> list[str]:
    seen = {_keyword_key(x) for x in items if str(x).strip()}
    for item in additions:
        item = str(item).strip()
        key = _keyword_key(item)
        if item and key not in seen:
            items.append(item)
            seen.add(key)
    return items


def _append_term(terms: list[str], value: str, seen: set[str]):
    value = re.sub(r"\s+", " ", str(value or "")).strip(" .,:;")
    key = _keyword_key(value)
    if value and key and key not in seen:
        terms.append(value)
        seen.add(key)


def _context_hint_keywords(context_hint: str) -> list[str]:
    parts = re.split(r"[,;\n]+", context_hint or "")
    return [p.strip(" .:-") for p in parts if p.strip(" .:-")]


def _merge_hint_texts(*hints: str) -> str:
    terms, seen = [], set()
    for hint in hints:
        for part in _context_hint_keywords(hint):
            _append_term(terms, part, seen)
    return ", ".join(terms)[:1000]


def _clean_generated_keyword(value: str):
    """Reject sentence fragments/truncated tokens from generated keyword arrays.

    Video metadata generation is more likely to return partially parsed JSON or
    bleed description text into the keyword list (e.g. "Washington. A",
    "Mountain la", "Columbia River and"). Keep only clean stock-search
    keyword phrases.
    """
    kw = re.sub(r"\s+", " ", str(value or "").strip().strip(",;:|"))
    if not kw:
        return None
    low = kw.lower()

    # Expand common clipped video terms observed in generated clips before
    # applying trailing-fragment rejection.
    clipped = {
        "mountain la": "Mountain landscape",
        "alpine la": "Alpine lake",
        "oregon la": "Oregon landscape",
        "ferry la": "Ferry landing",
        "time la": "Time lapse",
        "madison st": "Madison Street",
        "seneca st": "Seneca Street",
    }
    if low in clipped:
        return clipped[low]

    # Keywords should be short phrases, not sentence/title remnants.
    if any(ch in kw for ch in ".!?\n\r"):
        return None
    # Reject keywords containing slashes, backslashes, or CJK characters
    # (these come from geocoder artifacts or model formatting leaks).
    if re.search(r"[/\\]", kw) or re.search(r"[一-鿿㐀-䶿]", kw):
        return None
    # Reject keywords containing fullwidth parentheses (Chinese punctuation)
    if re.search(r"[（）]", kw):
        return None
    if re.search(r"^(aerial view|wide shot|close[- ]up|pov|point of view|tracking shot|panning shot|tilting shot|low angle|night view|daytime view|exterior of|interior of|view of|shot of|walking along|walking through|driving on|driving through)\b", low):
        return None
    if re.search(r"\b(and|or|of|with|in|on|at|for|to|from|by)$", low):
        return None
    if re.search(r"\b[a-z]{1,2}$", low) and low not in {"ai", "us", "usa", "uk", "uw", "mt", "tv", "rv", "dc", "wa"}:
        return None
    if len(low) >= 5 and not re.search(r"[aeiou]", low) and low not in {"lgbtq", "lgbtq+", "rhythm"}:
        return None

    # Reject sentence fragments such as "Rows of" before they reach the
    # metadata. A useful keyword cannot end with a connector/article.
    if re.search(r"\b(?:and|at|by|for|from|in|of|on|or|the|to|with)$", low):
        return None

    # Avoid overlong phrases; usually a leaked sentence clause.
    if len(kw) > 42 or len(kw.split()) > 5:
        return None
    return kw[0].upper() + kw[1:]


def _location_metadata_keywords(location_str: str) -> list[str]:
    """Keep useful location names, but reject geocoder street/intersection noise.

    Reverse/geocoding and folder inference can return entries such as
    ``Addison @ Addison Cir - N - FS``.  These are useful for navigation but
    poor stock-search keywords and should never be appended automatically.
    """
    parts = [p.strip(" .:-") for p in re.split(r"[,;\n]+", location_str or "") if p.strip(" .:-")]
    result = []
    for part in parts:
        low = part.lower()
        if "@" in part or re.search(r"\b(?:intersection|crossing|roundabout)\b", low):
            continue
        # County/Airport are reverse-geocoder containment details, not useful
        # automatic stock keywords unless the model explicitly names them in
        # the visible caption.
        if re.search(r"\b(?:county|airport)\b", low):
            continue
        # Drop geocoder marker/address labels and model-truncated fragments.
        if re.search(r"\s-\s", part) or re.search(r"\b(?:du|de|la)\s*$", low):
            continue
        if re.search(r"\b(?:FS|NS|ES|WS|MB|NB|SB|EB|WB|FWY|HWY|FM|SR)\b", part, re.I):
            continue
        if re.search(r"\s+at\s+", low):
            continue
        if part and part not in result:
            result.append(part)
    return result


def _metadata_text_keywords(title: str, description: str) -> list[str]:
    """Pull named places, landmarks, and distinctive proper phrases from generated text."""
    stop_starts = {
        "a", "an", "and", "at", "by", "for", "from", "in", "near", "of", "on", "the", "to", "with",
        "close", "colorful", "dramatic", "historic", "modern", "old", "scenic", "sunlit", "urban",
        "aerial", "daytime", "driving", "exterior", "interior", "night", "panning", "point", "pov",
        "shot", "tilting", "tracking", "view", "walking", "wide",
    }
    weak_terms = {
        "architecture", "building", "city", "downtown", "landmark", "landscape", "river",
        "skyline", "street", "travel", "view",
    }
    landmark_suffixes = {
        "arch", "arena", "avenue", "bay", "beach", "bridge", "building", "canyon",
        "capitol", "castle", "cathedral", "center", "church", "creek", "district",
        "falls", "garden", "glacier", "hall", "hotel", "house", "inn", "island",
        "lake", "lodge", "mansion", "market", "meadows", "memorial", "monument",
        "mount", "mountain", "museum", "needle", "overlook", "palace", "park",
        "peak", "pier", "point", "pyramid", "ridge", "river", "square", "station",
        "street", "summit", "temple", "theater", "tower", "trail", "trailhead",
        "valley", "viewpoint", "wharf",
    }
    text = f"{title or ''}. {description or ''}"
    # Match capitalized/proper-noun phrases, including connectors used in real names.
    pattern = r"\b[A-Z][A-Za-z0-9&.'-]*(?:\s+(?:of|de|del|du|la|le|the|and|&|[A-Z][A-Za-z0-9&.'-]*)){0,6}"
    candidates = []
    matches = [m.group(0) for m in re.finditer(pattern, text)]
    suffix_pattern = "|".join(sorted(landmark_suffixes | {"riverfront", "sign", "signage"}))
    matches.extend(m.group(0) for m in re.finditer(rf"\b[A-Z][A-Za-z0-9&.'-]*(?:\s+(?:{suffix_pattern})){{1,3}}\b", text))
    for raw_match in matches:
        phrase = re.sub(r"\s+", " ", raw_match).strip(" .,:;()[]")
        words = phrase.split()
        if not words:
            continue
        # The capitalized phrase matcher can see the opening of a sentence
        # (for example, "Rows of wooden barrels") as "Rows of". Do not turn
        # that incomplete clause into a keyword.
        if words[-1].lower() in {"and", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with"}:
            continue
        if len(words) == 1 and words[0].lower() not in landmark_suffixes:
            continue
        if words[0].lower() in stop_starts:
            phrase = " ".join(words[1:]).strip()
            words = phrase.split()
        if not phrase or len(phrase) > 60 or ". " in phrase:
            continue
        key = _keyword_key(phrase)
        if not key or key in weak_terms:
            continue
        # A real name needs at least one non-generic word ("Paradise Inn" yes,
        # "Inn lodge" no).
        if all(w.lower() in landmark_suffixes for w in words):
            continue
        if len(words) >= 2 or words[-1].lower() in landmark_suffixes:
            candidates.append(phrase)
    # Named places (ending in a landmark suffix) are the whole point of this
    # backfill — keep them ahead of generic capitalized phrases before capping.
    candidates.sort(key=lambda p: 0 if p.split()[-1].lower() in landmark_suffixes else 1)
    return candidates[:12]
