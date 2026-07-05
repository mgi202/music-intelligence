// ── Matches: sub-tabs (Duplicates / Removed) — the dedup/match review queue ──
async function loadMatches() {
  state.reviewPairs = await api("/api/dedup").catch(() => []);
  const badge = $("matchesbadge");
  if (state.reviewPairs.length) { badge.textContent = state.reviewPairs.length; badge.style.display = ""; }
  else badge.style.display = "none";
  const sub = state.matchesSub || "duplicates";
  $("matches").innerHTML = `<nav class="subtabs">
      <button id="sub-dup" class="${sub === "duplicates" ? "active" : ""}">Duplicates${state.reviewPairs.length ? ` <span class="badge">${state.reviewPairs.length}</span>` : ""}</button>
      <button id="sub-rem" class="${sub === "removed" ? "active" : ""}">Removed</button>
    </nav><div id="subcontent"></div>`;
  $("sub-dup").onclick = () => { state.matchesSub = "duplicates"; loadMatches(); };
  $("sub-rem").onclick = () => { state.matchesSub = "removed"; loadMatches(); };
  if (sub === "duplicates") renderDuplicates();
  else loadRemoved();
}

function renderDuplicates() {
  const pairs = state.reviewPairs || [];
  $("subcontent").innerHTML = pairs.length ? pairs.map(p => {
    const side = (t) => t ? `<div style="flex:1;min-width:0">
        <div class="title">${esc(t.canonical_title)}</div>
        <div class="artist">${esc(t.canonical_artist)}${t.album_title ? " · " + esc(t.album_title) : ""}</div>
        <div class="health">${t.match_status} · ${t.duration_ms ? Math.round(t.duration_ms/1000)+"s" : "?"} · ${t.personal_rating ? "★".repeat(t.personal_rating) : "unrated"}</div>
      </div>` : "<div>(missing)</div>";
    return `<div class="pl">
      <div class="sub">Possible duplicate · ${esc(p.reason || "")}${p.duration_delta_ms != null ? " · Δ" + p.duration_delta_ms + "ms" : ""}</div>
      <div style="display:flex;gap:10px;margin-bottom:10px">${side(p.track_a)}${side(p.track_b)}</div>
      <div class="actions">
        <button class="primary" onclick="mergePair(${p.id})">Merge (keep older)</button>
        <button onclick="dismissPair(${p.id})">Not a duplicate</button>
      </div>
    </div>`;
  }).join("") : '<div class="empty">No pending duplicate pairs.</div>';
}

async function loadRemoved() {
  let removals = [];
  try { removals = await api("/api/removals?days=14"); } catch (e) {}
  $("subcontent").innerHTML = removals.length
    ? `<div class="stats">Removed from your YTM playlists in the last 14 days — tap Re-add to put one back.</div>`
      + removals.map(rm => `<div class="pl">
          <div class="name">${esc(rm.track_title || rm.track_pk)}</div>
          <div class="sub">${esc(rm.track_artist || "")} · removed from <b style="color:#6fb6d6">${esc(rm.playlist_name)}</b>${rm.kind === "all" ? " (remove-all)" : ""} · ${rm.removed_at.slice(0,16).replace("T"," ")}</div>
          <div class="actions"><button class="primary" onclick="undoRemoval(${rm.id})">Re-add</button></div>
        </div>`).join("")
    : '<div class="empty">Nothing removed in the last 14 days.</div>';
}

async function undoRemoval(id) {
  toast("Re-adding…");
  try { await api(`/api/removals/${id}/undo`, { method: "POST", body: "{}" }); toast("Re-added ✓"); loadRemoved(); }
  catch (e) { alert("Re-add failed: " + e.message); }
}

async function mergePair(id) {
  if (!confirm("Merge these two tracks? The newer one is folded into the older.")) return;
  await api(`/api/dedup/${id}/merge`, { method: "POST", body: "{}" });
  toast("Merged"); loadMatches();
}
async function dismissPair(id) {
  await api(`/api/dedup/${id}/dismiss`, { method: "POST", body: "{}" });
  loadMatches();
}

// ── Tags admin: suggestions queue + read-only vocabulary + hide/alias list ──
function vocabRowsHtml(rows) {
  return rows.length ? rows.map(v => `
    <div class="vocab ${v.hidden ? "hidden-tag" : ""}">
      <div class="vname">${esc(v.tag)} <span class="vn">${v.n}</span>${v.alias_to ? ` <span class="alias">→ ${esc(v.alias_to)}</span>` : ""}</div>
      ${v.layer ? `<span class="vlayer" title="this tag is a vocabulary profile">${esc(v.layer)}</span>` : ""}
      ${v.manual ? '<span class="vlayer manual" title="applied by hand on ≥1 track">manual</span>' : ""}
      <button class="vbtn ${v.hidden ? "on" : ""}" onclick="toggleHide('${esc(v.tag)}', ${v.hidden ? 0 : 1})">${v.hidden ? "Hidden" : "Hide"}</button>
      <button class="vbtn" onclick="aliasTag('${esc(v.tag)}')">${v.alias_to ? "Re-alias" : "Alias"}</button>
    </div>`).join("") : '<div class="empty">No tags match.</div>';
}

// New-subgenre suggestions the nightly extraction proposed (per-family tier
// quota). Approve = becomes a real subgenre profile (survives deploys);
// reject = never suggested again.
function suggRowsHtml(rows) {
  return rows.map(s => `
    <div class="sugg" id="sugg-${s.suggestion_id}">
      <div class="sname">${esc(s.tag)} <span class="sn">${s.track_count} tracks</span>
        ${s.family ? ` <span class="sfam">${esc(s.family)}</span>` : ""}
        ${s.was_alias_to ? ` <span class="salias" title="the vocab lock folds this tag into ‘${esc(s.was_alias_to)}’ — approving removes that fold">currently folded → ${esc(s.was_alias_to)}</span>` : ""}</div>
      <button class="sbtn ok" onclick="decideSuggestion(${s.suggestion_id}, 'approve')">✓ Add</button>
      <button class="sbtn" onclick="decideSuggestion(${s.suggestion_id}, 'reject')">✕</button>
    </div>`).join("");
}

// Display order for the read-only vocabulary section (differs from the
// tap-palette's LAYER_ORDER: browsing reads set-arc → moments → genres).
const VOCAB_LAYER_ORDER = ["functional", "personal", "family", "subgenre", "era"];
function vocabProfilesHtml(profiles) {
  return VOCAB_LAYER_ORDER.map(layer => {
    const ps = profiles.filter(p => p.taxonomy_layer === layer);
    if (!ps.length) return "";
    return `<div class="vsec">${layer} · ${ps.length}</div>
      <div class="vprof">${ps.map(p =>
        `<span class="vp ${p.origin === "user_approved" ? "userok" : ""}"
               title="${esc(p.description || "")}${p.origin === "user_approved" ? " (added by you via suggestions)" : ""}">${esc(p.tag_name)} <span class="vpn">${p.n}</span></span>`).join("")}</div>`;
  }).join("");
}

async function loadTags() {
  const [sugg, profiles, vocab] = await Promise.all([
    api("/api/vocab-suggestions"), api("/api/vocabulary/profiles"), api("/api/vocabulary"),
  ]);
  state.vocab = vocab;
  $("tags").innerHTML =
    `${sugg.suggestions.length ? `<div class="vsec">Suggested subgenres · ${sugg.suggestions.length}</div>
        <div class="stats">Your library's coverage earned these candidate slots. ✓ Add makes it a real subgenre; ✕ hides it forever.</div>
        <div id="sugglist">${suggRowsHtml(sugg.suggestions)}</div>`
      : '<div class="vsec">Suggested subgenres</div><div class="stats" id="sugglist-empty">None pending. New candidates appear as your library and tagging grow.</div>'}
     <button class="scanbtn" onclick="rescanVocab(this)">⟳ Scan library for candidates now</button>
     <div class="vsec">Vocabulary</div>
     <div class="stats">The tagging vocabulary, grouped by layer. Additions arrive via suggestions above; renames stay locked.</div>
     ${vocabProfilesHtml(profiles)}
     <div class="vsec">All raw tags</div>
     <div class="stats">${state.vocab.length} distinct tags · "Hide" removes a tag everywhere; "Alias" merges a spelling variant into another tag.</div>
     <input class="vocabsearch" id="vocabsearch" placeholder="Filter tags…" autocomplete="off" autocapitalize="none">
     <div id="vocablist">${vocabRowsHtml(state.vocab.slice(0, 250))}</div>`;
  $("vocabsearch").oninput = (e) => {
    const f = e.target.value.trim().toLowerCase();
    const rows = (f ? state.vocab.filter(v => v.tag.includes(f)) : state.vocab).slice(0, 250);
    $("vocablist").innerHTML = vocabRowsHtml(rows);
  };
}

async function decideSuggestion(id, action) {
  try {
    const r = await api(`/api/vocab-suggestions/${id}/${action}`, { method: "POST", body: "{}" });
    toast(action === "approve" ? `Added “${r.tag}” to the vocabulary ✓` : `Rejected — won't suggest again`);
  } catch (e) { toast(e.message || "Couldn't save"); }
  await loadTags(); loadTagChips();
}

async function rescanVocab(btn) {
  btn.disabled = true; btn.textContent = "Scanning…";
  try {
    const r = await api("/api/vocab-suggestions/recompute", { method: "POST", body: "{}" });
    toast(r.new.length ? `${r.new.length} new suggestion${r.new.length > 1 ? "s" : ""}` : "No new candidates right now");
  } catch (e) { toast(e.message || "Scan failed"); }
  await loadTags();
}

async function toggleHide(tag, hidden) {
  await api(`/api/vocabulary/${encodeURIComponent(tag)}`, { method: "PUT", body: JSON.stringify({ hidden: !!hidden }) });
  toast(hidden ? `Hidden: ${tag}` : `Shown: ${tag}`);
  await loadTags(); loadTagChips();
}

async function aliasTag(tag) {
  const target = prompt(`Merge "${tag}" into which tag?\nType an existing tag name, or leave blank to clear.`, "");
  if (target === null) return;
  await api(`/api/vocabulary/${encodeURIComponent(tag)}`, { method: "PUT", body: JSON.stringify({ alias_to: target.trim() || null }) });
  toast(target.trim() ? `${tag} → ${target.trim().toLowerCase()}` : "Alias cleared");
  await loadTags(); loadTagChips();
}

