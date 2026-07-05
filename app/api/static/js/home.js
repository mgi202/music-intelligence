// ── Home hub: playlist tree + node table + persistent queue (2026-07-03) ──
// Left: pinned-first playlist tree + smart views. Middle: the selected node's
// tracks, one compact row each. Right: THE queue — up-next and set-crate are
// one merged, server-persisted object.

async function loadHome() {
  loadTree();
  loadServerQueue();
  pollRemote();
  const saved = state.homeNode
    || JSON.parse(localStorage.getItem("homeNode") || "null")
    || { type: "recent" };
  selectNode(saved, false);
}

async function loadTree() {
  try { state.plTree = await api("/api/source-playlists"); }
  catch (e) { state.plTree = []; }
  renderTree();
}

const SMART_NODES = [
  { type: "recent", name: "Recent listens", icon: "ti-history" },
  { type: "gems",   name: "Forgotten gems", icon: "ti-diamond" },
];

function renderTree() {
  const pls = state.plTree || [];
  const pinnedCount = pls.filter(p => p.pinned).length;
  const cap = Math.max(8, pinnedCount);
  const shown = state.treeExpanded ? pls : pls.slice(0, cap);
  $("tree-playlists").innerHTML = shown.map((p, i) => {
    const active = state.homeNode && state.homeNode.type === "playlist"
      && state.homeNode.id === p.playlist_id;
    return `<div class="treeitem ${active ? "active" : ""}" onclick="selectPlaylistByIdx(${i})">
      <button class="pinbtn ${p.pinned ? "pinned" : ""}" onclick="event.stopPropagation();togglePinByIdx(${i})"
        title="${p.pinned ? "unpin from top" : "pin to top"}"><i class="ti ti-pin"></i></button>
      <span class="tname">${esc(p.playlist_name)}</span><span class="cnt">${p.n}</span>
    </div>`;
  }).join("") || '<div class="empty">No playlists ingested yet.</div>';
  const more = $("treemore");
  more.style.display = pls.length > cap ? "" : "none";
  more.textContent = state.treeExpanded ? "fewer playlists ▴" : `all playlists (${pls.length}) ▾`;
  $("tree-smart").innerHTML = SMART_NODES.map(s => {
    const active = state.homeNode && state.homeNode.type === s.type;
    return `<div class="treeitem ${active ? "active" : ""}" onclick="selectNode({type:'${s.type}'})">
      <i class="ti ${s.icon}"></i> <span class="tname">${s.name}</span></div>`;
  }).join("")
  + '<div class="treeitem ghost" title="New releases from watched labels, scored against your profiles — arrives with Stage 1 audio enrichment"><i class="ti ti-radar-2"></i> Discovery · stage 1</div>';
}

function toggleTreeMore() { state.treeExpanded = !state.treeExpanded; renderTree(); }
function selectPlaylistByIdx(i) {
  const p = (state.plTree || [])[i];
  if (p) selectNode({ type: "playlist", id: p.playlist_id, name: p.playlist_name });
}
async function togglePinByIdx(i) {
  const p = (state.plTree || [])[i];
  if (!p) return;
  await api(`/api/source-playlists/${encodeURIComponent(p.playlist_id)}/pin`,
    { method: p.pinned ? "DELETE" : "PUT" });
  await loadTree();
}

async function selectNode(node, save = true) {
  state.homeNode = node;
  if (save && node.type !== "search") localStorage.setItem("homeNode", JSON.stringify(node));
  renderTree();
  let tracks = [], title = "", sub = "";
  try {
    if (node.type === "playlist") {
      const d = await api(`/api/tracks?source_playlist=${encodeURIComponent(node.id)}&limit=500`);
      tracks = d.tracks; title = node.name || "Playlist";
    } else if (node.type === "recent") {
      const d = await api("/api/recent-listens?limit=40");
      tracks = d.listens || []; title = "Recent listens"; sub = "last 40 · newest first";
    } else if (node.type === "gems") {
      const d = await api("/api/forgotten-gems");
      tracks = d.tracks || []; title = "Forgotten gems"; sub = "loved, not heard in 6+ months";
    } else if (node.type === "search") {
      const d = await api(`/api/tracks?q=${encodeURIComponent(node.q)}&limit=200`);
      tracks = d.tracks; title = "Search";
      sub = `“${node.q}” · ${d.total} match${d.total === 1 ? "" : "es"}`;
    }
  } catch (e) { tracks = []; }
  state.homeTracks = tracks;
  state.tracks = tracks;   // playFromList/star toggles read the visible list
  state.midTitle = title; state.midSub = sub;
  renderMid();
}

function fmtDur(ms) {
  const m = Math.round(ms / 60000);
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} m` : `${m} m`;
}

function renderMid() {
  const tracks = state.homeTracks || [];
  $("mid-title").textContent = state.midTitle || "";
  const durMs = tracks.reduce((s, t) => s + (t.duration_ms || 0), 0);
  $("mid-sub").textContent = state.midSub
    || (tracks.length ? `${tracks.length} tracks${durMs ? " · " + fmtDur(durMs) : ""}` : "");
  $("mid-playall").style.display = tracks.some(playableId) ? "" : "none";
  $("mid-rows").innerHTML = tracks.map(midRow).join("")
    || '<div class="empty">Nothing here yet.</div>';
  highlightPlaying(currentPlayingPk());
}

// DJ readout: BPM · Camelot key · energy (E0–E10). Data arrives with Stage 1
// audio enrichment — until then the slot renders as a dim placeholder, so the
// mix-relevant column exists from day one and simply fills in.
function djReadout(t) {
  const has = t.bpm != null || t.camelot_key || t.energy != null;
  if (!has) return '<span class="dj dim" title="BPM · Camelot key · energy — fills in when Stage 1 audio analysis runs">– · – · –</span>';
  const e = t.energy != null ? "E" + Math.round(t.energy * 10) : "–";
  return `<span class="dj" title="BPM · Camelot key · energy">${
    t.bpm != null ? Math.round(t.bpm) : "–"} · ${esc(t.camelot_key || "–")} · ${e}</span>`;
}

function midRow(t) {
  const ext = t.playback_video_id
    ? ' <span style="color:var(--accent);font-size:10px" title="plays a set extended/video version">ext</span>' : "";
  return `<div class="midrow" id="mr-${t.track_pk}">
    <button class="playbtn" ${playableId(t) ? "" : "disabled"} onclick="playFromList('${t.track_pk}')" title="Play from here"><i class="ti ti-player-play"></i></button>
    <span class="m">${esc(t.canonical_title)}${ext} <span class="a">· ${esc(t.canonical_artist)}</span></span>
    ${djReadout(t)}
    ${starButtons(t)}
    <button class="mic" onclick="queueAdd('${t.track_pk}')" title="Add to queue"><i class="ti ti-circle-plus"></i></button>
    <button class="mic" onclick="trackMenu('${t.track_pk}')" title="more"><i class="ti ti-dots"></i></button>
  </div>`;
}

function playAllMid() {
  const first = (state.homeTracks || []).find(playableId);
  if (first) playFromList(first.track_pk);
}

// ── The queue (= set crate). Server-persisted; survives sessions. ──
async function loadServerQueue() {
  try {
    const d = await api("/api/queue");
    state.pq = d.queue || [];
    state.qMirror = d.mirror || null;
  } catch (e) { state.pq = []; }
  renderQueue();
}

function renderQueue() {
  const pq = state.pq || [];
  const durMs = pq.reduce((s, t) => s + (t.duration_ms || 0), 0);
  $("q-meta").textContent = pq.length ? `· ${pq.length}${durMs ? " · " + fmtDur(durMs) : ""}` : "";
  const cur = currentPlayingPk();
  $("q-rows").innerHTML = pq.map((t, i) => `
    <div class="qrow ${t.track_pk === cur ? "current" : ""}" onclick="playQueueFrom(${i})" title="Play from here">
      <div class="qm"><div class="qt">${esc(t.canonical_title)}</div>
        <div class="qa">${esc(t.canonical_artist)}${t.duration_ms ? " · " + fmtTime(t.duration_ms / 1000) : ""}</div></div>
      <button class="qx" onclick="event.stopPropagation();queueRemove(${i})" title="Remove from queue"><i class="ti ti-x"></i></button>
    </div>`).join("");
}

async function queueSave() {
  if (!(state.pq || []).length) { toast("Queue is empty"); return; }
  const def = (state.qMirror && state.qMirror.playlist_name) || "Set Crate";
  const name = prompt(
    "Save queue to a YTM playlist (re-saving updates the same playlist):\n\n" +
    "Heads-up: YTM may swap extended versions to the audio version inside " +
    "native playlists. In-app playback keeps your pinned versions.", def);
  if (name === null) return;
  toast("Saving to YTM…");
  try {
    const r = await api("/api/queue/save", { method: "POST", body: JSON.stringify({ name }) });
    state.qMirror = { playlist_id: r.playlist_id, playlist_name: r.playlist_name };
    toast(`Saved ${r.count} tracks → "${r.playlist_name}"`);
  } catch (e) { toast(`Save failed: ${e.message}`); }
}

function persistQueue() {
  api("/api/queue", {
    method: "PUT",
    body: JSON.stringify({ track_pks: (state.pq || []).map(t => t.track_pk) }),
  }).catch(() => {});
}

async function queueAdd(pk) {
  if ((state.pq || []).some(x => x.track_pk === pk)) { toast("Already in queue"); return; }
  let t = (state.homeTracks || []).find(x => x.track_pk === pk)
       || (state.tracks || []).find(x => x.track_pk === pk);
  if (!t) { try { t = await api(`/api/tracks/${pk}`); } catch (e) { return; } }
  state.pq.push(t); persistQueue(); renderQueue();
  toast("Added to queue");
}
function queueRemove(i) { state.pq.splice(i, 1); persistQueue(); renderQueue(); }
function clearQueue() {
  if (!(state.pq || []).length) return;
  state.pq = []; persistQueue(); renderQueue();
}
function qItem(t) {
  return { videoId: playableId(t), label: `${t.canonical_artist} — ${t.canonical_title}`,
           pk: t.track_pk, ext: !!t.playback_video_id };
}
function playQueueFrom(i) {
  const pq = state.pq || [];
  if (!pq[i]) return;
  const playable = pq.filter(playableId);
  const idx = playable.findIndex(t => t.track_pk === pq[i].track_pk);
  if (idx < 0) { toast("No playable version for this track"); return; }
  _startQueue(playable.map(qItem), idx);
}
function currentPlayingPk() {
  return (state.queue && state.qIndex >= 0 && state.queue[state.qIndex])
    ? state.queue[state.qIndex].pk : null;
}

// ── Remote-listening strip: what the phone (ListenBrainz) is playing, shown
//    only while the local player is idle. Zero height otherwise. ──
async function pollRemote() {
  const el = $("remotestrip");
  try {
    const np = await api("/api/now-playing");
    const localActive = typeof YT !== "undefined" && ytReady && ytPlayer.getPlayerState
      && [YT.PlayerState.PLAYING, YT.PlayerState.PAUSED, YT.PlayerState.BUFFERING]
          .includes(ytPlayer.getPlayerState());
    if (np.playing && !localActive) {
      const p = np.playing, t = p.track;
      el.innerHTML = `<span>📱</span><span style="color:var(--dim)">Listening on phone:</span>
        <b style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t ? t.canonical_title : p.track_name || "")}
          <span style="color:var(--dim);font-weight:400">· ${esc(t ? t.canonical_artist : p.artist_name || "")}</span></b>
        ${t ? starButtons(t) : '<span style="color:var(--dim);font-size:11px">not in library yet</span>'}`;
      el.classList.add("on");
    } else { el.classList.remove("on"); el.innerHTML = ""; }
  } catch (e) { el.classList.remove("on"); el.innerHTML = ""; }
}

let npTimer;
function startHomePolling() {
  stopHomePolling();
  npTimer = setInterval(() => { if (!document.hidden && state.view === "home") pollRemote(); }, 15000);
}
function stopHomePolling() { if (npTimer) clearInterval(npTimer); npTimer = null; }
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopHomePolling();
  else if (state.view === "home") { pollRemote(); startHomePolling(); }
});

let homeSearchTimer;
$("homesearch").addEventListener("input", (e) => {
  clearTimeout(homeSearchTimer);
  homeSearchTimer = setTimeout(() => {
    const q = e.target.value.trim();
    if (q) selectNode({ type: "search", q }, false);
    else selectNode(JSON.parse(localStorage.getItem("homeNode") || "null") || { type: "recent" }, false);
  }, 300);
});

// ── Badges: Review remaining count + Matches pending-pairs count ──
async function refreshBadges() {
  try {
    const q = await api("/api/verdict/queue?limit=1&sort=newest");
    const badge = $("reviewbadge"), n = (q.meta || {}).eligible_total || 0;
    if (n) { badge.textContent = n; badge.style.display = ""; }
    else badge.style.display = "none";
  } catch (e) {}
  try {
    const pairs = await api("/api/dedup");
    const badge = $("matchesbadge");
    if (pairs.length) { badge.textContent = pairs.length; badge.style.display = ""; }
    else badge.style.display = "none";
  } catch (e) {}
}

