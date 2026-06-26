"""
Reference label manager — add, remove, list, and audit reference_track_labels.

Reference labels are NOT tags. A tag asserts "this track IS X". A reference
label asserts "use this track as a {positive|negative|near_miss} EXEMPLAR for
profile X" — it carries polarity and a profile binding that a flat tag cannot.

These labels are the hand-curated training signal for the Stage 1 kNN
classifier. The classifier may only auto-apply a profile once that profile has
accumulated enough references (see profile_readiness):

    >= 15 positive  AND  >= 15 (negative + near_miss)  from >= 3 distinct artists

This module is the single entry point for all reference-label operations, and
mirrors the conventions of tag_manager.py (db_conn, dict rows, idempotency).

Although the kNN classifier is Stage 1, labelling is done by hand NOW, while
rating — the "is this a clean positive / a tricky near_miss" judgement is cheap
with the track in your ears and expensive to reconstruct later.
"""

from __future__ import annotations

import sqlite3

from app.db.connection import db_conn, get_connection

LABEL_TYPES = ("positive", "negative", "near_miss")

# created_by markers — let the reconcile sweep tell auto-derived labels apart
# from hand-made ones. Anything starting with "auto:" is owned by the
# derivation engine and may be added/removed automatically; everything else
# (CREATED_BY_MANUAL, CREATED_BY_VETO) is user intent and is never touched.
CREATED_BY_MANUAL = "manual"
CREATED_BY_TAG = "auto:tag"          # positive, derived from a manual tag
CREATED_BY_OPPOSING = "auto:opposing"  # negative, derived from an opposing positive
CREATED_BY_VETO = "veto"             # near_miss, user said "not a good example"
_AUTO_PREFIX = "auto:"

# Opposing-profiles map — which profiles are acoustically/contextually far
# enough apart that a POSITIVE exemplar of one is a sound NEGATIVE exemplar of
# the other. Stage 0 has no audio features yet, so contrast is the only way to
# populate negatives automatically. Declared one direction; symmetrised at load.
# Profiles NOT listed as opposites are treated as "too close to use as a clean
# negative" (e.g. hypnotic-rolling vs peak-time-dark-techno overlap heavily).
# Edit freely — this is the one piece of hand-tuning the system relies on.
_OPPOSING_PROFILES_DECL: dict[str, tuple[str, ...]] = {
    "peak-time-dark-techno": (
        "focus-minimal", "melodic-late-night", "late-night-drive", "afterhours-dubby",
    ),
    "warehouse-industrial": (
        "focus-minimal", "melodic-late-night", "late-night-drive", "euphoric-anthem",
    ),
    "euphoric-anthem": (
        "warehouse-industrial", "focus-minimal", "afterhours-dubby",
    ),
    "gym-aggressive": (
        "focus-minimal", "afterhours-dubby", "late-night-drive", "melodic-late-night",
    ),
    "warm-up-groove": (
        "peak-time-dark-techno", "warehouse-industrial", "gym-aggressive",
    ),
    "hypnotic-rolling": (
        "euphoric-anthem", "gym-aggressive",
    ),
}


def opposing_profiles_map() -> dict[str, set[str]]:
    """The opposing map, symmetrised: if A opposes B then B opposes A."""
    out: dict[str, set[str]] = {}
    for a, opps in _OPPOSING_PROFILES_DECL.items():
        for b in opps:
            out.setdefault(a, set()).add(b)
            out.setdefault(b, set()).add(a)
    return out

# A label that contradicts the polarity of another label on the same
# (track, profile). A track cannot be both a positive and a negative exemplar
# for the same profile.
_OPPOSING = {
    "positive": ("negative", "near_miss"),
    "negative": ("positive",),
    "near_miss": ("positive",),
}


def add_reference_label(
    track_pk: str,
    profile_id: str,
    label_type: str,
    notes: str | None = None,
    confidence: float = 1.0,
    reference_source: str = "manual",
    created_by: str = "manual",
    db_path: str | None = None,
) -> bool:
    """
    Label a track as a reference exemplar for a profile.

    Idempotent — if the exact (track, profile, label_type) already exists,
    returns False. Returns True if a new label was created.

    `created_by` records the mechanism so auto-derived labels can be told
    apart from hand-made ones (see CREATED_BY_* constants). It is never
    overwritten by the polarity guard or the reconcile sweep.

    Raises ValueError if:
      - label_type is not one of positive/negative/near_miss
      - the track or profile does not exist
      - the track already carries an OPPOSING label for this profile
        (e.g. trying to add 'positive' when a 'negative' exists)
    """
    if label_type not in LABEL_TYPES:
        raise ValueError(
            f"label_type must be one of {LABEL_TYPES}, got {label_type!r}"
        )

    with db_conn(db_path) as conn:
        if not conn.execute(
            "SELECT 1 FROM tracks WHERE track_pk = ?", (track_pk,)
        ).fetchone():
            raise ValueError(f"Track not found: {track_pk}")

        if not conn.execute(
            "SELECT 1 FROM tag_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone():
            raise ValueError(f"Profile not found: {profile_id}")

        # Polarity guard — refuse contradictory exemplars.
        opposing = _OPPOSING[label_type]
        clash = conn.execute(
            f"""
            SELECT label_type FROM reference_track_labels
            WHERE track_pk = ? AND profile_id = ?
              AND label_type IN ({",".join("?" * len(opposing))})
            """,
            (track_pk, profile_id, *opposing),
        ).fetchone()
        if clash:
            raise ValueError(
                f"Track {track_pk} is already a '{clash['label_type']}' exemplar "
                f"for {profile_id}; cannot also be '{label_type}'. "
                f"Remove the existing label first."
            )

        # Idempotent insert on the UNIQUE(track_pk, profile_id, label_type).
        try:
            conn.execute(
                """
                INSERT INTO reference_track_labels (
                    track_pk, profile_id, label_type,
                    confidence, notes, reference_source, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (track_pk, profile_id, label_type, confidence, notes,
                 reference_source, created_by),
            )
        except sqlite3.IntegrityError:
            return False  # Already labelled with this exact polarity.

        return True


def remove_reference_label(
    track_pk: str,
    profile_id: str,
    label_type: str | None = None,
    db_path: str | None = None,
) -> int:
    """
    Remove reference label(s) for a (track, profile).

    If label_type is given, removes only that polarity. If None, removes all
    labels for the track on that profile. Returns the number of rows removed.
    """
    with db_conn(db_path) as conn:
        if label_type is not None:
            if label_type not in LABEL_TYPES:
                raise ValueError(
                    f"label_type must be one of {LABEL_TYPES}, got {label_type!r}"
                )
            result = conn.execute(
                """
                DELETE FROM reference_track_labels
                WHERE track_pk = ? AND profile_id = ? AND label_type = ?
                """,
                (track_pk, profile_id, label_type),
            )
        else:
            result = conn.execute(
                """
                DELETE FROM reference_track_labels
                WHERE track_pk = ? AND profile_id = ?
                """,
                (track_pk, profile_id),
            )
        return result.rowcount


def list_reference_labels(
    track_pk: str | None = None,
    profile_id: str | None = None,
    label_type: str | None = None,
    db_path: str | None = None,
) -> list[dict]:
    """
    List reference labels, optionally filtered by any combination of
    track_pk, profile_id, and label_type.

    Returns rows joined to track identity, ordered profile → polarity → artist.
    Keys: track_pk, profile_id, label_type, confidence, notes,
          reference_source, created_at, canonical_title, canonical_artist
    """
    conn = get_connection(db_path)
    try:
        where, params = [], []
        if track_pk is not None:
            where.append("r.track_pk = ?")
            params.append(track_pk)
        if profile_id is not None:
            where.append("r.profile_id = ?")
            params.append(profile_id)
        if label_type is not None:
            where.append("r.label_type = ?")
            params.append(label_type)
        clause = ("WHERE " + " AND ".join(where)) if where else ""

        rows = conn.execute(
            f"""
            SELECT r.track_pk, r.profile_id, r.label_type, r.confidence,
                   r.notes, r.reference_source, r.created_at,
                   t.canonical_title, t.canonical_artist
            FROM reference_track_labels r
            JOIN tracks t ON t.track_pk = r.track_pk
            {clause}
            ORDER BY r.profile_id,
                     CASE r.label_type
                         WHEN 'positive'  THEN 0
                         WHEN 'near_miss' THEN 1
                         WHEN 'negative'  THEN 2
                     END,
                     t.canonical_artist, t.canonical_title
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def profile_readiness(
    profile_id: str,
    db_path: str | None = None,
) -> dict:
    """
    Report whether a profile has enough references to enable auto-apply.

    Gate (spec v1.4): >= 15 positive AND >= 15 (negative + near_miss),
    with >= 3 distinct artists among the positive exemplars.

    Artist diversity is measured on the positive set because that is what the
    reference_diversity_score guards against — a profile defined by a single
    artist's sound. Distinct artist is resolved via track_artists (primary
    role) when present, falling back to the track's normalised/canonical
    artist string.

    Returns a dict with the raw counts, the per-requirement booleans, and an
    overall `ready` flag.
    """
    conn = get_connection(db_path)
    try:
        if not conn.execute(
            "SELECT 1 FROM tag_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone():
            raise ValueError(f"Profile not found: {profile_id}")

        counts = {
            "positive": 0,
            "negative": 0,
            "near_miss": 0,
        }
        for row in conn.execute(
            """
            SELECT label_type, COUNT(*) AS n
            FROM reference_track_labels
            WHERE profile_id = ?
            GROUP BY label_type
            """,
            (profile_id,),
        ).fetchall():
            counts[row["label_type"]] = row["n"]

        # Distinct artists among positive exemplars. Prefer the first-class
        # artist entity (primary role); fall back to the track's own artist
        # string so the check still works before artist resolution has run.
        positive_artists = conn.execute(
            """
            SELECT COUNT(DISTINCT artist_key) AS n FROM (
                SELECT r.track_pk,
                       LOWER(COALESCE(
                           (SELECT a.name
                              FROM track_artists ta
                              JOIN artists a ON a.artist_id = ta.artist_id
                             WHERE ta.track_pk = r.track_pk AND ta.role = 'primary'
                             ORDER BY ta.position LIMIT 1),
                           t.normalized_artist,
                           t.canonical_artist
                       )) AS artist_key
                FROM reference_track_labels r
                JOIN tracks t ON t.track_pk = r.track_pk
                WHERE r.profile_id = ? AND r.label_type = 'positive'
            )
            """,
            (profile_id,),
        ).fetchone()["n"]

        neg_total = counts["negative"] + counts["near_miss"]
        enough_pos = counts["positive"] >= 15
        enough_neg = neg_total >= 15
        enough_artists = positive_artists >= 3

        return {
            "profile_id": profile_id,
            "positive": counts["positive"],
            "negative": counts["negative"],
            "near_miss": counts["near_miss"],
            "negative_plus_near_miss": neg_total,
            "distinct_positive_artists": positive_artists,
            "needs_positive": max(0, 15 - counts["positive"]),
            "needs_negative_or_near_miss": max(0, 15 - neg_total),
            "needs_artists": max(0, 3 - positive_artists),
            "enough_positive": enough_pos,
            "enough_negative_or_near_miss": enough_neg,
            "enough_artists": enough_artists,
            "ready": enough_pos and enough_neg and enough_artists,
        }
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Automatic derivation — references as a byproduct of manual tagging.
#
# Design: a manual tag is a deliberate genre claim. When a tag maps to a
# profile, the track is a POSITIVE exemplar of that profile; and, because the
# profile contrasts with its opposites, the track is a NEGATIVE exemplar of
# each opposing profile. Rating is preference and plays no part here.
#
# recompute_track_references(track_pk) is the single, idempotent reconcile:
# it derives the desired auto-labels from the track's current manual tags and
# makes the table match — adding what's missing, removing stale auto-labels,
# and never touching hand-made ('manual') or vetoed ('veto') labels.
# ─────────────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Normalise a tag or profile term for matching: lower, unify separators."""
    return " ".join(s.lower().replace("-", " ").replace("_", " ").split())


def _build_tag_index(conn) -> tuple[dict[str, set[str]], dict[str, str]]:
    """
    Build, from tag_profiles, a map of normalised-term -> {profile_id, ...}.

    A profile is matched by its tag_name and by each of its context_terms.
    Terms that are too generic — appearing under 3+ profiles (e.g. "techno") —
    are dropped so a broad tag can't spray positives across the taxonomy.
    Returns (term_to_profiles, profile_layer).
    """
    import json as _json

    raw: dict[str, set[str]] = {}
    layer: dict[str, str] = {}
    for row in conn.execute(
        "SELECT profile_id, tag_name, taxonomy_layer, context_terms_json FROM tag_profiles"
    ).fetchall():
        pid = row["profile_id"]
        layer[pid] = row["taxonomy_layer"]
        terms = {_norm(row["tag_name"])}
        if row["context_terms_json"]:
            try:
                terms.update(_norm(t) for t in _json.loads(row["context_terms_json"]))
            except (ValueError, TypeError):
                pass
        for t in terms:
            if t:
                raw.setdefault(t, set()).add(pid)

    # Drop over-generic terms (shared by 3+ profiles). tag_name terms are always
    # kept, since a profile's own name is never "too generic" for itself.
    names = {_norm(r["tag_name"]) for r in
             conn.execute("SELECT tag_name FROM tag_profiles").fetchall()}
    index = {
        term: pids for term, pids in raw.items()
        if term in names or len(pids) < 3
    }
    return index, layer


def profiles_for_tag(tag: str, db_path: str | None = None) -> list[str]:
    """Which profile_id(s) a manual tag maps to (may be none). Public/inspectable."""
    conn = get_connection(db_path)
    try:
        index, _ = _build_tag_index(conn)
        return sorted(index.get(_norm(tag), set()))
    finally:
        conn.close()


def _manual_tags(conn, track_pk: str) -> list[str]:
    rows = conn.execute(
        "SELECT tag FROM track_tags WHERE track_pk = ? AND tag_type = 'private_manual'",
        (track_pk,),
    ).fetchall()
    return [r["tag"] for r in rows]


def recompute_track_references(
    track_pk: str,
    db_path: str | None = None,
) -> dict:
    """
    Reconcile a track's AUTO-derived reference labels against its manual tags.

    Idempotent. Safe to call on every tag add/remove and during backfill.

    - desired positives = profiles its manual tags map to
    - desired negatives = opposites of those profiles (minus any it's positive
      for, and minus profiles the user vetoed — the polarity guard enforces this)
    - hand-made ('manual') and vetoed ('veto') labels are never added or removed

    Returns {added: [...], removed: [...]} describing the auto-labels changed.
    """
    with db_conn(db_path) as conn:
        if not conn.execute(
            "SELECT 1 FROM tracks WHERE track_pk = ?", (track_pk,)
        ).fetchone():
            raise ValueError(f"Track not found: {track_pk}")

        index, _ = _build_tag_index(conn)
        opp = opposing_profiles_map()

        # Desired auto-labels: {(profile_id, label_type): created_by}
        desired: dict[tuple[str, str], str] = {}
        positives: set[str] = set()
        for tag in _manual_tags(conn, track_pk):
            for pid in index.get(_norm(tag), set()):
                positives.add(pid)
        for pid in positives:
            desired[(pid, "positive")] = CREATED_BY_TAG
        for pid in positives:
            for other in opp.get(pid, set()):
                if other in positives:
                    continue  # can't be a negative of a profile it's positive for
                desired[(other, "negative")] = CREATED_BY_OPPOSING

        # Current AUTO labels on this track (ignore manual/veto entirely).
        current_auto = {
            (r["profile_id"], r["label_type"])
            for r in conn.execute(
                "SELECT profile_id, label_type FROM reference_track_labels "
                "WHERE track_pk = ? AND created_by LIKE 'auto:%'",
                (track_pk,),
            ).fetchall()
        }

        added, removed = [], []

        # Remove auto-labels no longer desired.
        for (pid, lt) in current_auto - set(desired):
            conn.execute(
                "DELETE FROM reference_track_labels "
                "WHERE track_pk = ? AND profile_id = ? AND label_type = ? "
                "AND created_by LIKE 'auto:%'",
                (track_pk, pid, lt),
            )
            removed.append({"profile_id": pid, "label_type": lt})

        # Add desired auto-labels not yet present. add_reference_label is
        # idempotent and polarity-guarded; a guard clash (e.g. user vetoed →
        # near_miss exists, or a hand-made opposite) just means "skip".
        for (pid, lt), created_by in desired.items():
            if (pid, lt) in current_auto:
                continue
            try:
                made = add_reference_label(
                    track_pk, pid, lt,
                    notes=f"auto-derived ({created_by})",
                    reference_source="manual",
                    created_by=created_by,
                    db_path=db_path,
                )
                if made:
                    added.append({"profile_id": pid, "label_type": lt})
            except ValueError:
                pass  # opposing/conflict or unknown profile — leave as-is

        return {"track_pk": track_pk, "added": added, "removed": removed}


def backfill_all_references(db_path: str | None = None) -> dict:
    """
    Run recompute over every track that carries at least one manual tag.

    Idempotent — re-running makes no further changes once settled. Use after
    first enabling derivation, or after editing the opposing map / profiles.
    """
    conn = get_connection(db_path)
    try:
        pks = [
            r["track_pk"] for r in conn.execute(
                "SELECT DISTINCT track_pk FROM track_tags WHERE tag_type = 'private_manual'"
            ).fetchall()
        ]
    finally:
        conn.close()

    tracks_changed, total_added, total_removed = 0, 0, 0
    for pk in pks:
        try:
            res = recompute_track_references(pk, db_path=db_path)
        except ValueError:
            continue
        if res["added"] or res["removed"]:
            tracks_changed += 1
            total_added += len(res["added"])
            total_removed += len(res["removed"])
    return {
        "tracks_scanned": len(pks),
        "tracks_changed": tracks_changed,
        "labels_added": total_added,
        "labels_removed": total_removed,
    }


def track_reference_profiles(track_pk: str, db_path: str | None = None) -> list[dict]:
    """
    The profiles a track is a POSITIVE exemplar for, with veto state — the data
    the UI needs to offer a one-tap 'not a good example' control.

    Returns [{profile_id, tag_name, origin: 'auto'|'manual', vetoed: bool}, ...].
    """
    conn = get_connection(db_path)
    try:
        pos = conn.execute(
            """
            SELECT r.profile_id, p.tag_name, r.created_by
            FROM reference_track_labels r
            JOIN tag_profiles p ON p.profile_id = r.profile_id
            WHERE r.track_pk = ? AND r.label_type = 'positive'
            ORDER BY p.tag_name
            """,
            (track_pk,),
        ).fetchall()
        vetoed = {
            r["profile_id"] for r in conn.execute(
                "SELECT profile_id FROM reference_track_labels "
                "WHERE track_pk = ? AND label_type = 'near_miss' AND created_by = ?",
                (track_pk, CREATED_BY_VETO),
            ).fetchall()
        }
        out = [
            {
                "profile_id": r["profile_id"],
                "tag_name": r["tag_name"],
                "origin": "auto" if str(r["created_by"]).startswith(_AUTO_PREFIX) else "manual",
                "vetoed": False,
            }
            for r in pos
        ]
        for pid in vetoed:
            tag_name = conn.execute(
                "SELECT tag_name FROM tag_profiles WHERE profile_id = ?", (pid,)
            ).fetchone()
            out.append({
                "profile_id": pid,
                "tag_name": tag_name["tag_name"] if tag_name else pid,
                "origin": "veto",
                "vetoed": True,
            })
        return out
    finally:
        conn.close()


def veto_exemplar(
    track_pk: str,
    profile_id: str,
    db_path: str | None = None,
) -> dict:
    """
    'Not a good example.' Demote a track from positive to near_miss for a
    profile: drop the (auto or manual) positive and record a sticky near_miss
    so derivation can't re-promote it. Returns {vetoed: True}.
    """
    with db_conn(db_path) as conn:
        if not conn.execute(
            "SELECT 1 FROM tag_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone():
            raise ValueError(f"Profile not found: {profile_id}")
        conn.execute(
            "DELETE FROM reference_track_labels "
            "WHERE track_pk = ? AND profile_id = ? AND label_type = 'positive'",
            (track_pk, profile_id),
        )
        conn.execute(
            "DELETE FROM reference_track_labels "
            "WHERE track_pk = ? AND profile_id = ? AND label_type = 'near_miss' "
            "AND created_by != ?",
            (track_pk, profile_id, CREATED_BY_VETO),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO reference_track_labels
                (track_pk, profile_id, label_type, confidence, notes,
                 reference_source, created_by)
            VALUES (?, ?, 'near_miss', 1.0, 'user veto: not a good example',
                    'review_queue', ?)
            """,
            (track_pk, profile_id, CREATED_BY_VETO),
        )
    return {"track_pk": track_pk, "profile_id": profile_id, "vetoed": True}


def unveto_exemplar(
    track_pk: str,
    profile_id: str,
    db_path: str | None = None,
) -> dict:
    """Undo a veto: remove the sticky near_miss and let derivation re-promote."""
    with db_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM reference_track_labels "
            "WHERE track_pk = ? AND profile_id = ? AND label_type = 'near_miss' "
            "AND created_by = ?",
            (track_pk, profile_id, CREATED_BY_VETO),
        )
    recompute_track_references(track_pk, db_path=db_path)
    return {"track_pk": track_pk, "profile_id": profile_id, "vetoed": False}
