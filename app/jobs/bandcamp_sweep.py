"""Job 9 — nightly Bandcamp search sweep (approved 3 Jul 2026).

This deliberately loosens the original "no auto-search against Bandcamp"
stance (spec §11 / bandcamp.py docstring): Matthias approved a capped,
polite, night-only sweep after learning bulk enrichment gets ZERO Bandcamp
data — enrich() without a manual_url returns unmatched by design, and
Bandcamp's tag quality is strongest exactly where the API sources are
thinnest (his underground library).

Hard caps, non-negotiable:
  · ≤ BANDCAMP_SWEEP_BATCH_SIZE lookups per night (default 500)
  · BANDCAMP_SWEEP_RATE_SECONDS sleep between requests (default 4)
  · night window only (enforced by the scheduler that calls this)
  · one User-Agent, no concurrency
  · a missed track is never retried for 30 days (bandcamp_search_missed_at)
  · degrade gracefully: 403/429/captcha or an unrecognised response shape
    stops the night's sweep IMMEDIATELY and marks the job degraded — never
    hammer a refusing endpoint.

Search: Bandcamp's public autocomplete endpoint first, HTML search-page
parse as fallback. A match needs normalised artist agreement AND title
similarity ≥ 0.85, item type track or album. On match the page is fetched
through the EXISTING enrich_by_url() JSON-LD parser, tags land as source
'bandcamp' through the existing track_tags write path, and the URL is stored
in tracks.bandcamp_url so future re-enrichment uses the manual_url path.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import requests

from app.db.connection import db_conn
from app.enrichment import bandcamp
from app.enrichment.pipeline import _update_enrichment_state, _write_tags
from app.ingestion.normalise import _unicode_normalise, normalise_artist, normalise_title

logger = logging.getLogger(__name__)

_AUTOCOMPLETE_URL = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"
_SEARCH_URL = "https://bandcamp.com/search"
_USER_AGENT = "Mozilla/5.0 (compatible; MusicIntelligenceSystem/0.1)"

_TITLE_SIMILARITY = 0.85
_MISS_RETRY_DAYS = 30


class SweepDegraded(Exception):
    """Endpoint refused or changed shape — stop the night's sweep now."""


def _batch_size() -> int:
    return int(os.getenv("BANDCAMP_SWEEP_BATCH_SIZE", "500"))


def _rate_seconds() -> float:
    return float(os.getenv("BANDCAMP_SWEEP_RATE_SECONDS", "4"))


def _check_refusal(status_code: int, body: str) -> None:
    if status_code in (403, 429):
        raise SweepDegraded(f"HTTP {status_code} from Bandcamp")
    if "captcha" in body[:2000].lower():
        raise SweepDegraded("captcha challenge in response")


def _search_autocomplete(query: str) -> list[dict]:
    """Public autocomplete endpoint. Returns [{name, band_name, url, type}]."""
    resp = requests.post(
        _AUTOCOMPLETE_URL,
        json={"search_text": query, "search_filter": "",
              "full_page": False, "fan_id": None},
        headers={"User-Agent": _USER_AGENT},
        timeout=15,
    )
    _check_refusal(resp.status_code, resp.text)
    resp.raise_for_status()
    data = resp.json()
    results = (data.get("auto") or {}).get("results")
    if results is None:
        raise SweepDegraded("autocomplete response missing auto.results")
    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        # type: 't' = track, 'a' = album (other values: bands, fans — skip)
        if r.get("type") not in ("t", "a"):
            continue
        url = r.get("item_url_path") or ""
        if not url and r.get("item_url_root"):
            url = r["item_url_root"]
        if not url:
            continue
        out.append({
            "title": r.get("name") or "",
            "artist": r.get("band_name") or "",
            "url": url,
            "kind": "track" if r["type"] == "t" else "album",
        })
    return out


def _search_html(query: str) -> list[dict]:
    """Fallback: parse the public search-result page."""
    import re

    resp = requests.get(
        _SEARCH_URL,
        params={"q": query, "item_type": "t"},
        headers={"User-Agent": _USER_AGENT},
        timeout=15,
    )
    _check_refusal(resp.status_code, resp.text)
    resp.raise_for_status()
    html = resp.text
    out = []
    # Each result block: itemtype heading link + "by <artist>" subhead.
    for block in re.findall(
        r'<li class="searchresult[^"]*".*?</li>', html, re.DOTALL
    )[:10]:
        m_url = re.search(r'<a href="(https://[^"]+)"', block)
        m_title = re.search(r'<div class="heading">\s*<a[^>]*>([^<]+)</a>', block)
        m_artist = re.search(r'<div class="subhead">\s*(?:by\s+)?([^<\n]+)', block)
        if not (m_url and m_title):
            continue
        out.append({
            "title": m_title.group(1).strip(),
            "artist": (m_artist.group(1).strip() if m_artist else ""),
            "url": m_url.group(1).split("?")[0],
            "kind": "track",
        })
    if not out and "searchresult" not in html:
        raise SweepDegraded("search page shape unrecognised (no result markup)")
    return out


def _best_match(results: list[dict], artist: str, title: str) -> dict | None:
    """Top result clearing normalised artist + title-similarity gates."""
    want_artist = _unicode_normalise(normalise_artist(artist))
    want_title = _unicode_normalise(normalise_title(title))
    for r in results:
        got_artist = _unicode_normalise(normalise_artist(r["artist"] or ""))
        got_title = _unicode_normalise(normalise_title(r["title"] or ""))
        if not got_title:
            continue
        artist_ok = bool(want_artist) and (
            want_artist == got_artist
            or want_artist in got_artist
            or got_artist in want_artist
            # album results sometimes carry the label as band_name; let a
            # title that embeds the artist through ("Artist - Title")
            or want_artist in got_title
        )
        if not artist_ok:
            continue
        sim = SequenceMatcher(None, want_title, got_title).ratio()
        if sim >= _TITLE_SIMILARITY or (
            r["kind"] == "album" and want_title in got_title
        ):
            return r
    return None


def _select_targets(conn, limit: int) -> list[dict]:
    """Sweep queue, hard-capped. Priority: rated → public_metadata_weak →
    reference/verdict-relevant → rest by created_at. Rated + weak first is
    the point — Bandcamp is strongest where the API sources are thinnest."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=_MISS_RETRY_DAYS)
    ).isoformat()
    return [dict(r) for r in conn.execute(
        """
        SELECT t.track_pk, t.canonical_title, t.canonical_artist
        FROM tracks t
        LEFT JOIN enrichment_state es ON es.track_pk = t.track_pk
        WHERE t.bandcamp_url IS NULL
          AND COALESCE(es.has_bandcamp_data, 0) = 0
          AND (es.bandcamp_search_missed_at IS NULL
               OR es.bandcamp_search_missed_at < ?)
        ORDER BY
          CASE
            WHEN t.personal_rating IS NOT NULL THEN 0
            WHEN t.match_status = 'public_metadata_weak' THEN 1
            WHEN EXISTS (SELECT 1 FROM reference_track_labels r
                         WHERE r.track_pk = t.track_pk) THEN 2
            ELSE 3
          END,
          t.created_at ASC
        LIMIT ?
        """,
        (cutoff, limit),
    )]


def run_sweep(db_path: str | None = None, sleep=time.sleep) -> dict:
    """One night's sweep. Returns lookup/hit counts (both go in the digest)."""
    stats = {"lookups": 0, "hits": 0, "misses": 0, "errors": 0, "degraded": False}
    rate = _rate_seconds()

    with db_conn(db_path) as conn:
        targets = _select_targets(conn, _batch_size())

    for t in targets:
        query = f"{t['canonical_artist']} {t['canonical_title']}".strip()
        try:
            sleep(rate)
            stats["lookups"] += 1
            try:
                results = _search_autocomplete(query)
            except SweepDegraded:
                raise
            except Exception as e:  # endpoint hiccup → try the HTML fallback
                logger.info("autocomplete failed (%s), trying HTML search", e)
                sleep(rate)
                results = _search_html(query)

            match = _best_match(results, t["canonical_artist"], t["canonical_title"])
            if match is None:
                _stamp_miss(t["track_pk"], db_path)
                stats["misses"] += 1
                continue

            # enrich_by_url sleeps its own polite rate limit before fetching.
            bc = bandcamp.enrich_by_url(match["url"])
            if not bc.matched:
                _stamp_miss(t["track_pk"], db_path)
                stats["misses"] += 1
                continue

            with db_conn(db_path) as conn:
                if bc.tags:
                    _write_tags(t["track_pk"], [
                        {"tag": tag, "source": "bandcamp",
                         "confidence": bc.confidence, "tag_type": "public"}
                        for tag in bc.tags
                    ], conn)
                _update_enrichment_state(t["track_pk"], {
                    "has_bandcamp_data": 1,
                    "bandcamp_unavailable": 0,
                    "bandcamp_checked_at": datetime.now(timezone.utc).isoformat(),
                }, conn)
                if bc.tags:
                    _update_enrichment_state(
                        t["track_pk"], {"has_community_tags": 1}, conn
                    )
                conn.execute(
                    "UPDATE tracks SET bandcamp_url = ?, updated_at = ? WHERE track_pk = ?",
                    (match["url"], datetime.now(timezone.utc).isoformat(),
                     t["track_pk"]),
                )
            stats["hits"] += 1
        except SweepDegraded as e:
            logger.warning("Bandcamp sweep degraded — stopping: %s", e)
            stats["degraded"] = True
            stats["degraded_reason"] = str(e)
            break
        except Exception:  # noqa: BLE001 — one bad track never ends the night
            logger.exception("Bandcamp sweep error for %s", t["track_pk"])
            stats["errors"] += 1

    return stats


def _stamp_miss(track_pk: str, db_path: str | None) -> None:
    with db_conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO enrichment_state (track_pk) VALUES (?)",
            (track_pk,),
        )
        conn.execute(
            "UPDATE enrichment_state SET bandcamp_search_missed_at = ? WHERE track_pk = ?",
            (datetime.now(timezone.utc).isoformat(), track_pk),
        )
