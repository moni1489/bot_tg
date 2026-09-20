from __future__ import annotations

"""Lightweight pop-culture validation/recovery via public Wikidata.

This module is deliberately NOT a Funko catalogue. It provides a general
character/work vocabulary so OCR garbage cannot masquerade as a character and
real character names can be validated against a franchise. Results are cached
locally and network failures are non-fatal.
"""

import json
import logging
import re
import threading
import time
from pathlib import Path
from difflib import SequenceMatcher

import httpx

log = logging.getLogger(__name__)

_API = "https://www.wikidata.org/w/api.php"
_SPARQL = "https://query.wikidata.org/sparql"
_CACHE_FILE = Path("data/culture_cache.json")
_LOCK = threading.Lock()
_CACHE: dict[str, object] | None = None
_TTL = 30 * 24 * 3600


def _load_cache() -> dict[str, object]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        try:
            raw = json.loads(_CACHE_FILE.read_text("utf-8"))
            _CACHE = raw if isinstance(raw, dict) else {}
        except Exception:
            _CACHE = {}
        return _CACHE


def _save_cache() -> None:
    cache = _load_cache()
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        log.debug("culture cache save failed", exc_info=True)


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().casefold())


def _clean_label(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9'&:!?.\- ]+", " ", text or "")).strip()


def _search_entities(query: str, limit: int = 8) -> list[dict]:
    query = _clean_label(query)
    if len(query) < 3:
        return []
    cache = _load_cache()
    key = f"search::{_key(query)}"
    cached = cache.get(key)
    if isinstance(cached, dict) and time.time() - float(cached.get("ts", 0)) < _TTL:
        return cached.get("rows", []) if isinstance(cached.get("rows"), list) else []
    params = {
        "action": "wbsearchentities",
        "search": query,
        "language": "en",
        "uselang": "en",
        "format": "json",
        "limit": str(limit),
        "type": "item",
    }
    try:
        r = httpx.get(_API, params=params, timeout=6.0, headers={"User-Agent": "FunkoDealBot/46 culture resolver"})
        r.raise_for_status()
        data = r.json()
        rows = data.get("search", []) if isinstance(data, dict) else []
    except Exception:
        log.debug("Wikidata search failed for %r", query, exc_info=True)
        rows = []
    with _LOCK:
        cache[key] = {"ts": time.time(), "rows": rows}
        _save_cache()
    return rows


def _entity(entity_id: str) -> dict:
    cache = _load_cache()
    key = f"entity::{entity_id}"
    cached = cache.get(key)
    if isinstance(cached, dict) and time.time() - float(cached.get("ts", 0)) < _TTL:
        data = cached.get("data")
        return data if isinstance(data, dict) else {}
    try:
        r = httpx.get(_API, params={
            "action": "wbgetentities",
            "ids": entity_id,
            "languages": "en",
            "props": "labels|descriptions|claims|aliases",
            "format": "json",
        }, timeout=6.0, headers={"User-Agent": "FunkoDealBot/46 culture resolver"})
        r.raise_for_status()
        data = r.json().get("entities", {}).get(entity_id, {})
        if not isinstance(data, dict):
            data = {}
    except Exception:
        log.debug("Wikidata entity failed %s", entity_id, exc_info=True)
        data = {}
    with _LOCK:
        cache[key] = {"ts": time.time(), "data": data}
        _save_cache()
    return data


def _label(entity_id: str) -> str:
    data = _entity(entity_id)
    return str(data.get("labels", {}).get("en", {}).get("value") or "").strip()


def _description(entity_id: str) -> str:
    data = _entity(entity_id)
    return str(data.get("descriptions", {}).get("en", {}).get("value") or "").strip().casefold()


def _candidate_is_work(row: dict) -> bool:
    desc = str(row.get("description") or "").casefold()
    label = str(row.get("label") or "").casefold()
    return any(word in desc for word in ("television series", "tv series", "film", "movie", "anime", "manga", "comic", "video game", "series of", "fictional work")) or any(word in label for word in ("sopranos", "bleach", "naruto", "marvel", "pokemon"))


def resolve_work(query: str) -> str | None:
    """Return the best matching creative-work label, or None."""
    query = _clean_label(query)
    if not query:
        return None
    rows = _search_entities(query)
    best: tuple[float, str] | None = None
    q = _key(query)
    for row in rows:
        label = str(row.get("label") or "").strip()
        eid = str(row.get("id") or "").strip()
        if not label or not eid:
            continue
        ratio = SequenceMatcher(None, q, _key(label)).ratio()
        if _key(label) == q:
            ratio = 1.0
        if ratio < 0.72:
            continue
        if not _candidate_is_work(row):
            # Entity descriptions are often better than labels, but keep exact matches.
            if ratio < 0.97:
                continue
        score = ratio
        if best is None or score > best[0]:
            best = (score, label)
    return best[1] if best else None


def _entity_ids_for_exact_label(label: str) -> list[str]:
    q = _key(label)
    out: list[tuple[float, str]] = []
    for row in _search_entities(label, limit=10):
        lab = str(row.get("label") or "").strip()
        eid = str(row.get("id") or "").strip()
        if not eid or not lab:
            continue
        ratio = SequenceMatcher(None, q, _key(lab)).ratio()
        if _key(lab) == q:
            ratio = 1.0
        if ratio >= 0.82:
            out.append((ratio, eid))
    out.sort(reverse=True)
    return [eid for _, eid in out[:5]]


def character_matches_work(character: str, work: str) -> bool | None:
    """Validate a character/work relation using Wikidata P1441.

    Returns True when confirmed, False when an exact entity is found but relation
    is not confirmed, and None when Wikidata could not confidently resolve the pair.
    """
    character = _clean_label(character)
    work = _clean_label(work)
    if not character or not work:
        return None
    cache = _load_cache()
    key = f"pair::{_key(character)}::{_key(work)}"
    cached = cache.get(key)
    if isinstance(cached, dict) and time.time() - float(cached.get("ts", 0)) < _TTL:
        value = cached.get("value")
        return value if value in {True, False, None} else None

    work_ids = _entity_ids_for_exact_label(work)
    char_rows = _search_entities(character, limit=10)
    char_ids: list[tuple[float, str]] = []
    cq = _key(character)
    for row in char_rows:
        label = str(row.get("label") or "").strip()
        eid = str(row.get("id") or "").strip()
        if not eid or not label:
            continue
        ratio = SequenceMatcher(None, cq, _key(label)).ratio()
        if _key(label) == cq:
            ratio = 1.0
        if ratio >= 0.82:
            char_ids.append((ratio, eid))
    char_ids.sort(reverse=True)
    char_ids = char_ids[:8]
    if not char_ids or not work_ids:
        value = None
    else:
        work_set = set(work_ids)
        value = False
        found_character = False
        for _, cid in char_ids:
            entity = _entity(cid)
            claims = entity.get("claims", {}) if isinstance(entity, dict) else {}
            p1441 = claims.get("P1441", []) if isinstance(claims, dict) else []
            target_ids: set[str] = set()
            for statement in p1441 or []:
                try:
                    target = statement["mainsnak"]["datavalue"]["value"]["id"]
                    if target:
                        target_ids.add(str(target))
                except Exception:
                    continue
            if target_ids:
                found_character = True
                if target_ids & work_set:
                    value = True
                    break
        if not found_character:
            value = None
    with _LOCK:
        cache[key] = {"ts": time.time(), "value": value}
        _save_cache()
    return value


def choose_character(candidate: str, work: str | None = None) -> tuple[str | None, float]:
    """Canonicalize a candidate name against the general culture index.

    A canonical Wikidata label is returned when an exact-ish entity exists. The
    score is a validation confidence, not an absolute probability.
    """
    candidate = _clean_label(candidate)
    if len(candidate) < 3:
        return None, 0.0
    rows = _search_entities(candidate, limit=8)
    cq = _key(candidate)
    best: tuple[float, str] | None = None
    for row in rows:
        label = str(row.get("label") or "").strip()
        eid = str(row.get("id") or "").strip()
        desc = str(row.get("description") or "").casefold()
        if not label or not eid:
            continue
        ratio = SequenceMatcher(None, cq, _key(label)).ratio()
        if _key(label) == cq:
            ratio = 1.0
        if ratio < 0.80:
            continue
        fictional_hint = any(x in desc for x in ("fictional character", "character", "fictional", "superhero", "anime character", "manga character"))
        score = ratio + (0.12 if fictional_hint else 0.0)
        if work:
            relation = character_matches_work(label, work)
            if relation is True:
                score += 0.35
            elif relation is False:
                score -= 0.30
        if best is None or score > best[0]:
            best = (score, label)
    if not best:
        return None, 0.0
    return best[1], max(0.0, min(1.0, best[0] / 1.45))
