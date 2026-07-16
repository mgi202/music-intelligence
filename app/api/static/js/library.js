// ── Library ──────────────────────────────

async function loadTracks(append = false) {
  if (!append) state.offset = 0;
  const p = new URLSearchParams({ limit: state.limit, offset: state.offset, sort: state.sort });
  if (state.q) p.set("q", state.q);
  if (state.rating !== "") p.set("rating", state.rating);
  if (state.untagged) p.set("untagged", "true");
  if (state.dismissed) p.set("dismissed", "true");
  if (state.tags.length) { p.set("tags", state.tags.join(",")); p.set("tag_mode", state.tagMode); }
  if (state.sourcePlaylist) p.set("source_playlist", state.sourcePlaylist);
  if (state.pendingVersions) p.set("pending_versions", "true");
  const data = await api(`/api/tracks?${p}`);
  state.total = data.total;
  state.tracks = append ? state.tracks.concat(data.tracks) : data.tracks;
  renderLibrary();
  renderSumBar();
}

function starButtons(t) {
  const r = t.personal_rating || 0;
  let html = '<div class="stars">';
  for (let i = 1; i <= 4; i++) {
    html += `<button class="${i <= r ? "lit" : ""}" onclick="rate('${t.track_pk}', ${i})">★</button>`;
  }
  return html + "</div>";
}

function trackCard(t, opts = {}) {
  const flags = [];
  if (t.blocked_from_playlists) flags.push('<span class="flag">⛔ blocked</span>');
  if (t.do_not_recommend) flags.push('<span class="flag">🚫 no rec</span>');
  const tags = (t.tags || []).map(g => {
    const manual = g.tag_type === "private_manual";
    const click = manual
      ? `onclick="removeTag('${t.track_pk}','${esc(g.tag)}')" title="manual tag — tap to remove"`
      : `onclick="rejectTag('${t.track_pk}','${esc(g.tag)}')" title="auto tag — tap to reject for this track"`;
    return `<span class="tag ${manual ? "manual" : "auto"}" ${click}>${esc(g.tag)}</span>`;
  }).join("");
  const plchips = (t.playlists || []).map(p =>
    `<span class="plchip">
       <span class="plname" title="filter to this playlist" onclick="filterPlaylist('${esc(p.playlist_id)}')">${esc(p.playlist_name)}</span>
       <button class="plx" title="remove from this playlist on YTM" onclick="removeFromPlaylist('${t.track_pk}','${esc(p.playlist_id)}')">×</button>
     </span>`).join("");
  return `
    <div class="track" id="tr-${t.track_pk}">
      <button class="playbtn" ${(t.ytm_track_id || t.playback_video_id) ? "" : "disabled"} onclick="playFromList('${t.track_pk}')" title="Play from here">▶</button>
      <div class="meta">
        <div class="title">${esc(t.canonical_title)}${t.playback_video_id ? ' <span class="vbad hi" title="plays a set extended/video version">· ext</span>' : ""}${
          t.pending_version_count ? ` <span class="pendbadge" title="playback versions to review" onclick="versionDialog('${t.track_pk}')">⇱ ${t.pending_version_count}</span>` : ""}</div>
        <div class="artist">${esc(t.canonical_artist)}${t.album_title ? " · " + esc(t.album_title) : ""}</div>
      </div>
      <div class="tmid">
        ${flags.join("")}
        ${tags}
        <button class="addtag" onclick="addTag('${t.track_pk}')">+ tag</button>
        ${plchips}
      </div>
      <div class="tctl">
        ${starButtons(t)}
        ${t.inbox_dismissed_at ? `<button class="addtag" onclick="restoreDismissed('${t.track_pk}')" title="clear the 'not for me' verdict — the track re-enters Review">restore</button>` : ""}
        <button class="qadd" onclick="queueAdd('${t.track_pk}')" title="Add to queue">⊕</button>
        <button class="more" onclick="trackMenu('${t.track_pk}')" title="more">⋯</button>
      </div>
    </div>`;
}

// R11: clear a "Not for me" verdict from the Library's Dismissed filter.
async function restoreDismissed(pk) {
  try { await api(`/api/tracks/${pk}/undismiss`, { method: "POST", body: "{}" }); }
  catch (e) { toast("Failed — try again"); return; }
  toast("Restored — back in Review");
  loadTracks();
}

function renderLibrary() {
  $("trackcount").textContent = state.total ? `· ${state.total}` : "";
  const el = $("library");
  if (!state.tracks.length) { el.innerHTML = '<div class="empty">No tracks match.</div>'; return; }
  el.innerHTML = state.tracks.map(t => trackCard(t)).join("")
    + (state.tracks.length < state.total
        ? `<button class="loadmore" onclick="more()">Load more (${state.total - state.tracks.length} remaining)</button>` : "");
}

// Locate a track by pk across the pools that render cards. Review-queue tracks
// use short field names, so map them to the canonical track shape.
function findTrack(pk) {
  const t = (state.tracks || []).find(x => x.track_pk === pk);
  if (t) return t;
  const v = (state.vq || []).find(x => x.pk === pk);
  if (v) return {
    track_pk: v.pk, canonical_title: v.title, canonical_artist: v.artist,
    album_title: v.album, ytm_track_id: v.video_id, playback_video_id: v.playback_video_id,
    personal_rating: v.personal_rating, blocked_from_playlists: v.blocked_from_playlists,
    do_not_recommend: v.do_not_recommend, tags: v.tags || [],
    playlists: v.playlists || [], reference_profiles: [],
  };
  return {};
}

// ── ⋯ menu: block / don't-recommend / reference exemplars / wrong-match ──
async function trackMenu(pk) {
  const t = findTrack(pk);
  const bg = document.createElement("div");
  bg.className = "menu";
  bg.onclick = (ev) => { if (ev.target === bg) bg.remove(); };

  // Reference-exemplar rows: one per profile this track is (or was) an example
  // of. Tap to veto ("not a good example") or restore. Derived from your tags —
  // nothing to set up. Hidden entirely if the track maps to no profile.
  const refs = t.reference_profiles || [];
  const refRows = refs.map(r => r.vetoed
    ? `<button onclick="unvetoReference('${pk}','${esc(r.profile_id)}');this.closest('.menu').remove()">
         ↩︎ Restore as example of “${esc(r.tag_name)}”</button>`
    : `<button onclick="vetoReference('${pk}','${esc(r.profile_id)}');this.closest('.menu').remove()">
         ✗ Not a good example of “${esc(r.tag_name)}”</button>`
  ).join("");
  const refSection = refRows
    ? `<div class="menuhint">Reference examples (from your tags)</div>${refRows}<div class="menusep"></div>`
    : "";

  bg.innerHTML = `<div class="sheet">
    ${refSection}
    <button onclick="versionDialog('${pk}');this.closest('.menu').remove()">
      🔎 ${t.playback_video_id ? "Change playback version" : "Find extended / video version"}${
        t.pending_version_count ? ` <span class="vbad mid">${t.pending_version_count} to review</span>` : ""}</button>
    <div class="menusep"></div>
    <button onclick="setFlag('${pk}','blocked_from_playlists',${t.blocked_from_playlists ? 0 : 1});this.closest('.menu').remove()">
      ${t.blocked_from_playlists ? "Unblock" : "Block from playlists"}</button>
    <button onclick="setFlag('${pk}','do_not_recommend',${t.do_not_recommend ? 0 : 1});this.closest('.menu').remove()">
      ${t.do_not_recommend ? "Allow recommending" : "Don't recommend"}</button>
    ${(t.playlists || []).map(p =>
      `<button onclick="removeFromPlaylist('${pk}','${esc(p.playlist_id)}');this.closest('.menu').remove()">
         Remove from “${esc(p.playlist_name)}”</button>`).join("")}
    <button onclick="removeFromAllPlaylists('${pk}');this.closest('.menu').remove()">Remove from all my YTM playlists</button>
    <button class="danger" onclick="wrongMatch('${pk}');this.closest('.menu').remove()">Wrong match (quarantine)</button>
    <button onclick="this.closest('.menu').remove()">Cancel</button>
  </div>`;
  document.body.appendChild(bg);
}

// Reload whichever list is on screen (shared by the version flow).
function reloadCurrent() {
  if (state.view === "library") loadTracks();
  else if (state.view === "home") loadHome();
}
function fmtDurMs(ms) {
  const s = Math.round((ms || 0) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// Two-part dialog: auto-search YTM for extended/club versions (scored, one-click
// apply/reject with the evidence shown), plus the manual paste-a-link fallback.
async function versionDialog(pk) {
  const t = findTrack(pk);
  const bg = document.createElement("div");
  bg.className = "menu";
  bg.onclick = (ev) => { if (ev.target === bg) bg.remove(); };
  bg.innerHTML = `<div class="sheet verdlg">
    <div class="menuhint">Playback version — ${esc(t.canonical_title || "")}</div>
    <button class="verbtn" id="ver-search">🔎 Search YTM for the extended version</button>
    <button class="verbtn" id="ver-vsearch">🎬 Search for the official video</button>
    <div id="ver-results" class="verresults"></div>
    <div class="menusep"></div>
    <div class="menuhint">Or paste a link / videoId</div>
    <div class="verpaste">
      <input id="ver-paste" type="text" placeholder="YTM link or videoId — leave blank to clear"
             value="${t.playback_video_id ? `https://music.youtube.com/watch?v=${t.playback_video_id}` : ""}">
      <button id="ver-save">Save</button>
    </div>
    <button onclick="this.closest('.menu').remove()" style="margin-top:6px">Close</button>
  </div>`;
  document.body.appendChild(bg);
  state.verCache = { pk, t, cands: [] };

  bg.querySelector("#ver-search").onclick = async () => {
    const res = bg.querySelector("#ver-results");
    res.innerHTML = '<div class="verhint">Searching YouTube Music…</div>';
    try {
      const data = await api(`/api/tracks/${pk}/version-candidates/search`, { method: "POST", body: "{}" });
      state.verCache = { pk, t, cands: data.candidates };
      renderVersionCandidates(res);
      reloadCurrent();  // an auto-apply may have set the version already
    } catch (e) { res.innerHTML = `<div class="verhint">Search failed: ${esc(e.message)}</div>`; }
  };
  // Official-video discovery: quality-checks YTM's audio↔video pairing and
  // finds videos living on remix/feat. variant tracks. Approving feeds the
  // prefer-videos toggle (official_video_id), not the pinned version.
  bg.querySelector("#ver-vsearch").onclick = async () => {
    const res = bg.querySelector("#ver-results");
    res.innerHTML = '<div class="verhint">Searching for the official video…</div>';
    try {
      const data = await api(`/api/tracks/${pk}/video-candidates/search`, { method: "POST", body: "{}" });
      state.verCache = { pk, t, cands: data.candidates };
      renderVersionCandidates(res);
      reloadCurrent();
    } catch (e) { res.innerHTML = `<div class="verhint">Search failed: ${esc(e.message)}</div>`; }
  };
  bg.querySelector("#ver-save").onclick = async () => {
    const v = bg.querySelector("#ver-paste").value.trim();
    try {
      const r = await api(`/api/tracks/${pk}/playback-version`, { method: "PUT", body: JSON.stringify({ video: v }) });
      toast(r.playback_video_id ? "Playback version set ✓" : "Reverted to normal version");
      bg.remove();
      reloadCurrent();
    } catch (e) { toast("Couldn't read that link — try the watch URL"); }
  };
}

function renderVersionCandidates(el) {
  const { t, cands } = state.verCache;
  if (!cands.length) { el.innerHTML = '<div class="verhint">No candidates found. Try the paste box below.</div>'; return; }
  const canon = t.duration_ms ? fmtDurMs(t.duration_ms) : "?";
  el.innerHTML = cands.map(c => {
    const cd = c.candidate_duration_ms ? fmtDurMs(c.candidate_duration_ms) : "?";
    const conf = Math.round((c.confidence || 0) * 100);
    const applied = (c.status === "auto_applied" || c.status === "approved");
    const badge = c.veto_reason
      ? `<span class="vbad veto" title="disqualified">⚠ ${esc(c.veto_reason)}</span>`
      : `<span class="vbad ${conf >= 92 ? "hi" : conf >= 60 ? "mid" : "lo"}">${conf}%</span>`;
    const ev = `title ${Math.round(c.title_similarity * 100)}% · artist ${Math.round(c.artist_similarity * 100)}%`
      + ` · dur ${Math.round(c.duration_score * 100)}% · uploader ${c.uploader_score}`
      + (c.keyword_score ? ` · keyword ${c.keyword_score}` : "");
    const label = `${(t.canonical_artist || "")} — ${(t.canonical_title || "")}`;
    return `<div class="vrow ${c.veto_reason ? "isveto" : ""}">
      <div class="vmain">
        <div class="vtitle">${c.kind === "video" ? '<span class="vbad mid" title="official-video candidate — approving sets the prefer-videos version">🎬</span> ' : ""}${esc(c.candidate_title || "?")} ${badge}${applied ? ' <span class="vbad hi">✓ in use</span>' : ""}${c.status === "superseded" ? ' <span class="vbad lo">superseded</span>' : ""}</div>
        <div class="vsub">${esc(c.candidate_channel || "")} · ${cd} vs ${canon}${c.result_type ? " · " + esc(c.result_type) : ""}</div>
        <div class="vev">${ev}</div>
      </div>
      <div class="vact">
        <button title="preview in player" onclick="previewVersion('${esc(c.video_id)}','${esc(label)}','${esc(t.track_pk)}')">▶</button>
        ${c.status === "pending" ? `<button title="use this version" onclick="approveVersion(${c.candidate_id})">Use</button>` : ""}
        ${c.status === "pending" ? `<button class="danger" title="reject — never suggest again" onclick="rejectVersion(${c.candidate_id})">✕</button>` : ""}
      </div>
    </div>`;
  }).join("");
}

function previewVersion(videoId, label, pk) {
  play(videoId, label, pk, true);  // reuse the IFrame player; ext=true → "· extended"
}

async function approveVersion(candidateId) {
  let applied;
  try {
    applied = await api(`/api/version-candidates/${candidateId}/approve`, { method: "POST", body: "{}" });
  } catch (e) { toast(e.message || "Couldn't apply"); return; }
  toast(applied.kind === "video" ? "Official video set ✓" : "Playback version set ✓");
  // Reflect locally: this one approved, the rest OF THE SAME KIND superseded.
  const c = state.verCache.cands.find(x => x.candidate_id === candidateId);
  if (c) {
    c.status = "approved";
    state.verCache.cands.forEach(x => { if (x.candidate_id !== candidateId && x.status === "pending" && x.kind === c.kind) x.status = "superseded"; });
  }
  const el = document.querySelector("#ver-results");
  if (el) renderVersionCandidates(el);
  reloadCurrent();
}

async function rejectVersion(candidateId) {
  try {
    await api(`/api/version-candidates/${candidateId}/reject`, { method: "POST", body: "{}" });
  } catch (e) { toast(e.message || "Couldn't reject"); return; }
  toast("Rejected — won't suggest again");
  const c = state.verCache.cands.find(x => x.candidate_id === candidateId);
  if (c) c.status = "rejected";
  const el = document.querySelector("#ver-results");
  if (el) renderVersionCandidates(el);
  reloadCurrent();
}

async function vetoReference(pk, profileId) {
  await api(`/api/tracks/${pk}/reference/${encodeURIComponent(profileId)}/veto`, { method: "POST", body: "{}" });
  toast("Won't use as an example");
  if (state.view === "library") loadTracks();
}

async function unvetoReference(pk, profileId) {
  await api(`/api/tracks/${pk}/reference/${encodeURIComponent(profileId)}/veto`, { method: "DELETE" });
  toast("Restored as an example");
  if (state.view === "library") loadTracks();
}

// Retire the current review card when an action just made it ineligible for
// both lenses (block, quarantine) — mirrors the old inbox's list reload.
function vqRetireIfCurrent(pk) {
  const t = vqCard();
  if (state.view === "review" && t && t.pk === pk) {
    if (state.vqMeta) state.vqMeta.eligible_total = Math.max(0, (state.vqMeta.eligible_total || 1) - 1);
    state.vqLastAdv = "";
    state.vqIndex++;
    renderVerdict();
  }
}

async function setFlag(pk, flag, value) {
  await api(`/api/tracks/${pk}/flags`, { method: "PUT", body: JSON.stringify({ [flag]: !!value }) });
  toast(flag === "blocked_from_playlists" ? (value ? "Blocked" : "Unblocked") : (value ? "Won't recommend" : "Recommending"));
  if (state.view === "library") loadTracks();
  if (value) vqRetireIfCurrent(pk);
}

async function wrongMatch(pk) {
  if (!confirm("Mark as wrong match? It will be quarantined and removed from playlists.")) return;
  await api(`/api/tracks/${pk}/wrong-match`, { method: "POST", body: "{}" });
  toast("Quarantined → Matches");
  if (state.view === "library") loadTracks();
  vqRetireIfCurrent(pk);
}

function more() { state.offset += state.limit; loadTracks(true); }

async function rate(pk, rating) {
  const t = (state.tracks || []).find(x => x.track_pk === pk);
  const cur = t ? (t.personal_rating || 0) : 0;
  const newRating = (cur === rating) ? null : rating;  // tap same star = clear
  await api(`/api/tracks/${pk}/rating`, { method: "PUT", body: JSON.stringify({ rating: newRating }) });
  if (t) t.personal_rating = newRating;
  const pq = (state.pq || []).find(x => x.track_pk === pk);
  if (pq) pq.personal_rating = newRating;
  toast(newRating ? ["", "Like", "Really like", "Love", "Perfect ✦"][newRating] : "Rating cleared");
  if (state.view === "library") renderLibrary();
  else if (state.view === "home") { renderMid(); pollRemote(); }
  if (currentPlayingPk() === pk) updateHeroMeta(pk);   // hero stars stay honest
  refreshBadges();   // rating retires a track from the Review "Newest" lens
}

// Layer display order + labels for the tap-palette. Family leads (Matthias,
// 5 Jul): pick the family first, subgenres filter to it.
const LAYER_ORDER = ["family", "subgenre", "functional", "personal", "era"];
const LAYER_LABEL = { subgenre: "Subgenre", functional: "Function (set role)", personal: "Personal", family: "Family", era: "Era (sounds like)" };

async function addTag(pk) {
  const t = findTrack(pk);
  return openTagModal(pk, t, {
    onClose: () => {
      refreshActiveView(); loadTagChips();
      if (currentPlayingPk() === pk) updateHeroMeta(pk);
    },
  });
}

// Shared tag-modal core — Library's + tag, the hero player's + tag and Review's
// + tag all call this so the family-gated profile palette and free-text escape
// hatch stay in one place. The profile vocabulary = your tap-palette.
// Recognition, not recall — plus a search box (Matthias, 15 Jul) so 60+
// subgenres never need cmd+F again. Search filters every layer live and cuts
// across the family gate; Enter toggles the first hit, or adds the query as a
// free-text tag when nothing matches.
// `t` is the live track object (its `.tags` array is mutated in place). Hooks:
//   opts.onChange(tag, added, profile|null) — after each successful apply/remove
//   opts.onClose()                          — once on dismiss, only if changed
async function openTagModal(pk, t, opts = {}) {
  if (!state.allTags.length) { try { state.allTags = await api("/api/tags"); } catch (e) {} }
  if (!state.profileList) {
    try { state.profileList = (await api("/api/reference/profiles")).profiles || []; }
    catch (e) { state.profileList = []; }
  }
  // Self-hydrate (15 Jul fix): callers pass differently-shaped track objects —
  // the hero player's findTrack() often lacks tags, so the family gate saw "no
  // family" and hid subgenres there while Review showed them. Fetch the
  // effective tags ourselves; never trust the caller for gating.
  try {
    const fresh = await api(`/api/tracks/${encodeURIComponent(pk)}`);
    if (fresh && Array.isArray(fresh.tags)) t.tags = fresh.tags;
  } catch (e) {}
  const profileByName = {};
  state.profileList.forEach(p => { profileByName[p.tag_name.toLowerCase()] = p; });
  const manual = new Set((t.tags || [])
    .filter(g => g.tag_type === "private_manual").map(g => g.tag.toLowerCase()));

  // Keep the live track's tag list in sync so the caller re-renders correctly
  // and reopening the modal reflects reality, then fire the caller's hook.
  let changed = false;
  const notifyChange = (tag, added) => {
    changed = true;
    const key = tag.toLowerCase();
    t.tags = (t.tags || []).filter(g =>
      !(g.tag_type === "private_manual" && (g.tag || "").toLowerCase() === key));
    if (added) t.tags.push({ tag: key, tag_type: "private_manual" });
    if (opts.onChange) opts.onChange(tag, added, profileByName[key] || null);
  };

  const bg = document.createElement("div");
  bg.className = "modal-bg";
  const close = () => {
    document.removeEventListener("keydown", escClose, true);
    bg.remove();
    if (changed && opts.onClose) opts.onClose();
  };
  const escClose = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  document.addEventListener("keydown", escClose, true);
  bg.onclick = (ev) => { if (ev.target === bg) close(); };

  bg.innerHTML = `<div class="modal tag-modal">
    <h2>Tag track</h2>
    <input id="tag-search" placeholder="Search tags — Enter toggles the first match, or adds free-text"
           autocomplete="off" autocapitalize="none" spellcheck="false">
    <div id="tag-selected" class="tag-selected"></div>
    <div id="palette"></div>
    <div class="btnrow">
      <button class="primary" id="tag-done">Done</button>
    </div>
  </div>`;
  document.body.appendChild(bg);

  // ── Palette ──────────────────────────────────────────────────────────────
  // Subgenres are family-gated (Matthias, 5 Jul): a hip hop track never gets
  // offered deep house. Active families = the track's effective family tags
  // (any type — public counts) plus families tapped in this modal. No family
  // yet → subgenres hide behind a pick-a-family hint. "show all styles" is the
  // escape hatch for genuine cross-family hybrids (hip house etc.).
  const palette = bg.querySelector("#palette");
  // Layers you can grow from here without a trip to the Tags tab (2026-07-06).
  const MANAGED_LAYERS = ["personal", "subgenre", "era"];
  let byLayer = {};
  let familyNames = new Set();
  const rebuildByLayer = () => {
    byLayer = {};
    state.profileList.forEach(p => { (byLayer[p.taxonomy_layer] ||= []).push(p); });
    familyNames = new Set((byLayer.family || []).map(p => p.tag_name.toLowerCase()));
  };
  rebuildByLayer();
  const trackFams = new Set((t.tags || [])
    .map(g => (g.tag || "").toLowerCase()).filter(x => familyNames.has(x)));
  let showAllSubs = false;

  // Search state (15 Jul): a non-empty query filters chips in EVERY layer by
  // substring and cuts across the family gate — searching is explicit intent,
  // so gating out of sight would just feel broken. Enter toggles the first
  // visible match; no vocab match → Enter adds the query as free-text.
  let query = "";
  const selectedBox = bg.querySelector("#tag-selected");
  const renderSelected = () => {
    selectedBox.innerHTML = manual.size
      ? [...manual].sort().map(k =>
          `<button class="chip active" data-tag="${esc(k)}" title="tap to remove">✓ ${esc(k)}</button>`).join("")
      : '<span class="hint">No manual tags yet — tap chips below or search.</span>';
  };

  const renderPalette = () => {
    renderSelected();
    const q = query.trim().toLowerCase();
    const activeFams = new Set([...trackFams, ...[...manual].filter(x => familyNames.has(x))]);
    let firstHit = null;
    const sections = LAYER_ORDER.filter(l => byLayer[l]).map(layer => {
      // family/subgenre read A–Z; functional/personal/era keep the API's
      // tag_profiles.sort_order so the chips line up with the Review card
      // (set order, decade order) instead of alphabetical (16 Jul).
      let items = layer === "family" || layer === "subgenre"
        ? [...byLayer[layer]].sort((a, b) => a.tag_name.localeCompare(b.tag_name))
        : [...byLayer[layer]];
      let extra = "";
      if (q) {
        items = items.filter(p => p.tag_name.toLowerCase().includes(q));
        if (!items.length) return "";               // hide empty layers while searching
      } else if (layer === "subgenre") {
        if (!activeFams.size && !showAllSubs) {
          return `<div class="tag-sec"><div class="playlbl">${LAYER_LABEL[layer]}</div>
            <div class="hint">Pick a family above to see its styles —
              <a href="#" id="subs-showall" style="color:var(--accent)">or show all styles</a></div></div>`;
        }
        if (!showAllSubs) {
          items = items.filter(p => manual.has(p.tag_name.toLowerCase())
            || !p.parent_family
            || activeFams.has(p.parent_family.toLowerCase()));
          extra = `<a href="#" id="subs-showall" class="hint" style="color:var(--dim);align-self:center">show all styles</a>`;
        } else {
          extra = `<a href="#" id="subs-showall" class="hint" style="color:var(--dim);align-self:center">only matching styles</a>`;
        }
      }
      const chips = items.map(p => {
        const on = manual.has(p.tag_name.toLowerCase());
        if (q && !firstHit && !on) firstHit = p.tag_name;
        return `<button class="chip ${on ? "active" : ""}" data-tag="${esc(p.tag_name)}">${on ? "✓ " : ""}${esc(p.tag_name)}</button>`;
      }).join("");
      // Grow the vocabulary in place — no trip to the Tags tab (2026-07-06).
      const newBtn = !q && MANAGED_LAYERS.includes(layer)
        ? `<button class="chip newprof" data-newlayer="${esc(layer)}" title="create a new ${layer} tag">+ new</button>` : "";
      return `<div class="tag-sec"><div class="playlbl">${LAYER_LABEL[layer] || layer}</div><div class="palette-row">${chips}${newBtn}${extra}</div></div>`;
    }).filter(Boolean).join("");
    // Free-text: merged into the search box. Only offered when the query
    // matches no vocab chip exactly (descriptive only — won't teach a profile).
    const exact = q && state.profileList.some(p => p.tag_name.toLowerCase() === q);
    const freeRow = q && !exact
      ? `<div class="tag-sec"><div class="playlbl">Free-text</div>
           <button class="chip newprof" data-free="${esc(query.trim())}">↵ add “${esc(query.trim())}” as free-text (descriptive only — won't teach a profile)</button></div>`
      : "";
    palette.innerHTML = (sections + freeRow)
      || '<div class="hint">No profiles seeded yet.</div>';
    palette.dataset.firstHit = firstHit || "";
    const sa = palette.querySelector("#subs-showall");
    if (sa) sa.onclick = (e) => { e.preventDefault(); showAllSubs = !showAllSubs; renderPalette(); };
  };
  renderPalette();

  // "+ new" opens the shared create-profile modal (family selector for
  // subgenre). On success we re-fetch the profile list and re-render this
  // palette so the new chip is immediately tappable.
  const openNewProfile = (layer) => {
    vpAdd(layer, { onCreated: async () => {
      try { state.profileList = (await api("/api/reference/profiles")).profiles || []; } catch (e) {}
      rebuildByLayer(); renderPalette();
    }});
  };

  const toggleTag = async (tag, btn) => {
    const key = tag.toLowerCase();
    if (btn) btn.disabled = true;
    try {
      if (manual.has(key)) {
        await api(`/api/tracks/${pk}/tags/${encodeURIComponent(tag)}`, { method: "DELETE" });
        manual.delete(key); notifyChange(tag, false); toast(`Removed: ${key}`);
      } else {
        await api(`/api/tracks/${pk}/tags`, { method: "POST", body: JSON.stringify({ tag }) });
        manual.add(key); notifyChange(tag, true); toast(`Tagged: ${key}`);
        // Picking a family is explicit intent to gate — snap the "show all
        // styles" escape hatch back off so its subgenres prune immediately.
        if (familyNames.has(key)) showAllSubs = false;
      }
      renderPalette();
    } catch (e) { toast("Failed — try again"); if (btn) btn.disabled = false; }
  };

  const onChipClick = async (ev) => {
    const nb = ev.target.closest(".newprof");
    if (nb && nb.dataset.newlayer) { openNewProfile(nb.dataset.newlayer); return; }
    if (nb && nb.dataset.free) {
      await toggleTag(nb.dataset.free, nb);
      input.value = ""; query = ""; renderPalette(); input.focus();
      return;
    }
    const btn = ev.target.closest(".chip"); if (!btn || !btn.dataset.tag) return;
    await toggleTag(btn.dataset.tag, btn);
  };
  palette.onclick = onChipClick;
  selectedBox.onclick = onChipClick;

  // ── Search box: live filter + Enter-to-toggle + free-text fallback ──────
  const input = bg.querySelector("#tag-search");
  input.oninput = () => { query = input.value; renderPalette(); };
  input.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    const q = input.value.trim(); if (!q) return;
    const first = palette.dataset.firstHit;
    await toggleTag(first || q, null);          // vocab hit, else free-text
    input.value = ""; query = ""; renderPalette(); input.focus();
  });
  // Desktop-first: straight into typing. On touch, skip so the keyboard
  // doesn't shove the palette off-screen.
  if (matchMedia("(pointer: fine)").matches) input.focus();

  bg.querySelector("#tag-done").onclick = close;
}

async function removeTag(pk, tag) {
  if (!confirm(`Remove your manual tag "${tag}"?`)) return;
  await api(`/api/tracks/${pk}/tags/${encodeURIComponent(tag)}`, { method: "DELETE" });
  refreshActiveView(); loadTagChips();
  if (currentPlayingPk() === pk) updateHeroMeta(pk);
}

// Reject an auto-pulled tag for THIS track only. Survives re-enrichment.
async function rejectTag(pk, tag) {
  if (!confirm(`Reject "${tag}" on this track?\n\nIt won't show here or count toward playlists, and won't return on re-enrichment. To hide a tag across your whole library, use the Tags tab instead.`)) return;
  await api(`/api/tracks/${pk}/tags/${encodeURIComponent(tag)}/reject`, { method: "POST", body: "{}" });
  refreshActiveView(); loadTagChips();
  if (currentPlayingPk() === pk) updateHeroMeta(pk);
  toast(`Rejected: ${tag}`);
}

