"""
Flight pack — a self-contained, downloadable offline review page (2026-07-23).

One HTML file with the playlist's tracks + the locked tag vocabulary baked in
as JSON. Opened from file:// with no network: rate (1-4) and add/remove
vocabulary tags; every action is appended to localStorage immediately. Back on
the tailnet, "Sync to server" replays the netted actions against the EXISTING
rating/tag endpoints — nothing new server-side, so all normal semantics
(idempotent ratings, private_manual tags, alias folding) apply unchanged.

Track order = membership rowid: the ingest delete-replaces each playlist's
rows walking the YTM tracklist top-to-bottom, so rowid ascending within a
playlist is YTM play order as of the last complete ingest.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone

from app.db.connection import get_connection

# Same layer precedence the Tags admin uses.
_LAYER_ORDER = ("CASE p.taxonomy_layer WHEN 'functional' THEN 0 "
                "WHEN 'personal' THEN 1 WHEN 'family' THEN 2 "
                "WHEN 'subgenre' THEN 3 ELSE 4 END")


def build_flight_pack(playlist_id: str, api_base: str,
                      db_path: str | None = None) -> tuple[str, str] | None:
    """Return (filename, html) for the playlist, or None if it has no tracks."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT t.track_pk, t.canonical_title, t.canonical_artist,
                      t.album_title, t.duration_ms, t.personal_rating,
                      m.playlist_name
               FROM track_playlist_membership m
               JOIN tracks t ON t.track_pk = m.track_pk
               WHERE m.playlist_id = ?
               ORDER BY m.rowid""",
            (playlist_id,),
        ).fetchall()
        if not rows:
            return None
        pks = [r["track_pk"] for r in rows]
        ph = ",".join("?" * len(pks))
        tag_rows = conn.execute(
            f"""SELECT track_pk, tag, tag_type FROM effective_track_tags
                WHERE track_pk IN ({ph}) ORDER BY type_rank, tag""",
            pks,
        ).fetchall()
        tags_by_pk: dict[str, list] = {}
        for tr in tag_rows:
            tags_by_pk.setdefault(tr["track_pk"], []).append(
                {"tag": tr["tag"], "tag_type": tr["tag_type"]})
        vocab_rows = conn.execute(
            f"""SELECT p.tag_name, p.taxonomy_layer, p.parent_family
                FROM tag_profiles p
                WHERE p.retired_at IS NULL
                ORDER BY {_LAYER_ORDER}, p.sort_order, p.tag_name"""
        ).fetchall()
    finally:
        conn.close()

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    playlist_name = rows[0]["playlist_name"]
    data = {
        "playlist_id": playlist_id,
        "playlist_name": playlist_name,
        "generated_at": generated_at,
        "api_base": api_base.rstrip("/"),
        "tracks": [
            {
                "track_pk": r["track_pk"],
                "position": i + 1,
                "title": r["canonical_title"],
                "artist": r["canonical_artist"],
                "album": r["album_title"],
                "duration_ms": r["duration_ms"],
                "rating": r["personal_rating"],
                "tags": tags_by_pk.get(r["track_pk"], []),
            }
            for i, r in enumerate(rows)
        ],
        "vocab": [
            {"tag": v["tag_name"].lower(), "layer": v["taxonomy_layer"],
             "family": v["parent_family"]}
            for v in vocab_rows
        ],
    }
    # "</" -> "<\/" so titles like "</script>" can't break out of the blob.
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = (_TEMPLATE
            .replace("__TITLE__", html.escape(playlist_name))
            .replace("__DATA__", blob))
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", playlist_name.lower())).strip("-") or "playlist"
    filename = f"flight-pack-{slug[:60]}-{generated_at[:10]}.html"
    return filename, page


# The whole pack page. file:// context: every byte inline, zero external
# references (no fonts, no CDN, no images). Vanilla JS only.
_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flight pack — __TITLE__</title>
<style>
  :root {
    --bg:#0d0d11; --panel:#16161d; --panel2:#1e1e28; --text:#e8e8ee;
    --dim:#8a8a99; --accent:#c8ff00; --accent-dim:#87ab00; --danger:#ff5470;
    --radius:10px;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text);
         font-family:-apple-system,"Segoe UI",Roboto,sans-serif; padding-bottom:60px; }
  header { position:sticky; top:0; z-index:20; background:rgba(13,13,17,.97);
           padding:12px 16px 8px; border-bottom:1px solid #23232e; }
  h1 { font-size:15px; letter-spacing:2px; text-transform:uppercase; color:var(--accent); }
  h1 span { color:var(--dim); font-weight:400; text-transform:none; letter-spacing:0; }
  .meta { color:var(--dim); font-size:12px; margin:2px 0 8px; }
  .meta b { color:var(--text); font-weight:600; }
  .bar { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  .chip { background:var(--panel2); border:1px solid #2b2b38; color:var(--dim);
          padding:5px 12px; border-radius:999px; font-size:12.5px; cursor:pointer; user-select:none; }
  .chip.active { background:var(--accent); color:#111; border-color:var(--accent); font-weight:600; }
  #q { flex:1; min-width:140px; background:var(--panel2); border:1px solid #2b2b38;
       color:var(--text); padding:6px 10px; border-radius:var(--radius); font-size:13px; outline:none; }
  #q:focus { border-color:var(--accent-dim); }
  .btn { background:var(--panel2); border:1px solid #2b2b38; color:var(--text);
         padding:6px 12px; border-radius:8px; font-size:12.5px; cursor:pointer; }
  .btn.primary { background:var(--accent); color:#111; border-color:var(--accent); font-weight:700; }
  .btn:disabled { opacity:.5; cursor:default; }
  #syncpanel { display:none; margin-top:8px; background:var(--panel); border:1px solid #2b2b38;
               border-radius:var(--radius); padding:10px; font-size:12.5px; }
  #syncpanel.open { display:block; }
  #syncpanel label { color:var(--dim); margin-right:6px; }
  #apibase { width:280px; max-width:100%; background:var(--panel2); border:1px solid #2b2b38;
             color:var(--text); padding:5px 8px; border-radius:6px; font-size:12px; outline:none; }
  #syncstatus { margin-top:6px; color:var(--dim); white-space:pre-line; }
  #list { padding:6px 10px; }
  .row { display:flex; gap:10px; align-items:flex-start; padding:8px 8px;
         border-bottom:1px solid #1c1c26; border-left:3px solid transparent; border-radius:4px; }
  .row.focused { background:var(--panel); border-left-color:var(--accent); }
  .row.changed .pos { color:var(--accent); }
  .pos { width:34px; flex-shrink:0; text-align:right; color:var(--dim);
         font-size:12px; padding-top:3px; font-variant-numeric:tabular-nums; }
  .main { flex:1; min-width:0; }
  .t { font-size:14px; }
  .t .a { color:var(--dim); }
  .sub { color:var(--dim); font-size:11.5px; margin-top:1px; }
  .tags { display:flex; flex-wrap:wrap; gap:4px; margin-top:5px; align-items:center; }
  .tag { font-size:11px; border-radius:999px; padding:2px 8px; border:1px solid #2b2b38;
         background:var(--panel2); color:var(--dim); }
  .tag.manual { color:var(--accent); border-color:var(--accent-dim); cursor:pointer; }
  .tag.manual .x { margin-left:4px; color:var(--dim); }
  .tag.manual:hover .x { color:var(--danger); }
  .addtag { font-size:11px; border-radius:999px; padding:2px 9px; border:1px dashed #3a3a48;
            background:none; color:var(--dim); cursor:pointer; }
  .addtag:hover { color:var(--accent); border-color:var(--accent-dim); }
  .stars { display:flex; gap:4px; flex-shrink:0; padding-top:2px; }
  .rb { width:30px; height:30px; border-radius:8px; border:1px solid #2b2b38;
        background:var(--panel2); color:var(--dim); font-size:13px; cursor:pointer; }
  .rb.on { background:var(--accent); color:#111; border-color:var(--accent); font-weight:700; }
  .picker { position:relative; margin-top:6px; }
  .picker input { width:260px; max-width:100%; background:var(--panel2); border:1px solid var(--accent-dim);
                  color:var(--text); padding:6px 9px; border-radius:8px; font-size:13px; outline:none; }
  .plist { position:absolute; z-index:30; top:calc(100% + 4px); left:0; width:320px; max-width:80vw;
           max-height:260px; overflow-y:auto; background:var(--panel); border:1px solid #2b2b38;
           border-radius:10px; box-shadow:0 8px 24px rgba(0,0,0,.55); }
  .pitem { padding:6px 10px; font-size:12.5px; cursor:pointer; display:flex; gap:8px; align-items:baseline; }
  .pitem.sel, .pitem:hover { background:var(--panel2); }
  .pitem .lyr { color:var(--dim); font-size:10.5px; margin-left:auto; }
  .empty { color:var(--dim); padding:30px; text-align:center; }
  kbd { background:var(--panel2); border:1px solid #2b2b38; border-radius:4px;
        padding:0 4px; font-size:10.5px; color:var(--dim); }
  .keys { color:var(--dim); font-size:11px; margin-top:6px; }
</style>
</head>
<body>
<header>
  <h1>Flight pack <span>· __TITLE__</span></h1>
  <div class="meta">Generated <b id="genat"></b> · <b id="ntracks"></b> tracks ·
    <b id="progress"></b> touched this session</div>
  <div class="bar">
    <span class="chip active" data-f="all" onclick="setFilter('all')">All</span>
    <span class="chip" data-f="unrated" onclick="setFilter('unrated')">Unrated</span>
    <span class="chip" data-f="untagged" onclick="setFilter('untagged')">Untagged</span>
    <span class="chip" data-f="changed" onclick="setFilter('changed')">Changed</span>
    <input id="q" placeholder="search title / artist…" oninput="onSearch()">
    <button class="btn" onclick="exportVerdicts()">Export verdicts</button>
    <button class="btn primary" onclick="toggleSync()">Sync to server</button>
  </div>
  <div id="syncpanel">
    <label>API base</label><input id="apibase">
    <button class="btn primary" id="dosync" onclick="doSync()">Sync now</button>
    <div id="syncstatus"></div>
  </div>
  <div class="keys"><kbd>↑</kbd><kbd>↓</kbd> move · <kbd>1</kbd>–<kbd>4</kbd> rate
    (like / really like / love / perfect) · <kbd>0</kbd> clear · <kbd>t</kbd> tag</div>
</header>
<div id="list"></div>
<script id="packdata" type="application/json">__DATA__</script>
<script>
"use strict";
var PACK = JSON.parse(document.getElementById("packdata").textContent);
var KEY = "flightpack:" + PACK.playlist_id + ":" + PACK.generated_at;
var actions = [];
try { actions = JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { actions = []; }
var byPk = {};
PACK.tracks.forEach(function (t) { byPk[t.track_pk] = t; });
var filter = "all", query = "", focusedPk = null, pickerPk = null, pickerSel = 0;

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
function save() { localStorage.setItem(KEY, JSON.stringify(actions)); }
function act(pk, kind, value) {
  actions.push({ track_pk: pk, kind: kind, value: value,
                 at: new Date().toISOString(), synced: false });
  save(); updateRow(pk); updateHeader();
}

// ── Derived state: replay actions over the baked data ──
function ratingOf(pk, syncedOnly) {
  var r = byPk[pk].rating;
  for (var i = 0; i < actions.length; i++) {
    var a = actions[i];
    if (a.track_pk === pk && a.kind === "rate" && (!syncedOnly || a.synced)) r = a.value;
  }
  return r;
}
function bakedManual(pk) {
  var s = {};
  byPk[pk].tags.forEach(function (t) { if (t.tag_type === "private_manual") s[t.tag] = 1; });
  return s;
}
function manualTags(pk, syncedOnly) {
  var s = bakedManual(pk);
  for (var i = 0; i < actions.length; i++) {
    var a = actions[i];
    if (a.track_pk !== pk || (syncedOnly && !a.synced)) continue;
    if (a.kind === "tag_add") s[a.value] = 1;
    else if (a.kind === "tag_remove") delete s[a.value];
  }
  return s;
}
function autoTags(pk) {
  return byPk[pk].tags.filter(function (t) { return t.tag_type !== "private_manual"; });
}
function touched(pk) {
  return actions.some(function (a) { return a.track_pk === pk; });
}

// ── Rendering ──
function fmtDur(ms) {
  if (!ms) return "";
  var s = Math.round(ms / 1000);
  return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
}
var RATE_TITLES = ["", "1 · like", "2 · really like", "3 · love", "4 · perfect"];
function rowHtml(t) {
  var pk = t.track_pk, r = ratingOf(pk), man = Object.keys(manualTags(pk)).sort();
  var chips = man.map(function (tag) {
    return '<span class="tag manual" title="remove tag" onclick="removeTag(\'' + pk + '\',this.dataset.t)" data-t="' + esc(tag) + '">'
      + esc(tag) + '<span class="x">×</span></span>';
  }).join("");
  chips += autoTags(pk).map(function (x) {
    return '<span class="tag" title="' + esc(x.tag_type) + ' (read-only)">' + esc(x.tag) + '</span>';
  }).join("");
  var stars = "";
  for (var n = 1; n <= 4; n++)
    stars += '<button class="rb' + (r === n ? " on" : "") + '" title="' + RATE_TITLES[n]
      + '" onclick="rate(\'' + pk + '\',' + n + ')">' + n + "</button>";
  var picker = pickerPk === pk
    ? '<div class="picker"><input id="tagq" placeholder="add vocabulary tag…" autocomplete="off"><div class="plist" id="plist"></div></div>'
    : "";
  return '<div class="row' + (focusedPk === pk ? " focused" : "") + (touched(pk) ? " changed" : "")
    + '" id="r-' + pk + '" onclick="focusRow(\'' + pk + '\')">'
    + '<div class="pos">' + t.position + "</div>"
    + '<div class="main"><div class="t">' + esc(t.title) + ' <span class="a">· ' + esc(t.artist) + "</span></div>"
    + '<div class="sub">' + esc(t.album || "") + (t.album ? " · " : "") + fmtDur(t.duration_ms) + "</div>"
    + '<div class="tags">' + chips
    + '<button class="addtag" onclick="event.stopPropagation();openPicker(\'' + pk + '\')">+ tag</button></div>'
    + picker + "</div>"
    + '<div class="stars">' + stars + "</div></div>";
}
function visible() {
  return PACK.tracks.filter(function (t) {
    var pk = t.track_pk;
    if (filter === "unrated" && ratingOf(pk) != null) return false;
    if (filter === "untagged" && Object.keys(manualTags(pk)).length) return false;
    if (filter === "changed" && !touched(pk)) return false;
    if (query) {
      var hay = (t.title + " " + t.artist).toLowerCase();
      if (hay.indexOf(query) < 0) return false;
    }
    return true;
  });
}
function render() {
  var vis = visible();
  document.getElementById("list").innerHTML =
    vis.map(rowHtml).join("") || '<div class="empty">No tracks match.</div>';
  if (pickerPk) bindPicker();
  updateHeader();
}
function updateRow(pk) {
  var el = document.getElementById("r-" + pk);
  if (el) { el.outerHTML = rowHtml(byPk[pk]); if (pickerPk === pk) bindPicker(); }
  else render();
}
function updateHeader() {
  var n = {};
  actions.forEach(function (a) { n[a.track_pk] = 1; });
  document.getElementById("progress").textContent = Object.keys(n).length;
}

// ── Interactions ──
function focusRow(pk, scroll) {
  if (focusedPk === pk && !scroll) return; // clicks inside the row (picker input) must not re-render it
  var prev = focusedPk;
  focusedPk = pk;
  if (prev && prev !== pk) { if (pickerPk === prev) closePicker(); updateRow(prev); }
  updateRow(pk);
  if (scroll) {
    var el = document.getElementById("r-" + pk);
    if (el) el.scrollIntoView({ block: "nearest" });
  }
}
function rate(pk, n) {
  event.stopPropagation();
  act(pk, "rate", ratingOf(pk) === n ? null : n);
}
function removeTag(pk, tag) {
  event.stopPropagation();
  act(pk, "tag_remove", tag);
}
function openPicker(pk) {
  focusedPk = pk;
  var prev = pickerPk;
  pickerPk = pk; pickerSel = 0;
  if (prev && prev !== pk) updateRow(prev);
  render();
  var inp = document.getElementById("tagq");
  if (inp) inp.focus();
}
function closePicker() {
  var pk = pickerPk;
  pickerPk = null;
  if (pk) updateRow(pk);
}
function pickerMatches() {
  var q = (document.getElementById("tagq") || {}).value || "";
  q = q.toLowerCase().trim();
  var have = manualTags(pickerPk);
  return PACK.vocab.filter(function (v) {
    return !have[v.tag] && (!q || v.tag.indexOf(q) >= 0 || (v.family || "").indexOf(q) >= 0);
  }).slice(0, 40);
}
function renderPicker() {
  var m = pickerMatches();
  if (pickerSel >= m.length) pickerSel = Math.max(0, m.length - 1);
  document.getElementById("plist").innerHTML = m.map(function (v, i) {
    return '<div class="pitem' + (i === pickerSel ? " sel" : "") + '" onclick="event.stopPropagation();addTag(this.dataset.t)" data-t="' + esc(v.tag) + '">'
      + esc(v.tag) + '<span class="lyr">' + esc(v.layer) + (v.family ? " · " + esc(v.family) : "") + "</span></div>";
  }).join("") || '<div class="pitem" style="color:var(--dim)">no vocabulary match</div>';
}
function bindPicker() {
  var inp = document.getElementById("tagq");
  if (!inp) return;
  renderPicker();
  inp.addEventListener("input", function () { pickerSel = 0; renderPicker(); });
  inp.addEventListener("keydown", function (e) {
    var m = pickerMatches();
    if (e.key === "Escape") { closePicker(); e.stopPropagation(); }
    else if (e.key === "ArrowDown") { pickerSel = Math.min(pickerSel + 1, m.length - 1); renderPicker(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { pickerSel = Math.max(pickerSel - 1, 0); renderPicker(); e.preventDefault(); }
    else if (e.key === "Enter" && m[pickerSel]) { addTag(m[pickerSel].tag); }
    e.stopPropagation();
  });
  inp.focus();
}
function addTag(tag) {
  var pk = pickerPk;
  closePicker();
  if (pk) act(pk, "tag_add", tag);
}
function setFilter(f) {
  filter = f;
  document.querySelectorAll(".chip").forEach(function (c) {
    c.classList.toggle("active", c.dataset.f === f);
  });
  render();
}
function onSearch() {
  query = document.getElementById("q").value.toLowerCase().trim();
  render();
}
document.addEventListener("keydown", function (e) {
  var tgt = e.target;
  if (tgt && (tgt.id === "q" || tgt.id === "tagq" || tgt.id === "apibase")) return;
  var vis = visible(), idx = vis.findIndex(function (t) { return t.track_pk === focusedPk; });
  if (e.key === "ArrowDown") { focusRow(vis[Math.min(idx + 1, vis.length - 1)] ? vis[Math.min(idx + 1, vis.length - 1)].track_pk : focusedPk, true); e.preventDefault(); }
  else if (e.key === "ArrowUp") { focusRow(vis[Math.max(idx - 1, 0)] ? vis[Math.max(idx - 1, 0)].track_pk : focusedPk, true); e.preventDefault(); }
  else if (focusedPk && e.key >= "1" && e.key <= "4") act(focusedPk, "rate", ratingOf(focusedPk) === +e.key ? null : +e.key);
  else if (focusedPk && e.key === "0") { if (ratingOf(focusedPk) != null) act(focusedPk, "rate", null); }
  else if (focusedPk && e.key === "t") { openPicker(focusedPk); e.preventDefault(); }
});

// ── Sync: diff desired state vs server-expected state, replay via the
//    existing endpoints, mark contributing actions synced on success. ──
function computeOps() {
  var ops = [], pks = {}, i, a;
  for (i = 0; i < actions.length; i++) pks[actions[i].track_pk] = 1;
  Object.keys(pks).forEach(function (pk) {
    var want = ratingOf(pk), server = ratingOf(pk, true);
    if (want !== server)
      ops.push({ method: "PUT", path: "/api/tracks/" + encodeURIComponent(pk) + "/rating",
                 body: { rating: want }, pk: pk, kind: "rate" });
    var wantT = manualTags(pk), serverT = manualTags(pk, true);
    Object.keys(wantT).forEach(function (tag) {
      if (!serverT[tag])
        ops.push({ method: "POST", path: "/api/tracks/" + encodeURIComponent(pk) + "/tags",
                   body: { tag: tag }, pk: pk, kind: "tag", tag: tag });
    });
    Object.keys(serverT).forEach(function (tag) {
      if (!wantT[tag])
        ops.push({ method: "DELETE",
                   path: "/api/tracks/" + encodeURIComponent(pk) + "/tags/" + encodeURIComponent(tag),
                   pk: pk, kind: "tag", tag: tag });
    });
  });
  return ops;
}
function markSynced(op) {
  actions.forEach(function (a) {
    if (a.track_pk !== op.pk) return;
    if (op.kind === "rate" && a.kind === "rate") a.synced = true;
    if (op.kind === "tag" && (a.kind === "tag_add" || a.kind === "tag_remove") && a.value === op.tag)
      a.synced = true;
  });
}
function toggleSync() {
  var p = document.getElementById("syncpanel");
  p.classList.toggle("open");
  if (p.classList.contains("open")) {
    var ops = computeOps();
    document.getElementById("syncstatus").textContent =
      ops.length ? ops.length + " change" + (ops.length === 1 ? "" : "s") + " to sync." : "Nothing to sync.";
  }
}
function doSync() {
  var base = document.getElementById("apibase").value.trim().replace(/\/+$/, "");
  var ops = computeOps(), ok = 0, fail = 0, i = 0;
  var btn = document.getElementById("dosync"), st = document.getElementById("syncstatus");
  if (!ops.length) { st.textContent = "Nothing to sync."; return; }
  btn.disabled = true;
  function done() {
    save();
    btn.disabled = false;
    st.textContent = ok + " synced, " + fail + " failed."
      + (fail ? "\nFailed changes are kept locally — fix the API base / connection and sync again." : "");
    render();
  }
  function next() {
    if (i >= ops.length) return done();
    var op = ops[i++];
    st.textContent = "Syncing " + i + "/" + ops.length + "…";
    fetch(base + op.path, {
      method: op.method,
      headers: op.body ? { "Content-Type": "application/json" } : undefined,
      body: op.body ? JSON.stringify(op.body) : undefined,
    }).then(function (resp) {
      // DELETE 404 = the manual tag is already gone server-side; state matches.
      if (resp.ok || (op.method === "DELETE" && resp.status === 404)) { markSynced(op); ok++; }
      else fail++;
      next();
    }).catch(function () { fail++; next(); });
  }
  next();
}
function exportVerdicts() {
  var blob = new Blob([JSON.stringify({
    playlist_id: PACK.playlist_id, playlist_name: PACK.playlist_name,
    generated_at: PACK.generated_at, exported_at: new Date().toISOString(),
    actions: actions,
  }, null, 2)], { type: "application/json" });
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "flight-pack-verdicts-" + PACK.playlist_id + ".json";
  a.click();
  URL.revokeObjectURL(a.href);
}

// ── Boot ──
document.getElementById("genat").textContent = PACK.generated_at.slice(0, 10);
document.getElementById("ntracks").textContent = PACK.tracks.length;
document.getElementById("apibase").value = PACK.api_base;
if (PACK.tracks.length) focusedPk = PACK.tracks[0].track_pk;
render();
</script>
</body>
</html>
"""
