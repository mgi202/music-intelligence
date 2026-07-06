"""
FE vocabulary manager — self-service personal + subgenre + era profiles.

The personal layer is Matthias's own (place-anchored listening contexts like
egg-floor-2 or beach belong to him, not to a code deploy), subgenres grow with
the library, and era (production vibe — "sounds like") is his to shape too
(2026-07-06). This module makes those layers manageable from the Tags tab:

  create_profile   — new profile, starts untrained (0/15/15/3). Rows are
                     stamped user_defined=1 (+ origin='user_approved') so
                     reconcile_tag_profiles never drops or overwrites them.
  rename_profile   — the reconcile rename machinery, applied at runtime:
                     migrates reference labels, classification results AND
                     private_manual track_tags onto the new name, folds the
                     old raw tag into the new one, and records a tombstone in
                     tag_profile_renames so a renamed-away LOCKED profile is
                     never re-inserted on the next init_db.
  retire_profile   — SOFT: stamps retired_at. The profile disappears from
                     Review questions/suggestions and readiness (see
                     _build_tag_index / the API filters); existing tags and
                     labels stay untouched. restore_profile undoes it.
  delete_profile   — HARD, only when the profile has zero reference labels
                     and zero manual tags; otherwise refuse (retire instead).

Functional stays code-locked: set order and hotkeys depend on its exact
membership (locked scope decision, 2026-07-05). Era became self-service on
2026-07-06 — the Review card's decade prefill follows FE renames/retires of the
five canonical decade slots (see verdict_queue._resolve_era_prefill), so it
stays intact even when Matthias reshapes the layer.

Error contract (mapped by the API layer):
  ValueError  → bad input / unknown profile (400/404 by message)
  LookupError → conflict: name taken, or delete refused (409)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from app.db.connection import db_conn

MANAGEABLE_LAYERS = ("personal", "subgenre", "era")

# Post-normalisation shape: the charset the locked vocabulary already uses
# (r&b-soul, drum and bass, 00s-sound, hip-house…).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9 &'/.-]*$")
_MAX_NAME_LEN = 40


def _normalize_name(name: str, layer: str) -> str:
    """Lower, trim, collapse whitespace. Personal names are kebab-cased
    (egg floor 2 → egg-floor-2) to match the locked layer's style; subgenres
    keep their spaces (deep house)."""
    n = " ".join((name or "").lower().split())
    if layer == "personal":
        n = n.replace(" ", "-")
    if not n:
        raise ValueError("Profile name is required")
    if len(n) > _MAX_NAME_LEN:
        raise ValueError(f"Profile name too long (max {_MAX_NAME_LEN} chars)")
    if not _NAME_RE.match(n):
        raise ValueError(
            "Profile name may only use letters, digits, spaces, & ' / . -"
        )
    return n


def _require_manageable(row, profile_id: str) -> None:
    if row is None:
        raise ValueError(f"Profile not found: {profile_id}")
    if row["taxonomy_layer"] not in MANAGEABLE_LAYERS:
        raise ValueError(
            f"The {row['taxonomy_layer']} layer is code-locked — only "
            f"personal, subgenre and era profiles are manageable from the FE"
        )


def create_profile(
    name: str,
    layer: str,
    description: str,
    context_terms: list[str] | None = None,
    parent_family: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Create a user-defined personal, subgenre or era profile.

    The description is mandatory — it becomes the chip's hover tooltip in
    Review. New profiles start untrained (0/15/15/3); sort_order appends at
    the end of the layer so existing hotkey positions never shift. New era
    profiles are NOT auto-prefilled from release decade (only the five
    canonical decade slots are) — they're selected by ear like any other chip.
    """
    if layer not in MANAGEABLE_LAYERS:
        raise ValueError(
            f"layer must be one of {MANAGEABLE_LAYERS}, got {layer!r}"
        )
    if not (description or "").strip():
        raise ValueError("A description is required — it becomes the chip's tooltip")
    name = _normalize_name(name, layer)
    now = datetime.now(timezone.utc).isoformat()

    with db_conn(db_path) as conn:
        if conn.execute(
            "SELECT 1 FROM tag_profiles "
            "WHERE LOWER(profile_id) = ? OR LOWER(tag_name) = ?",
            (name, name),
        ).fetchone():
            raise LookupError(f"A profile named “{name}” already exists")

        fam = None
        if layer == "subgenre" and parent_family:
            fam_row = conn.execute(
                "SELECT tag_name FROM tag_profiles "
                "WHERE taxonomy_layer = 'family' AND LOWER(tag_name) = ?",
                (parent_family.lower().strip(),),
            ).fetchone()
            if not fam_row:
                raise ValueError(f"Unknown family: {parent_family}")
            fam = fam_row["tag_name"]

        # Promoted into the vocabulary: any alias/hide fold on this raw tag
        # would hide it from the effective view — clear it (same mechanics as
        # approving a vocab suggestion).
        conn.execute("DELETE FROM tag_vocabulary WHERE tag = ?", (name,))

        next_sort = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM tag_profiles "
            "WHERE taxonomy_layer = ?",
            (layer,),
        ).fetchone()[0]
        terms = [t.strip().lower() for t in (context_terms or []) if t.strip()]
        conn.execute(
            """INSERT INTO tag_profiles
                   (profile_id, tag_name, description, taxonomy_layer,
                    context_terms_json, sort_order, origin, parent_family,
                    user_defined, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'user_approved', ?, 1, ?, ?)""",
            (name, name, description.strip(), layer,
             json.dumps(terms) if terms else None, next_sort, fam, now, now),
        )

    return {
        "profile_id": name, "tag_name": name, "taxonomy_layer": layer,
        "description": description.strip(), "parent_family": fam,
        "sort_order": next_sort, "user_defined": True,
    }


def rename_profile(
    profile_id: str,
    new_name: str,
    db_path: str | None = None,
) -> dict:
    """Rename a personal/subgenre profile, migrating everything that hangs off
    it — reference labels, classification results, private_manual tags — and
    tombstoning the old id so reconcile never resurrects a locked original.
    Labels are never lost: UNIQUE clashes are IGNOREd (the label already
    exists on the target), then stale old rows are deleted.
    """
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tag_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        _require_manageable(row, profile_id)
        new = _normalize_name(new_name, row["taxonomy_layer"])
        old_tag = row["tag_name"]
        if new == profile_id and new == old_tag:
            return {"profile_id": new, "renamed": False}
        clash = conn.execute(
            "SELECT 1 FROM tag_profiles "
            "WHERE (LOWER(profile_id) = ? OR LOWER(tag_name) = ?) "
            "AND profile_id != ?",
            (new, new, profile_id),
        ).fetchone()
        if clash:
            raise LookupError(f"A profile named “{new}” already exists")

        # New row first (children re-point at it), then migrate, then drop old.
        conn.execute("DELETE FROM tag_vocabulary WHERE tag = ?", (new,))
        conn.execute(
            """INSERT INTO tag_profiles
                   (profile_id, tag_name, description, taxonomy_layer,
                    bpm_min, bpm_max, energy_min, energy_max,
                    valence_min, valence_max,
                    positive_prompt, negative_prompt, context_terms_json,
                    sort_order, origin, parent_family, user_defined,
                    retired_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (new, new, row["description"], row["taxonomy_layer"],
             row["bpm_min"], row["bpm_max"], row["energy_min"], row["energy_max"],
             row["valence_min"], row["valence_max"],
             row["positive_prompt"], row["negative_prompt"],
             row["context_terms_json"], row["sort_order"], row["origin"],
             row["parent_family"], row["retired_at"], row["created_at"], now),
        )

        labels_migrated = 0
        for tbl in ("reference_track_labels", "classification_results"):
            moved = conn.execute(
                f"UPDATE OR IGNORE {tbl} SET profile_id = ? WHERE profile_id = ?",
                (new, profile_id),
            )
            if tbl == "reference_track_labels":
                labels_migrated = moved.rowcount
            conn.execute(f"DELETE FROM {tbl} WHERE profile_id = ?", (profile_id,))

        # Manual tags follow the rename (UNIQUE(track_pk, tag, source) clashes
        # mean the track already carries the new tag — drop the stale old row).
        tags_migrated = conn.execute(
            "UPDATE OR IGNORE track_tags SET tag = ? "
            "WHERE LOWER(tag) = ? AND tag_type = 'private_manual'",
            (new, old_tag.lower()),
        ).rowcount
        conn.execute(
            "DELETE FROM track_tags "
            "WHERE LOWER(tag) = ? AND tag_type = 'private_manual'",
            (old_tag.lower(),),
        )

        conn.execute("DELETE FROM tag_profiles WHERE profile_id = ?", (profile_id,))

        # Keep the raw-tag world coherent: public tags spelled the old way
        # fold into the new name, and anything that aliased TO the old name
        # is re-pointed (no chains for the view to half-apply).
        conn.execute(
            "UPDATE tag_vocabulary SET alias_to = ?, updated_at = ? "
            "WHERE LOWER(alias_to) = ?",
            (new, now, old_tag.lower()),
        )
        conn.execute(
            """INSERT INTO tag_vocabulary (tag, hidden, alias_to, updated_at)
               VALUES (?, 0, ?, ?)
               ON CONFLICT(tag) DO UPDATE SET
                   hidden = 0, alias_to = excluded.alias_to,
                   updated_at = excluded.updated_at""",
            (old_tag.lower(), new, now),
        )

        # Tombstone: reconcile must never re-insert a locked id renamed away.
        # Flatten prior tombstones that pointed at the old id first.
        conn.execute(
            "UPDATE tag_profile_renames SET new_profile_id = ? "
            "WHERE new_profile_id = ?",
            (new, profile_id),
        )
        conn.execute(
            """INSERT INTO tag_profile_renames
                   (old_profile_id, new_profile_id, renamed_at)
               VALUES (?, ?, ?)
               ON CONFLICT(old_profile_id) DO UPDATE SET
                   new_profile_id = excluded.new_profile_id,
                   renamed_at = excluded.renamed_at""",
            (profile_id, new, now),
        )

    return {
        "profile_id": new, "renamed": True, "from": profile_id,
        "labels_migrated": labels_migrated, "tags_migrated": tags_migrated,
    }


def retire_profile(profile_id: str, db_path: str | None = None) -> dict:
    """Soft retire — hidden from Review questions/suggestions and readiness;
    tags and labels stay. Idempotent."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT taxonomy_layer FROM tag_profiles WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        _require_manageable(row, profile_id)
        conn.execute(
            "UPDATE tag_profiles SET retired_at = COALESCE(retired_at, ?), "
            "updated_at = ? WHERE profile_id = ?",
            (now, now, profile_id),
        )
    return {"profile_id": profile_id, "retired": True}


def restore_profile(profile_id: str, db_path: str | None = None) -> dict:
    """Un-retire — the profile returns to Review and readiness immediately."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT taxonomy_layer FROM tag_profiles WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        _require_manageable(row, profile_id)
        conn.execute(
            "UPDATE tag_profiles SET retired_at = NULL, updated_at = ? "
            "WHERE profile_id = ?",
            (now, profile_id),
        )
    return {"profile_id": profile_id, "retired": False}


def delete_profile(profile_id: str, db_path: str | None = None) -> dict:
    """Hard delete — ONLY when the profile carries zero reference labels and
    zero manual tags. Anything with history should be retired instead."""
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT taxonomy_layer, tag_name FROM tag_profiles WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        _require_manageable(row, profile_id)
        n_labels = conn.execute(
            "SELECT COUNT(*) FROM reference_track_labels WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()[0]
        n_tags = conn.execute(
            "SELECT COUNT(*) FROM track_tags "
            "WHERE LOWER(tag) = ? AND tag_type = 'private_manual'",
            (row["tag_name"].lower(),),
        ).fetchone()[0]
        if n_labels or n_tags:
            raise LookupError(
                f"“{profile_id}” has {n_labels} reference label(s) and "
                f"{n_tags} manual tag(s) — retire it instead of deleting"
            )
        conn.execute("DELETE FROM tag_profiles WHERE profile_id = ?", (profile_id,))
    return {"profile_id": profile_id, "deleted": True}
