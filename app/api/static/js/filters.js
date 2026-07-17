// ── Filters / tabs / boot ────────────────

// ── Library facets (2026-07-03): one row of tick-box dropdown filters replaces
//    the rating-chip row, flat tag-chip row and filterbar. Selected values live
//    as removable pills in the summary bar below. ──

const LIB_SORTS = [
  ["added_desc", "Added ↓"], ["added_asc", "Added ↑"], ["title", "Title"],
  ["artist", "Artist"], ["rating_desc", "Rating ↓"], ["duration_desc", "Duration ↓"],
];
const RATING_OPTS = [
  ["", "All"], ["4", "★★★★ Perfect"], ["3", "★★★ Love"],
  ["2", "★★ Really like"], ["1", "★ Like"], ["0", "Unrated"],
];
const FACET_LAYERS = [
  ["family", "Family"], ["subgenre", "Subgenre"], ["functional", "Functional"],
  ["personal", "Personal"], ["era", "Era"], ["other", "Other"],
];

// Name kept from the chip era — every tag-mutation path already calls this.
// Under "Mine only" the facet options + counts cover hand-applied tags only,
// so the rail's numbers always match what the filtered list would show.
async function loadTagChips() {
  const p = state.mineOnly ? "?tag_source=manual" : "";
  try { state.allTags = await api("/api/tags" + p); } catch (e) { state.allTags = []; }
  renderFacetRow(); renderSumBar();
}

async function loadSourcePlaylists() {
  try { state.sourcePlaylists = await api("/api/source-playlists"); } catch (e) {}
  renderFacetRow();
}

// Label facet options (2026-07-16). Cheap — labels are sparse until
// re-enrichment fills them; the facet button hides while the list is empty.
async function loadLabelsFacet() {
  const p = state.mineOnly ? "&tag_source=manual" : "";
  try { state.labelsList = (await api("/api/labels?limit=500" + p)).labels || []; } catch (e) {}
  renderFacetRow();
}

// "Mine only" (2026-07-17): one toggle that re-scopes every other facet —
// style chips then mean "tracks *I* put in this style", counts follow, and
// the list restricts to manually-tagged or rated tracks. Facet options and
// label counts are re-fetched so the rail never lies about the active scope.
function toggleMineOnly() {
  state.mineOnly = !state.mineOnly;
  loadTagChips();
  loadLabelsFacet();
  loadTracks();          // dispatches to the entities view when active
  renderFacetRow();
}

function mineBtnHtml() {
  return `<button class="fbtn minebtn ${state.mineOnly ? "on" : ""}"
    title="Only your own curation: tag facets match hand-applied tags, list restricts to manually-tagged or rated tracks"
    onclick="toggleMineOnly()">👤 Mine only</button>`;
}

// Grand total for "showing X of Y" (changes only on ingest — fetched per visit).
async function loadGrandTotal() {
  try { state.grandTotal = (await api("/api/stats")).total_tracks; } catch (e) {}
  renderSumBar();
}

// Playlist dropdown ordering (client-side, kept from the old filterbar).
function sortedSourcePlaylists() {
  const s = state.playlistSort;
  const byName = (a, b) => a.playlist_name.localeCompare(b.playlist_name, undefined, { sensitivity: "base" });
  return state.sourcePlaylists.slice().sort((a, b) => {
    if (s === "name_desc") return byName(b, a);
    if (s === "tracks_asc") return (a.n - b.n) || byName(a, b);
    if (s === "tracks_desc") return (b.n - a.n) || byName(a, b);
    return byName(a, b);   // name_asc (default)
  });
}

function tagsByLayer() {
  const by = {};
  (state.allTags || []).forEach(t => {
    const l = t.layer || "other";
    (by[l] = by[l] || []).push(t);
  });
  return by;
}

function facetBtnHtml(id, label, on, badge) {
  return `<span class="facetwrap" data-facet="${esc(id)}">
    <button class="fbtn ${on ? "on" : ""}" onclick="toggleFacet('${esc(id)}')">${label}${
      badge ? ` <span class="fcnt">${badge}</span>` : ""} ▾</button></span>`;
}

function renderFacetRow() {
  const el = $("facetrow");
  const by = tagsByLayer();
  const ratingOn = state.untagged || state.dismissed || state.rating !== "";
  const ratingLabel = state.dismissed ? "Dismissed"
    : state.untagged ? "Untagged"
    : state.rating === "" ? "★ Rating"
    : (RATING_OPTS.find(o => o[0] === state.rating) || ["", "★ Rating"])[1];
  const pl = (state.sourcePlaylists || []).find(p => p.playlist_id === state.sourcePlaylist);
  let html = mineBtnHtml();
  html += facetBtnHtml("rating", ratingLabel, ratingOn);
  for (const [layer, label] of FACET_LAYERS) {
    const items = by[layer] || [];
    if (!items.length) continue;   // e.g. no free-text tags → no "Other" button
    const nSel = items.filter(t => state.tags.includes(t.tag)).length;
    html += facetBtnHtml("layer:" + layer, label, nSel > 0, nSel || "");
  }
  html += facetBtnHtml("playlist", pl ? esc(pl.playlist_name) : "All playlists", !!state.sourcePlaylist);
  const lb = (state.labelsList || []).find(l => l.label_id === state.label);
  if ((state.labelsList || []).length)
    html += facetBtnHtml("label", lb ? esc(lb.name) : "Label", !!state.label);
  html += facetBtnHtml("sort", "Sort: " + (LIB_SORTS.find(s => s[0] === state.sort) || LIB_SORTS[0])[1],
                       state.sort !== "added_desc");
  html += `<button class="fbtn ${state.pendingVersions ? "on" : ""}"
    title="Only tracks with a playback-version awaiting review"
    onclick="state.pendingVersions=!state.pendingVersions;loadTracks();renderFacetRow()">⇱ Versions</button>`;
  el.innerHTML = html;
}

// One dropdown open at a time. Rebuilding the row on open/close keeps button
// labels + count badges honest without fighting the open panel.
function closeFacetDd(rerender = true) {
  document.querySelectorAll(".fdd").forEach(d => d.remove());
  if (rerender) renderFacetRow();
}
document.addEventListener("click", (e) => {
  if (!e.target.closest(".facetwrap") && document.querySelector(".fdd")) closeFacetDd();
});

function toggleFacet(id) {
  const wrap = document.querySelector(`.facetwrap[data-facet="${CSS.escape(id)}"]`);
  const wasOpen = wrap && wrap.querySelector(".fdd");
  closeFacetDd();
  if (wasOpen) return;
  const nw = document.querySelector(`.facetwrap[data-facet="${CSS.escape(id)}"]`);
  if (!nw) return;
  const dd = document.createElement("div");
  dd.className = "fdd";
  nw.appendChild(dd);
  renderFacetDd(dd, id);
}

function fddRow(dd, { sel, label, count, onPick }) {
  const r = document.createElement("div");
  r.className = "fddrow" + (sel ? " sel" : "");
  r.innerHTML = `<span class="bx">${sel ? "✓" : ""}</span><span class="n"></span>${
    count !== undefined ? `<span class="c">${count}</span>` : ""}`;
  r.querySelector(".n").textContent = label;
  r.onclick = (e) => { e.stopPropagation(); onPick(r); };
  dd.appendChild(r);
  return r;
}

function renderFacetDd(dd, id) {
  dd.innerHTML = "";
  if (id === "rating") {
    RATING_OPTS.forEach(([v, l]) => fddRow(dd, {
      sel: !state.untagged && !state.dismissed && state.rating === v, label: l,
      onPick: () => { state.rating = v; state.untagged = false; state.dismissed = false; loadTracks(); closeFacetDd(); },
    }));
    fddRow(dd, {
      sel: state.untagged, label: "Untagged — no tags yet",
      onPick: () => { state.untagged = !state.untagged; state.rating = ""; state.dismissed = false; loadTracks(); closeFacetDd(); },
    });
    // R11: "Not for me" verdicts are visible, not gone — each row restorable.
    fddRow(dd, {
      sel: state.dismissed, label: "Dismissed — 'not for me' verdicts",
      onPick: () => { state.dismissed = !state.dismissed; state.rating = ""; state.untagged = false; loadTracks(); closeFacetDd(); },
    });
    return;
  }
  if (id === "sort") {
    LIB_SORTS.forEach(([v, l]) => fddRow(dd, {
      sel: state.sort === v, label: l,
      onPick: () => { state.sort = v; loadTracks(); closeFacetDd(); },
    }));
    return;
  }
  if (id === "label") {
    const items = state.labelsList || [];
    let input = null;
    if (items.length > 8) {
      input = document.createElement("input");
      input.placeholder = "Filter labels…";
      input.onclick = (e) => e.stopPropagation();
      dd.appendChild(input);
    }
    const list = document.createElement("div");
    dd.appendChild(list);
    const renderList = () => {
      list.innerHTML = "";
      const q = input ? input.value.trim().toLowerCase() : "";
      fddRow(list, {
        sel: !state.label, label: "All labels",
        onPick: () => { state.label = ""; loadTracks(); closeFacetDd(); },
      });
      const shown = items.filter(l => !q || l.name.toLowerCase().includes(q));
      shown.forEach(l => fddRow(list, {
        sel: state.label === l.label_id, label: l.name, count: l.track_count,
        onPick: () => { state.label = l.label_id; loadTracks(); closeFacetDd(); },
      }));
      if (!shown.length) list.innerHTML = '<div class="fddempty">No labels match.</div>';
    };
    if (input) input.oninput = renderList;
    renderList();
    if (input) setTimeout(() => input.focus(), 0);
    return;
  }
  if (id === "playlist") {
    const input = document.createElement("input");
    input.placeholder = "Filter playlists…";
    input.onclick = (e) => e.stopPropagation();
    dd.appendChild(input);
    const list = document.createElement("div");
    dd.appendChild(list);
    const renderList = () => {
      list.innerHTML = "";
      const q = input.value.trim().toLowerCase();
      fddRow(list, {
        sel: !state.sourcePlaylist, label: "All playlists",
        onPick: () => { state.sourcePlaylist = ""; loadTracks(); closeFacetDd(); },
      });
      const items = sortedSourcePlaylists().filter(p => !q || p.playlist_name.toLowerCase().includes(q));
      items.forEach(p => fddRow(list, {
        sel: state.sourcePlaylist === p.playlist_id, label: p.playlist_name, count: p.n,
        onPick: () => { state.sourcePlaylist = p.playlist_id; loadTracks(); closeFacetDd(); },
      }));
      if (!items.length) list.innerHTML = '<div class="fddempty">No playlists match.</div>';
    };
    input.oninput = renderList;
    renderList();
    setTimeout(() => input.focus(), 0);
    return;
  }
  if (id.startsWith("layer:")) {
    const layer = id.slice(6);
    const items = (tagsByLayer()[layer] || []);
    let input = null;
    if (items.length > 8) {
      input = document.createElement("input");
      input.placeholder = "Filter tags…";
      input.onclick = (e) => e.stopPropagation();
      dd.appendChild(input);
    }
    const list = document.createElement("div");
    dd.appendChild(list);
    const renderList = () => {
      list.innerHTML = "";
      const q = input ? input.value.trim().toLowerCase() : "";
      const shown = items.filter(t => !q || t.tag.includes(q));
      shown.forEach(t => fddRow(list, {
        sel: state.tags.includes(t.tag), label: t.tag, count: t.n,
        // Multi-select: toggle in place, keep the panel open for more ticks.
        onPick: (row) => {
          const i = state.tags.indexOf(t.tag);
          if (i >= 0) state.tags.splice(i, 1); else state.tags.push(t.tag);
          row.classList.toggle("sel");
          row.querySelector(".bx").textContent = row.classList.contains("sel") ? "✓" : "";
          loadTracks();
        },
      }));
      if (!shown.length) list.innerHTML = '<div class="fddempty">No tags match.</div>';
    };
    if (input) input.oninput = renderList;
    renderList();
    if (input) setTimeout(() => input.focus(), 0);
  }
}

function toggleTagMode() {
  state.tagMode = state.tagMode === "and" ? "or" : "and";
  loadTracks();
}

function removeTagFilter(tg) {
  const i = state.tags.indexOf(tg);
  if (i >= 0) state.tags.splice(i, 1);
  loadTracks(); renderFacetRow();
}

function libHasFilters() {
  return !!(state.q || state.tags.length || state.rating !== "" || state.untagged
            || state.sourcePlaylist || state.pendingVersions
            || state.label || state.artist || state.mineOnly);
}

function clearAllFilters() {
  state.q = ""; $("search").value = "";
  state.rating = ""; state.untagged = false; state.tags = [];
  state.sourcePlaylist = ""; state.pendingVersions = false;
  state.label = ""; state.artist = "";
  if (state.mineOnly) { state.mineOnly = false; loadTagChips(); loadLabelsFacet(); }
  loadTracks(); renderFacetRow();
}

function renderSumBar() {
  const el = $("sumbar");
  if (!el) return;
  const pills = [];
  const pill = (label, js) =>
    `<span class="spill" onclick="${js}" title="remove this filter">${label} ×</span>`;
  if (state.mineOnly) pills.push(pill("👤 mine only", "toggleMineOnly()"));
  state.tags.forEach(tg => pills.push(pill(esc(tg), `removeTagFilter('${esc(tg)}')`)));
  if (state.tags.length >= 2)
    pills.push(`<button class="modebtn2" title="match all vs any of the selected tags" onclick="toggleTagMode()">${state.tagMode.toUpperCase()}</button>`);
  if (state.untagged)
    pills.push(pill("untagged", "state.untagged=false;loadTracks();renderFacetRow()"));
  else if (state.rating !== "")
    pills.push(pill(esc((RATING_OPTS.find(o => o[0] === state.rating) || [])[1] || ""), "state.rating='';loadTracks();renderFacetRow()"));
  const pl = (state.sourcePlaylists || []).find(p => p.playlist_id === state.sourcePlaylist);
  if (pl) pills.push(pill(esc(pl.playlist_name), "state.sourcePlaylist='';loadTracks();renderFacetRow()"));
  if (state.label) {
    const lb = (state.labelsList || []).find(l => l.label_id === state.label);
    pills.push(pill("label: " + esc(lb ? lb.name : state.label), "state.label='';loadTracks();renderFacetRow()"));
  }
  if (state.artist) {
    const ar = (state.artistsList || []).find(a => a.artist_id === state.artist);
    pills.push(pill("artist: " + esc(ar ? ar.name : state.artist), "state.artist='';loadTracks();renderFacetRow()"));
  }
  if (state.pendingVersions)
    pills.push(pill("⇱ versions", "state.pendingVersions=false;loadTracks();renderFacetRow()"));
  const grand = state.grandTotal || state.total;
  const count = libHasFilters()
    ? `Showing <b>${state.total}</b> of ${grand}`
    : `<b>${state.total || grand}</b> tracks`;
  el.innerHTML = `<span class="pills">${count}${pills.length ? " · " : ""}${pills.join(" ")}</span>${
    libHasFilters() ? '<button class="clearall" onclick="clearAllFilters()">× Clear all</button>' : ""}`;
}

let searchTimer;
$("search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); loadTracks(); }, 300);
});

