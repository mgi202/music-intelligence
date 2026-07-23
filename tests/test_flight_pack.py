"""Flight pack (2026-07-23): one self-contained HTML file per source playlist
for offline review (rate + tag from file://, sync later through the existing
endpoints). Generation correctness + the sync surface the pack depends on."""

from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from app.db.connection import get_connection
from tests.conftest import insert_track


def _client():
    from app.api.server import app
    return TestClient(app)


def _seed(db):
    """Playlist PL1 with three tracks in a deliberate, non-alphabetical order;
    one rated + manually tagged track, one with only a public tag."""
    conn = get_connection(db)
    insert_track(conn, "pk_c", canonical_title="Charlie", canonical_artist="Artist C",
                 album_title="Album C", duration_ms=180000, personal_rating=3)
    insert_track(conn, "pk_a", canonical_title="Alpha", canonical_artist="Artist A")
    insert_track(conn, "pk_b", canonical_title="Bravo", canonical_artist="Artist B")
    insert_track(conn, "pk_x", canonical_title="Not In Playlist")
    for pk in ("pk_c", "pk_a", "pk_b"):  # insertion order = playlist order
        conn.execute(
            """INSERT INTO track_playlist_membership
               (track_pk, playlist_id, playlist_name, source) VALUES (?, 'PL1', 'My Mix', 'ytm')""",
            (pk,),
        )
    conn.executemany(
        "INSERT INTO track_tags (track_pk, tag, tag_type, source) VALUES (?, ?, ?, ?)",
        [("pk_c", "warm-up", "private_manual", "user"),
         ("pk_a", "techno", "public", "lastfm")],
    )
    conn.commit()
    conn.close()


def _pack_data(resp):
    m = re.search(
        r'<script id="packdata" type="application/json">(.*?)</script>',
        resp.text, re.S)
    assert m, "embedded pack JSON blob not found"
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_pack_track_set_order_ratings_and_tags(db):
    _seed(db)
    resp = _client().get("/api/flight-pack?playlist_id=PL1")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert "flight-pack-my-mix-" in resp.headers["content-disposition"]
    data = _pack_data(resp)
    assert data["playlist_id"] == "PL1"
    assert data["playlist_name"] == "My Mix"
    # Playlist (insertion) order, not alphabetical; the outsider excluded.
    assert [t["track_pk"] for t in data["tracks"]] == ["pk_c", "pk_a", "pk_b"]
    assert [t["position"] for t in data["tracks"]] == [1, 2, 3]
    by_pk = {t["track_pk"]: t for t in data["tracks"]}
    assert by_pk["pk_c"]["rating"] == 3
    assert by_pk["pk_a"]["rating"] is None
    # Manual vs non-manual tags distinguishable via tag_type.
    assert {"tag": "warm-up", "tag_type": "private_manual"} in by_pk["pk_c"]["tags"]
    assert {"tag": "techno", "tag_type": "public"} in by_pk["pk_a"]["tags"]


def test_pack_embeds_vocabulary_and_api_base(db):
    _seed(db)
    data = _pack_data(_client().get("/api/flight-pack?playlist_id=PL1"))
    # init_db seeds the locked vocabulary (tag_profiles) — the picker's source.
    assert len(data["vocab"]) > 10
    layers = {v["layer"] for v in data["vocab"]}
    assert {"functional", "personal", "subgenre"} <= layers
    assert all(re.fullmatch(r"[^A-Z]+", v["tag"]) for v in data["vocab"])
    assert data["api_base"].startswith("http://testserver")
    assert data["generated_at"]


def test_pack_is_fully_self_contained(db):
    _seed(db)
    text = _client().get("/api/flight-pack?playlist_id=PL1").text
    # No external resources of any kind — the file must work from file://
    # with the network off.
    assert "<link" not in text
    assert "@import" not in text
    assert "url(" not in text
    assert not re.search(r'src\s*=\s*["\']?https?://', text)
    assert "cdn" not in text.lower()
    # Exactly one document, scripts inline.
    assert text.count("<html") == 1


def test_pack_api_base_honours_forwarded_proto(db):
    """Behind tailscale serve (HTTPS → localhost:8080) the baked sync URL must
    be https, not the plain-http scheme uvicorn sees."""
    _seed(db)
    data = _pack_data(_client().get(
        "/api/flight-pack?playlist_id=PL1",
        headers={"X-Forwarded-Proto": "https"}))
    assert data["api_base"] == "https://testserver"


def test_pack_unknown_playlist_404(db):
    assert _client().get("/api/flight-pack?playlist_id=NOPE").status_code == 404


def test_cors_allows_null_origin_sync(db):
    """file:// pages send Origin: null — the wildcard CORS layer must answer
    both the preflight and the actual request, or pack sync is dead."""
    _seed(db)
    cl = _client()
    pre = cl.options(
        "/api/tracks/pk_a/rating",
        headers={"Origin": "null", "Access-Control-Request-Method": "PUT"},
    )
    assert pre.status_code == 200
    assert pre.headers["access-control-allow-origin"] == "*"
    assert "PUT" in pre.headers["access-control-allow-methods"]
    r = cl.put("/api/tracks/pk_a/rating", json={"rating": 2},
               headers={"Origin": "null"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"


def test_tags_delete_resolves_encoded_slash(db):
    """The pack removes tags via encodeURIComponent(tag) exactly like the SPA —
    a manual tag containing '/' must round-trip through the DELETE route."""
    _seed(db)
    cl = _client()
    assert cl.post("/api/tracks/pk_b/tags", json={"tag": "funk / soul"}).status_code == 200
    r = cl.delete("/api/tracks/pk_b/tags/funk%20%2F%20soul")
    assert r.status_code == 200
    assert r.json() == {"removed": True}
    conn = get_connection(db)
    left = conn.execute(
        "SELECT COUNT(*) AS n FROM track_tags WHERE track_pk='pk_b' AND tag_type='private_manual'"
    ).fetchone()["n"]
    conn.close()
    assert left == 0
