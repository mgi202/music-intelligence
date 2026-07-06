"""One-off sync for the Latin family addition (2026-07-06).

The Latin family and its 9 subgenres are in the locked seed, so reconcile
inserts the family and the 8 brand-new subgenres on deploy. The exception is
`reggaeton`: it already existed on prod as a user-approved profile parented
(wrongly) under `pop`, and reconcile never overwrites user-owned rows — so the
locked seed can't fix it. This script re-points that existing reggaeton row to
the Latin family and syncs it to the seed definition (parent, description,
context terms, layer, sort order). It has no reference labels, so nothing is
lost.

Only touches profile_id='reggaeton'. Idempotent — a second run changes nothing.

Usage:
    python scripts/add_latin_family.py [--db PATH] [--apply]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.connection import db_conn  # noqa: E402
from app.playlists.utility import LOCKED_TAG_PROFILES  # noqa: E402

_PID = "reggaeton"


def sync_reggaeton(db_path: str | None = None, apply: bool = False) -> dict:
    seed = next((p for p in LOCKED_TAG_PROFILES if p["profile_id"] == _PID), None)
    if seed is None:
        raise RuntimeError("reggaeton is not in LOCKED_TAG_PROFILES — aborting")
    terms = seed.get("context_terms") or []
    now = datetime.now(timezone.utc).isoformat()

    with db_conn(db_path) as conn:
        before = conn.execute(
            "SELECT taxonomy_layer, parent_family, context_terms_json "
            "FROM tag_profiles WHERE profile_id = ?", (_PID,)).fetchone()
        if before is None:
            return {"existed": False, "changed": False, "applied": apply}

        target = {
            "taxonomy_layer": seed["taxonomy_layer"],
            "parent_family": seed.get("parent_family"),
            "context_terms_json": json.dumps(terms) if terms else None,
        }
        changed = (
            before["taxonomy_layer"] != target["taxonomy_layer"]
            or before["parent_family"] != target["parent_family"]
            or (before["context_terms_json"] or None) != target["context_terms_json"]
        )
        if apply and changed:
            conn.execute(
                "UPDATE tag_profiles SET taxonomy_layer = ?, parent_family = ?, "
                "description = ?, context_terms_json = ?, sort_order = ?, "
                "updated_at = ? WHERE profile_id = ?",
                (target["taxonomy_layer"], target["parent_family"],
                 seed["description"], target["context_terms_json"],
                 seed.get("sort_order"), now, _PID),
            )

    return {
        "existed": True,
        "changed": changed,
        "from": {"parent": before["parent_family"], "layer": before["taxonomy_layer"]},
        "to": {"parent": target["parent_family"], "layer": target["taxonomy_layer"]},
        "applied": apply,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-parent reggaeton under the Latin family.")
    ap.add_argument("--db", default=None, help="Path to library.db")
    ap.add_argument("--apply", action="store_true", help="Actually update")
    args = ap.parse_args()

    s = sync_reggaeton(args.db, apply=args.apply)
    if not s["existed"]:
        print("reggaeton profile not present — nothing to sync (reconcile seeds it).")
    elif not s["changed"]:
        print("reggaeton already matches the seed (parent=latin) — no change.")
    else:
        verb = "Updated" if args.apply else "Would update"
        print(f"{verb} reggaeton: parent {s['from']['parent']} -> {s['to']['parent']}, "
              f"layer {s['from']['layer']} -> {s['to']['layer']}")
        if not args.apply:
            print("\nDry run — pass --apply to update.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
