"""GPS / reverse-geocoding for AI metadata generation.

Three independent location sources, from most to least authoritative for a
single photo:

1. Google reverse geocoding (locality/state/country + safe landmark types)
   plus Places nearby-search for named spots within 250m of the shot.
2. OSM Overpass ``is_in()`` polygon containment — the only reliable source
   for "this photo is inside Mount Rainier National Park" (Google's reverse
   geocode does not return enclosing parks, and polygon containment is
   immune to GPS drift that would throw off a plain distance check).
3. Raw GPS coordinates read from the file via exiftool.

Requires GOOGLE_MAPS_API_KEY for source #1; source #2 (OSM) works without any
API key. Both degrade gracefully (return empty) when unavailable.
"""

import json
import os
import subprocess

from core.binaries import find_binary
from core.keywords import _append_unique

GEOCODE_CACHE: dict = {}
GEOCODE_CONTEXT_CACHE: dict = {}
OSM_CONTAIN_CACHE: dict = {}


def _distance_km(lat1, lon1, lat2, lon2):
    import math
    r = 6371.0
    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dp = math.radians(float(lat2) - float(lat1))
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _read_gps_coordinates(path: str):
    """Read decimal GPS latitude/longitude with exiftool, if present."""
    try:
        result = subprocess.run(
            [find_binary("exiftool"), "-n", "-json", "-GPSLatitude", "-GPSLongitude", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        data = json.loads(result.stdout)[0]
        lat = data.get("GPSLatitude")
        lon = data.get("GPSLongitude")
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)
    except Exception as e:
        print(f"[geocode] GPS read failed: {e}")
        return None


def _gps_for_asset(asset: dict | None):
    """Return cached or lazily-read GPS coordinates for a selected asset."""
    if not asset:
        return None
    lat = asset.get("gps_lat")
    lon = asset.get("gps_lon")
    if lat is not None and lon is not None:
        return {"lat": float(lat), "lon": float(lon)}

    path = asset.get("path")
    if not path or not os.path.exists(path):
        return None
    coords = _read_gps_coordinates(path)
    if not coords:
        return None
    lat, lon = coords
    try:
        from models.db import update_asset_gps
        update_asset_gps(path, lat, lon)
    except Exception as e:
        print(f"[geocode] GPS cache update failed: {e}")
    return {"lat": lat, "lon": lon}


def _blocked_location_name(name: str) -> bool:
    name = str(name or "").strip()
    if not name:
        return True
    name_key = name.lower()
    business_suffixes = (" llc", " inc", " corp", " corporation", " co.", " company")
    blocked_name_terms = (
        "|",
        "!",
        "·",
        "100%",
        "african americans",
        "bar & grille",
        "bar and grille",
        "bedroom",
        "charging",
        "circu",
        "condominium",
        "condos",
        "ghost tours",
        "grill",
        "grille",
        "haunted",
        "health care",
        "location, location",
        "main street at",
        "love",
        "management",
        "modern updates",
        "new pathways",
        "ollien",
        "parking",
        "properties",
        "pub crawl",
        "realty",
        "residences",
        "apartments",
        "apartment",
    )
    return (
        name_key.endswith(business_suffixes)
        or any(term in name_key for term in blocked_name_terms)
        or len(name) > 64
    )


def _safe_location_place_name(name: str, types: set[str] | None = None) -> bool:
    name = str(name or "").strip()
    if not name or _blocked_location_name(name):
        return False
    name_key = name.lower()
    safe_place_types = {
        "tourist_attraction",
        "museum",
        "park",
        "natural_feature",
        "transit_station",
        "train_station",
        "light_rail_station",
        "airport",
    }
    public_name_terms = (
        "amphitheater",
        "bridge",
        "garden",
        "historic",
        "landing",
        "memorial",
        "monument",
        "museum",
        "park",
        "riverboat",
        "riverwalk",
        "sign",
        "trail",
        "trolley",
    )
    return bool((types or set()) & safe_place_types) or any(term in name_key for term in public_name_terms)


def _google_location_context(lat: float, lon: float, api_key: str) -> dict:
    """Return safe keyword terms plus loose GPS/Google context for prompting."""
    if not api_key:
        return {"keywords": [], "hint_terms": []}

    cache_key = f"{lat:.5f},{lon:.5f}"
    if cache_key in GEOCODE_CONTEXT_CACHE:
        cached = GEOCODE_CONTEXT_CACHE[cache_key]
        return {
            "keywords": list(cached.get("keywords") or []),
            "hint_terms": list(cached.get("hint_terms") or []),
        }

    try:
        import requests
        response = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"latlng": f"{lat},{lon}", "key": api_key},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") not in ("OK", "ZERO_RESULTS"):
            print(f"[geocode] Google status: {payload.get('status')}")
            return []

        component_types = {
            "locality",
            "administrative_area_level_1",
            "country",
            "neighborhood",
            "sublocality",
            "sublocality_level_1",
            "administrative_area_level_2",
        }
        safe_landmark_types = {
            "natural_feature",
            "park",
            "tourist_attraction",
            "airport",
            "transit_station",
        }
        # Google tags nearly every park/attraction with "establishment" too, so
        # blocking on it discards e.g. national parks. Block only true businesses.
        unsafe_poi_types = {
            "restaurant",
            "food",
            "store",
            "lodging",
            "bar",
            "cafe",
            "premise",
            "street_address",
        }

        keywords = []
        hint_terms = []
        for item in payload.get("results", []):
            types = set(item.get("types", []))
            if types & safe_landmark_types and not types & unsafe_poi_types:
                components = item.get("address_components", [])
                if components and _safe_location_place_name(components[0].get("long_name", ""), types):
                    keywords.append(components[0].get("long_name", ""))
                    hint_terms.append(components[0].get("long_name", ""))

            for comp in item.get("address_components", []):
                comp_types = set(comp.get("types", []))
                comp_name = comp.get("long_name", "")
                if comp_types & component_types and not _blocked_location_name(comp_name):
                    keywords.append(comp_name)
                    hint_terms.append(comp_name)

        try:
            places_response = requests.get(
                "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
                params={
                    "location": f"{lat},{lon}",
                    "rankby": "distance",
                    "key": api_key,
                },
                timeout=10,
            )
            places_response.raise_for_status()
            places_payload = places_response.json()
            if places_payload.get("status") in ("OK", "ZERO_RESULTS"):
                for place in places_payload.get("results", [])[:3]:
                    name = str(place.get("name") or "").strip()
                    types = set(place.get("types", []))
                    if _safe_location_place_name(name, types):
                        hint_terms.append(name)
                        # Photo taken AT this place (not just near it) — the
                        # place name belongs in keywords, e.g. Kerry Park.
                        loc = ((place.get("geometry") or {}).get("location") or {})
                        try:
                            # 250m: POI markers (waterfalls, viewpoints) sit a
                            # short walk from where photos are actually taken
                            if loc and _distance_km(lat, lon, loc["lat"], loc["lng"]) <= 0.25:
                                keywords.append(name)
                        except Exception:
                            pass
            else:
                print(f"[geocode] Google Places status: {places_payload.get('status')}")
        except Exception as e:
            print(f"[geocode] Places lookup failed: {e}")

        cleaned = []
        seen = set()
        for kw in keywords:
            kw = str(kw).strip()
            if kw and kw.lower() not in seen:
                cleaned.append(kw)
                seen.add(kw.lower())

        cleaned_hints = []
        seen_hints = set()
        for term in hint_terms:
            term = str(term).strip()
            key = term.lower()
            if term and key not in seen_hints:
                cleaned_hints.append(term)
                seen_hints.add(key)

        context = {"keywords": cleaned, "hint_terms": cleaned_hints[:10]}
        GEOCODE_CONTEXT_CACHE[cache_key] = context
        GEOCODE_CACHE[cache_key] = cleaned
        return {"keywords": list(cleaned), "hint_terms": list(cleaned_hints[:10])}
    except Exception as e:
        print(f"[geocode] Reverse geocode failed: {e}")
        return {"keywords": [], "hint_terms": []}


def _reverse_geocode_keywords(lat: float, lon: float, api_key: str) -> list[str]:
    """Return city/state/country plus safe landmark keywords from Google Geocoding."""
    return _google_location_context(lat, lon, api_key).get("keywords") or []


def _osm_containment_names(lat: float, lon: float) -> list[str]:
    """Named areas (national parks, protected areas, city parks) containing this
    point, via OSM Overpass polygon containment. Google reverse geocoding does
    not return enclosing parks, so distance-based lookups miss e.g. being
    inside Mount Rainier National Park — is_in() is immune to GPS drift."""
    cache_key = f"{lat:.4f},{lon:.4f}"
    if cache_key in OSM_CONTAIN_CACHE:
        return list(OSM_CONTAIN_CACHE[cache_key])
    try:
        import requests
        query = (
            f'[out:json][timeout:8];is_in({lat},{lon})->.a;'
            '(area.a[boundary~"national_park|protected_area"][name];'
            'area.a[leisure=park][name];);out tags;'
        )
        resp = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            headers={"User-Agent": "ToTag/3.3 (personal metadata tool)"},
            timeout=12,
        )
        resp.raise_for_status()
        tags = [e.get("tags", {}) for e in resp.json().get("elements", [])]
    except Exception as e:
        print(f"[osm] containment lookup failed: {e}")
        return []  # don't cache failures

    def _rank(t):
        name = (t.get("name") or "").lower()
        if t.get("boundary") == "national_park" or "national park" in name:
            return 0
        if t.get("leisure") == "park":
            return 1
        return 2

    names = []
    for t in sorted(tags, key=_rank):
        name = (t.get("name:en") or t.get("name") or "").strip()
        # English keywords only — skip names without a latin form
        if not name or len(name) > 60 or any(ord(c) > 0x24F for c in name):
            continue
        if name not in names:
            names.append(name)
    names = names[:3]
    OSM_CONTAIN_CACHE[cache_key] = list(names)
    return names


def _geotag_keywords(path: str, api_key: str) -> list[str]:
    coords = _read_gps_coordinates(path)
    if not coords:
        return []
    keywords = list(_reverse_geocode_keywords(*coords, api_key))
    keywords = _append_unique(keywords, _osm_containment_names(*coords))
    if keywords:
        print(f"[geocode] GPS keywords for {os.path.basename(path)}: {', '.join(keywords[:8])}")
    return keywords


def _geotag_location_hint(path: str, api_key: str) -> str:
    coords = _read_gps_coordinates(path)
    if not coords:
        return ""
    context = _google_location_context(*coords, api_key)
    terms = _append_unique(list(context.get("hint_terms") or []), _osm_containment_names(*coords))
    if terms:
        print(f"[geocode] GPS context for {os.path.basename(path)}: {', '.join(terms[:6])}")
    return ", ".join(terms[:8])
