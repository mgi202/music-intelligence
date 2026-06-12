"""
Track normalisation and track_pk generation.

Two things happen here:
1. Strip noise from raw title/artist strings so cross-source matching works.
2. Derive a stable, deterministic track_pk.

track_pk scheme (spec v1.4, Section 8):
  ISRC available  → "isrc:{ISRC}"
  No ISRC         → "synthetic:{sha256(normalized_artist + normalized_title + duration_ms)}"

Normalisation rules (spec Section 8):
  Strip:    "Original Mix", "Extended Mix", "Radio Edit", "Remastered",
            "Official Audio", "Visualizer", catalogue numbers, YouTube junk suffixes
  Preserve: remix credits — "Surgeon Remix", "Blawan Edit" etc.
            Remix identity IS track identity in electronic music.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from app.ingestion.base import TrackToken

# ── Patterns to strip from titles ────────────────────────────────────────────

# Version suffixes that add no musical information
_VERSION_NOISE = re.compile(
    r"\s*[\(\[]\s*(?:"
    r"original\s+mix"
    r"|extended\s+mix"
    r"|extended\s+version"
    r"|radio\s+edit"
    r"|single\s+version"
    r"|album\s+version"
    r"|clean\s+version"
    r"|explicit\s+version"
    r"|remastered(?:\s+\d{4})?"
    r"|\d{4}\s+remaster(?:ed)?"
    r"|official\s+(?:audio|video|music\s+video|visuali[sz]er|lyric\s+video)"
    r"|hd\s+(?:audio|video)"
    r"|full\s+(?:song|track|album|version)"
    r"|audio"
    r"|visuali[sz]er"
    r"|lyrics?\s+video"
    r")\s*[\)\]]\s*",
    re.IGNORECASE,
)

# YouTube-style catalogue/upload junk appended after a dash or pipe
_YOUTUBE_JUNK = re.compile(
    r"\s*[-|]\s*(?:official\s+(?:audio|video|music\s+video)|"
    r"free\s+download|out\s+now|premiere|hq|hd)\s*$",
    re.IGNORECASE,
)

# Catalogue numbers at start of title (e.g. "DFTD001 - Track Name")
_CATALOGUE_PREFIX = re.compile(r"^[A-Z0-9]{2,10}\d{1,6}\s*[-–]\s*", re.IGNORECASE)

# Leading/trailing whitespace and punctuation artefacts
_WHITESPACE_NORMALISE = re.compile(r"\s{2,}")


def _unicode_normalise(s: str) -> str:
    """NFKC normalisation + lower + strip."""
    return unicodedata.normalize("NFKC", s).lower().strip()


def normalise_title(raw: str) -> str:
    """
    Clean a raw track title.

    Strips noise versions/suffixes while preserving remix credits.
    The remix credit is part of the track's identity — do not remove it.
    """
    t = raw.strip()
    t = _CATALOGUE_PREFIX.sub("", t)
    t = _VERSION_NOISE.sub(" ", t)
    t = _YOUTUBE_JUNK.sub("", t)
    t = _WHITESPACE_NORMALISE.sub(" ", t).strip()
    return t


def normalise_artist(raw: str) -> str:
    """
    Normalise artist name for matching.

    Strips 'The ' prefix for matching purposes only.
    Does not alter the canonical_artist stored in the ledger.
    """
    a = raw.strip()
    a = _WHITESPACE_NORMALISE.sub(" ", a).strip()
    return a


def compute_track_pk(token: TrackToken) -> str:
    """
    Compute the stable track_pk for a TrackToken.

    Uses ISRC when available. Falls back to a sha256 synthetic key derived
    from normalized_artist + normalized_title + duration_ms.

    Call this after normalized_artist, normalized_title, and isrc are set.
    """
    if token.isrc:
        return f"isrc:{token.isrc.upper().strip()}"

    norm_artist = _unicode_normalise(token.normalized_artist or token.canonical_artist)
    norm_title = _unicode_normalise(token.normalized_title or token.canonical_title)
    duration_part = str(token.duration_ms) if token.duration_ms else "unknown"

    fingerprint = f"{norm_artist}|{norm_title}|{duration_part}"
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
    return f"synthetic:{digest}"


def normalise_token(token: TrackToken) -> TrackToken:
    """
    In-place normalisation of a TrackToken.

    Sets canonical_title, canonical_artist, normalized_title,
    normalized_artist, and track_pk.
    Returns the same token (mutated).
    """
    token.canonical_title = normalise_title(token.raw_title) or token.raw_title
    token.canonical_artist = normalise_artist(token.raw_artist) or token.raw_artist
    token.normalized_title = _unicode_normalise(token.canonical_title)
    token.normalized_artist = _unicode_normalise(token.canonical_artist)
    token.track_pk = compute_track_pk(token)
    return token
