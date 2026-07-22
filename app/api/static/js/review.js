// ── Training view: per-profile progress toward the kNN auto-apply gate ──
// Each profile needs 15 positives, 15 negatives-or-near-misses, and 3 distinct
// artists among its positives before it can auto-classify. All derived from
// your tags — this is just the read-out.
async function loadTraining() {
  const el = $("training");
  el.innerHTML = '<div class="trainhead">Loading…</div>';
  let data;
  try { data = await api("/api/reference/readiness"); }
  catch (e) { el.innerHTML = '<div class="empty">Couldn\'t load training status.</div>'; return; }
  const profiles = data.profiles || [];
  const ready = profiles.filter(p => p.ready).length;
  const anyData = profiles.some(p => p.positive || p.negative_plus_near_miss);

  const bar = (label, have, need) => {
    const pct = Math.min(100, Math.round((have / need) * 100));
    const done = have >= need;
    return `<div class="pbar">
      <div class="plabel"><span>${label}</span><span>${have} / ${need}</span></div>
      <div class="ptrack"><div class="pfill ${done ? "done" : ""}" style="width:${pct}%"></div></div>
    </div>`;
  };

  const cards = profiles.map(p => {
    const pill = p.ready
      ? '<span class="pill ready">✓ ready</span>'
      : `<span class="pill wip">in progress</span>`;
    return `<div class="pcard trainable" onclick="trainProfile('${jsarg(p.profile_id)}')" title="tap to label examples for this profile">
      <div class="ptop"><span class="pname">${esc(p.profile_id)}</span>${pill}</div>
      ${bar("Positives", p.positive, 15)}
      ${bar("Negatives + near-miss", p.negative_plus_near_miss, 15)}
      ${bar("Distinct artists", p.distinct_positive_artists, 3)}
    </div>`;
  }).join("");

  const intro = anyData
    ? `<b>${ready}</b> of ${profiles.length} profiles ready to auto-classify. Tap a profile to label examples directly — the fast path to 15+15.`
    : `No examples yet. Tap a profile below to start labelling examples directly, or tag tracks in Review — these bars fill in as you go. Each profile needs 15 positives, 15 negatives/near-misses, and 3 different artists.`;
  el.innerHTML = `<div class="trainhead">${intro}</div>${cards}`;
}

// ── Reference labelling drill-down (Phase 3): one-tap positive / negative /
// near-miss on served candidates — the 10× path to unlocking auto-apply. ──
async function trainProfile(pid) {
  const el = $("training");
  el.innerHTML = '<div class="trainhead">Loading candidates…</div>';
  let data, readiness;
  try {
    [data, readiness] = await Promise.all([
      api(`/api/reference/candidates?profile_id=${encodeURIComponent(pid)}&limit=30`),
      api("/api/reference/readiness"),
    ]);
  } catch (e) { el.innerHTML = '<div class="empty">Couldn\'t load candidates.</div>'; return; }
  const r = (readiness.profiles || []).find(x => x.profile_id === pid) || {};
  state.trainCands = data.candidates || [];

  const row = (t) => {
    const vid = t.playback_video_id || t.ytm_track_id;
    return `<div class="pl trainrow" id="tr-${cssId(t.track_pk)}">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        ${vid ? `<button onclick="play('${jsarg(vid)}', '${jsarg(`${t.canonical_artist} — ${t.canonical_title}`)}', '${jsarg(t.track_pk)}')">▶</button>` : ""}
        <div style="flex:1;min-width:180px">
          <div class="title">${esc(t.canonical_title)}${t.tag_match ? ' <span class="pill wip" title="its existing tags already point at this profile">tag match</span>' : ""}${t.provenance === "classifier_unsure" ? ' <span class="pill wip" title="the nightly classifier scored this near its decision threshold — your verdict here teaches it the most">classifier unsure</span>' : ""}</div>
          <div class="artist">${esc(t.canonical_artist)}${t.personal_rating ? " · " + "★".repeat(t.personal_rating) : ""}</div>
          ${(t.playlists || []).length ? `<div class="artist" title="source playlists this track lives in">in: ${t.playlists.map(p => esc(p.playlist_name)).join(" · ")}</div>` : ""}
        </div>
        <span style="display:flex;gap:6px">
          <button class="primary" title="a clean example of ${esc(pid)}" onclick="trainLabel('${jsarg(t.track_pk)}', '${jsarg(pid)}', 'positive')">✓ Yes</button>
          <button title="clearly not ${esc(pid)}" onclick="trainLabel('${jsarg(t.track_pk)}', '${jsarg(pid)}', 'negative')">✕ No</button>
          <button title="sounds close, but isn't — the most valuable label" onclick="trainLabel('${jsarg(t.track_pk)}', '${jsarg(pid)}', 'near_miss')">≈ Close</button>
        </span>
      </div>
    </div>`;
  };

  el.innerHTML = `<div class="trainhead">
      <a style="cursor:pointer;color:#6fb6d6" onclick="loadTraining()">← All profiles</a>
      &nbsp; <b>${esc(pid)}</b> — ${r.positive || 0}/15 yes · ${r.negative_plus_near_miss || 0}/15 no+close · ${r.distinct_positive_artists || 0}/3 artists
      <div class="sub" style="margin-top:4px">Listen, then one tap: <b>Yes</b> = clean example · <b>No</b> = clearly not · <b>Close</b> = sounds like it but isn't (teaches the boundary).</div>
    </div>
    <div id="trainlist">${state.trainCands.length ? state.trainCands.map(row).join("") : '<div class="empty">No unlabelled candidates left for this profile.</div>'}</div>`;
}

function cssId(s) { return s.replace(/[^a-zA-Z0-9_-]/g, "_"); }

async function trainLabel(pk, pid, labelType) {
  try {
    await api("/api/reference/label", {
      method: "POST",
      body: JSON.stringify({ track_pk: pk, profile_id: pid, label_type: labelType }),
    });
    toast({ positive: "Labelled ✓ yes", negative: "Labelled ✕ no", near_miss: "Labelled ≈ close" }[labelType]);
  } catch (e) { toast(e.message || "Couldn't label"); return; }
  const el = $("tr-" + cssId(pk));
  if (el) el.remove();
  state.trainCands = (state.trainCands || []).filter(t => t.track_pk !== pk);
  if (!state.trainCands.length) trainProfile(pid);
}

// ── Review: one queue, two sort lenses. One listen → rating + tags + cull. ──
// Refined 2026-07-03 (CLAUDE-CODE-HANDOFF-review-refinement.md / REVIEW-MOCKUP.html):
// compact card with triage demoted to the header, set-order chips with hotkeys,
// skim controls, playlist filter + up-next rail, Later counter with 14-day
// auto-return, empty-commit guard, undo for every verdict, a closest-to-ready
// panel and a this-session log. A card retires on commit, dismiss, or skip —
// never on rating alone.

const VQ_FKEYS = ["q", "w", "e", "r", "t", "y", "u", "i", "o"];  // functional chips, set order
// Personal chips, display order. k/l cover user-added profiles (vocab
// manager); chips beyond the key run render without a hotkey (mouse/tap).
const VQ_PKEYS = ["a", "s", "d", "f", "g", "h", "j", "k", "l"];
const VQ_EKEYS = ["7", "8", "9", "0", "-"];                  // era chips — number row, decade order

// The micro-question vocab (func/personal/era chips + tie-breakers). It's
// invalidated (set to null) whenever the vocab manager adds/renames/retires a
// profile, so every Review render path must reload it before drawing chips —
// otherwise the chips silently render empty. Cheap no-op once cached.
async function vqEnsureProfiles() {
  if (state.vqProfiles) return;
  try {
    const d = await api("/api/reference/profiles");
    state.vqProfiles = d.profiles || []; state.vqTiebreakers = d.tiebreakers || [];
  } catch (e) { state.vqProfiles = []; state.vqTiebreakers = []; }
}

async function loadVerdict() {
  const el = $("review");
  await vqEnsureProfiles();
  if (!state.sourcePlaylists.length) {
    try { state.sourcePlaylists = await api("/api/source-playlists"); } catch (e) {}
  }
  el.innerHTML = '<div class="vq-strip">Loading queue…</div>';
  try {
    const p = new URLSearchParams({ limit: 20, sort: state.reviewSort });
    if (state.vqPlFilter) p.set("source_playlist", state.vqPlFilter);
    const [q, r] = await Promise.all([
      api(`/api/verdict/queue?${p}`),
      api("/api/reference/readiness"),
    ]);
    state.vq = q.tracks || []; state.vqMeta = q.meta || {}; state.vqIndex = 0;
    state.vqReadiness = r.profiles || [];
    state.vqLoaded = true;
  } catch (e) { el.innerHTML = '<div class="empty">Couldn\'t load the queue.</div>'; return; }
  renderVerdict();
  vqStartClock();
}

// Re-entering the Review tab must NOT rebuild the queue — that would throw away
// the card you were on and restart playback. Re-render the current card in place
// (audio lives in the persistent player, so it keeps going). First-ever entry,
// and the global ⟳ (full page reload), still fetch a fresh queue. The vocab may
// have been invalidated by an edit in the Tags tab while away, so reload it
// before re-rendering or the chips would come back empty.
async function enterVerdict() {
  if (!state.vqLoaded) { loadVerdict(); return; }
  await vqEnsureProfiles();
  renderVerdict(); vqStartClock();
}

function setReviewSort(s) {
  if (s === state.reviewSort) return;
  state.reviewSort = s; localStorage.setItem("reviewSort", s);
  loadVerdict();
}

function vqSetPlaylist(v) {
  state.vqPlFilter = v; localStorage.setItem("reviewPlaylist", v);
  loadVerdict();
}

function vqStrip() {
  const lens = `<span class="vq-lens">
    <button class="${state.reviewSort === "newest" ? "on" : ""}" onclick="setReviewSort('newest')" title="unrated tracks, newest first — drain to zero">Newest</button>
    <button class="${state.reviewSort === "training" ? "on" : ""}" onclick="setReviewSort('training')" title="tracks whose verdict most advances a profile">Training</button></span>`;
  const plOpts = ['<option value="">All playlists</option>']
    .concat(sortedSourcePlaylists().map(p =>
      `<option value="${esc(p.playlist_id)}" ${state.vqPlFilter === p.playlist_id ? "selected" : ""}>${esc(p.playlist_name)}</option>`))
    .join("");
  const plSel = `<select onchange="vqSetPlaylist(this.value)" title="judge only tracks in this YTM playlist — counts follow the filter">${plOpts}</select>`;
  const prof = state.vqReadiness || [];
  const ready = `<span title="profiles with enough training data to auto-classify">ready <b>${prof.filter(x => x.ready).length}</b>/${prof.length}</span>`;
  const sess = `<span title="verdicts this sitting — resets on reload">session: <b>${state.vqSession.length}</b> judged</span>`;
  const skipped = (state.vqMeta || {}).skipped_total || 0;
  const later = skipped ? `<span title="tracks deferred with Later — they auto-return after 14 days">Later: <b>${skipped}</b> · <a class="laterlink" onclick="vqUnskipAll()">re-serve</a></span>` : "";
  const left = state.vqMeta ? (state.vqMeta.eligible_total || 0) : 0;
  const count = state.reviewSort === "newest"
    ? `<span title="tracks with no rating yet under the current filter — the inbox denominator"><b>${left}</b> unrated</span>`
    : `<span title="tracks eligible for judging under the current filter — excludes dismissed, deferred and already-answered tracks"><b>${left}</b> judgeable</span>`;
  return `<div class="vq-strip">${lens}${plSel}${ready}${sess}<span class="spacer"></span>${later}${count}</div>`;
}

function vqProfilesByLayer(layer) {
  // API order = tag_profiles.sort_order — functional arrives in SET order.
  return (state.vqProfiles || []).filter(p => p.taxonomy_layer === layer);
}

function renderVerdict() {
  const el = $("review");
  // Safety net: the chip vocab must be present or the func/personal/era rows
  // render empty (headers with no chips). If an edit invalidated it and a
  // render slipped through, reload and re-render rather than draw empty.
  if (!state.vqProfiles) { vqEnsureProfiles().then(renderVerdict); return; }
  if (state.vqIndex >= state.vq.length) {
    if (state.vq.length && state.vqMeta && state.vqMeta.eligible_total > state.vq.length) {
      loadVerdict(); return;   // fetch a fresh batch (judged tracks won't return)
    }
    el.innerHTML = vqStrip()
      + `<div class="empty">${state.reviewSort === "newest"
            ? "Review zero ✓ — nothing needs a first listen."
            : "Nothing left to judge here. 🎉"}<br>`
      + '<span class="vq-note"><a onclick="vqUnskipAll()">Re-serve tracks marked Later</a></span></div>'
      + vqSessionLogHtml();
    return;
  }
  const t = state.vq[state.vqIndex];
  // Era arrives PREFILLED from the release decade (CARD-WEIGHT RULE:
  // confirm-or-correct, never choose-from-five). Committing the card confirms
  // it; clearing the chip = no era claim.
  if (!t._sel) t._sel = { func: null, personal: new Set(), era: t.era_prefill || null, accepted: new Set(), rejected: new Set(), added: new Set() };
  state.vqRevealed = false;
  // Suggestions "+ n more" expander collapses again on each new card (S1).
  if (state.vqSuggCardPk !== t.pk) { state.vqSuggExpanded = false; state.vqSuggCardPk = t.pk; }

  const existing = (t.tags || []).map(g =>
    `<span class="tag ${g.tag_type === "private_manual" ? "manual" : ""}">${esc(g.tag)}</span>`).join("");
  // R10: playlist chips are actionable — same removal + undo path as Library.
  const plchips = (t.playlists || []).map(p =>
    `<span class="plchip"><span class="plname">${esc(p.playlist_name)}</span>
       <button class="plx" title="remove from this playlist on YTM" onclick="vqRemovePl('${t.pk}','${esc(p.playlist_id)}')">×</button></span>`).join("");

  // Suggestions — the section collapses entirely when empty (R1); it returns
  // by itself as enrichment data arrives. The backend returns ALL mapped
  // suggestions (capped ≤12); the card shows the top 3 with hotkeys and hides
  // the rest behind a "+ n more" expander (S1). Only the top 3 carry 1/2/3 —
  // expanded rows are click-only so the key map never shifts.
  const sugg = (t.suggestions || []);
  const suggCard = (s, i, hotkey) => {
    const acc = t._sel.accepted.has(s.profile_id);
    const rej = t._sel.rejected.has(s.profile_id);
    const ev = (s.evidence || []).map(e => `<span>${esc(e)}</span>`).join("");
    return `<div class="vq-scard ${acc ? "accepted" : ""} ${rej ? "rejected" : ""}">
      <div class="vq-srow">
        <span class="vq-skey">${hotkey ? i + 1 : "·"}</span>
        <span class="vq-sname">${esc(s.tag_name)}</span>
        <span class="vq-slayer">${esc(s.taxonomy_layer)}</span>
        <button class="vq-sbtn yes ${acc ? "on" : ""}" onclick="vqAccept(${i})">✓ yes</button>
        <button class="vq-sbtn no ${rej ? "on" : ""}" onclick="vqReject(${i})">✕ no</button>
      </div>
      ${ev ? `<div class="vq-ev">${ev}</div>` : ""}`
      + `</div>`;
  };
  let suggBlock = "";
  if (sugg.length) {
    const top = sugg.slice(0, 3).map((s, i) => suggCard(s, i, true)).join("");
    const restCount = sugg.length - 3;
    let expander = "";
    if (restCount > 0) {
      const rest = sugg.slice(3).map((s, i) => suggCard(s, i + 3, false)).join("");
      // Collapsed by default; state.vqSuggExpanded is reset per card (see below).
      expander = state.vqSuggExpanded
        ? `<div class="vq-sugg-rest">${rest}</div>
           <div class="vq-morelink" onclick="vqToggleSugg()">− show fewer</div>`
        : `<div class="vq-morelink" onclick="vqToggleSugg()">+ ${restCount} more</div>`;
    }
    const blur = state.vqGuessFirst ? "blurred" : "";
    const revealBtn = state.vqGuessFirst
      ? '<div class="vq-reveal" onclick="vqReveal()">👂 Guess first — tap or press <b>b</b> to reveal suggestions</div>' : "";
    suggBlock = `<div class="vq-seclbl">Suggestions <span class="vq-skiphint">1/2/3 = yes · ⇧1/2/3 = no</span>
      <button class="vq-addtag" onclick="vqAddTag()" title="add any vocab or free-text tag — vocab matches teach a profile">+ tag</button></div>
      ${revealBtn}<div class="vq-sugg ${blur}">${top}${expander}</div>`;
  } else {
    // No mapped suggestions yet — still let the user reach the full vocab.
    suggBlock = `<div class="vq-seclbl">Suggestions <span class="vq-skiphint">none mapped yet</span>
      <button class="vq-addtag" onclick="vqAddTag()" title="add any vocab or free-text tag — vocab matches teach a profile">+ tag</button></div>`;
  }

  const funcChips = vqProfilesByLayer("functional").map((p, i) =>
    `<span class="vq-chip ${t._sel.func === p.tag_name ? "on" : ""}" title="${esc(p.description || "")}" onclick="vqPickFunc('${jsarg(p.tag_name)}')"><kbd class="vqk">${VQ_FKEYS[i] || ""}</kbd>${esc(p.tag_name)}</span>`).join("");
  const persChips = vqProfilesByLayer("personal").map((p, i) =>
    `<span class="vq-chip ${t._sel.personal.has(p.tag_name) ? "on" : ""}" title="${esc(p.description || "")}" onclick="vqTogglePersonal('${jsarg(p.tag_name)}')"><kbd class="vqk">${VQ_PKEYS[i] || ""}</kbd>${esc(p.tag_name)}</span>`).join("");
  const eraChips = vqProfilesByLayer("era").map((p, i) =>
    `<span class="vq-chip ${t._sel.era === p.tag_name ? "on" : ""}" title="${esc(p.description || "")}" onclick="vqPickEra('${jsarg(p.tag_name)}')"><kbd class="vqk">${VQ_EKEYS[i] || ""}</kbd>${esc(p.tag_name)}</span>`).join("");
  const eraHint = t.era_prefill
    ? (t.release_year ? `prefilled from ${t.release_year} — correct if vibe ≠ date, tap again to clear`
                      : "prefilled from weak evidence — correct or tap again to clear")
    : "single · sounds-like, not release date · optional";

  const r = t.personal_rating || 0;
  const ratingStars = [1, 2, 3, 4].map(i =>
    `<button class="${i <= r ? "lit" : ""}" onclick="vqRate(${i})" title="${i}★ (${"zxcv"[i - 1]})">★</button>`).join("");

  el.innerHTML = vqStrip() + `<div class="vq-wrap"><div><div class="vq-card">
    <div class="vq-head">
      <button class="vq-play" id="vq-playbtn" onclick="vqPlay()" title="Play/Pause (space)">${vqIsCardPlaying() ? "⏸" : "▶"}</button>
      <div style="min-width:0">
        <div class="vq-title">${esc(t.title || "Untitled")}</div>
        <div class="vq-artist">${esc(t.artist || "")}</div>
        ${t.album ? `<div class="vq-album">${esc(t.album)}</div>` : ""}
        <div class="vq-time" id="vq-time">${t.duration_ms ? "0:00 / " + fmtDurMs(t.duration_ms) : ""}</div>
      </div>
      <div class="vq-headright">
        <div class="vq-htriage">
          <button class="nfm" onclick="vqDismiss()" title="stop showing me this — recoverable under Library → Dismissed (n)"><kbd class="vqk">n</kbd>✗ Not for me</button>
          <button onclick="vqSkip()" title="defer — auto-returns after 14 days (m)"><kbd class="vqk">m</kbd>→ Later</button>
          <button onclick="trackMenu('${t.pk}')" title="more (block, don't recommend, cull, versions…)">⋯</button>
        </div>
        <div class="plrow">${plchips}</div>
      </div>
    </div>
    ${existing ? `<div class="vq-existing">${existing}</div>` : ""}

    <div class="vq-pbar">
      <div class="vq-ptrack" onclick="vqSeekClick(event)" title="click to seek"><div class="vq-pfill" id="vq-pfill"></div></div>
      <div class="vq-pquarters" title="↑/↓ jump between quarters · ←/→ seek ±30s"><span>intro</span><span>25%</span><span>50%</span><span>75%</span><span>outro</span></div>
    </div>

    <div class="vq-seclbl">Rating <span class="vq-skiphint">z/x/c/v = 1–4★ · same again clears</span></div>
    <div class="vq-stars">${ratingStars}</div>

    <div class="vq-qgrid">
      <div>
        <div class="vq-seclbl">Where in a set? <span class="vq-help" onclick="vqShowTiebreakers()" title="tie-breaker rules">?</span><span class="vq-skiphint">single · set order</span></div>
        <div class="vq-chips">${funcChips}</div>
      </div>
      <div>
        <div class="vq-seclbl">Life context?<span class="vq-skiphint">multi</span></div>
        <div class="vq-chips">${persChips}</div>
      </div>
    </div>

    ${eraChips ? `<div class="vq-seclbl">Era? <span class="vq-skiphint">${esc(eraHint)}</span></div>
    <div class="vq-chips">${eraChips}</div>` : ""}

    ${suggBlock}

    <div class="vq-footer">
      <label class="vq-toggle"><input type="checkbox" ${state.vqGuessFirst ? "checked" : ""} onchange="vqToggleGuess(this.checked)"> Guess-first (hide suggestions until revealed)</label>
      <button class="commitbtn" onclick="vqCommit()" title="apply rating + chips and move on (⏎)">Commit &amp; next ⏎</button>
    </div>
  </div>${vqSessionLogHtml()}</div>
  <div class="vq-railcol">${vqRailHtml()}${vqReadyPanelHtml()}</div></div>`;
  // No auto-play (removed 2026-07-05): the card's ▶ and the rail are the only
  // ways a track starts. vqPlayedPk still tracks which card the player is on.
}

// ── Up-next rail (R5): peek + jump + dismiss-without-listening ──
function vqRailHtml() {
  const next = state.vq.slice(state.vqIndex + 1, state.vqIndex + 11);
  const rows = next.map((t, i) => {
    const abs = state.vqIndex + 1 + i;
    return `<div class="vq-row" onclick="vqJump(${abs})" title="jump the card to this track">
      <div class="t"><span class="n">${esc(t.title)}</span><span class="a">${esc(t.artist)}</span></div>
      <span class="d">${t.duration_ms ? fmtDurMs(t.duration_ms) : ""}</span>
      <button class="x" title="not for me — dismiss without listening" onclick="event.stopPropagation();vqRailDismiss(${abs})">✗</button>
    </div>`;
  }).join("");
  return `<div class="vq-panel"><h3>Up next</h3>${rows || '<div class="vq-slogempty">End of the fetched batch — committing on loads more.</div>'}</div>`;
}

function vqJump(i) {
  if (!state.vq[i]) return;
  state.vqIndex = i;
  renderVerdict();
}

async function vqRailDismiss(i) {
  const t = state.vq[i]; if (!t) return;
  try { await api(`/api/tracks/${t.pk}/dismiss`, { method: "POST", body: "{}" }); }
  catch (e) { toast("Failed — try again"); return; }
  state.vq.splice(i, 1);
  if (state.vqMeta) state.vqMeta.eligible_total = Math.max(0, (state.vqMeta.eligible_total || 1) - 1);
  vqLogAdd(t, "nfm", []);
  renderVerdict();
  undoToast(`Dismissed: ${t.title}`, async () => {
    await api(`/api/tracks/${t.pk}/undismiss`, { method: "POST", body: "{}" });
    state.vq.splice(Math.min(i, state.vq.length), 0, t);
    if (state.vqMeta) state.vqMeta.eligible_total = (state.vqMeta.eligible_total || 0) + 1;
    vqLogRemoveByPk(t.pk);
    renderVerdict();
  });
}

// ── Closest-to-ready panel (R9): the write-side readout of the Training tab ──
function vqReadyPanelHtml() {
  const list = (state.vqReadiness || []).map(p => ({
    ...p,
    deficit: (p.needs_positive || 0) + (p.needs_negative_or_near_miss || 0) + (p.needs_artists || 0) * 3,
  })).sort((a, b) => (a.ready ? 1 : 0) - (b.ready ? 1 : 0) || a.deficit - b.deficit).slice(0, 5);
  const bar = (pid, l, have, max) => {
    const pct = Math.min(100, (have / max) * 100);
    const bumped = l === "yes" && state.vqBumped.includes(pid);
    return `<div class="vq-bar"><span class="l">${l}</span><div class="tr"><div class="fl ${have >= max ? "full" : ""}" style="width:${pct}%"></div></div><span class="v">${bumped ? '<span class="bump">+1 </span>' : ""}${have}/${max}</span></div>`;
  };
  const cards = list.map(p => {
    const need = p.ready ? "✓ ready" : [
      p.needs_positive ? `${p.needs_positive} more yes` : null,
      p.needs_negative_or_near_miss ? `${p.needs_negative_or_near_miss} more no` : null,
      p.needs_artists ? `${p.needs_artists} more artist${p.needs_artists > 1 ? "s" : ""}` : null,
    ].filter(Boolean).join(" · ");
    return `<div class="vq-pcard">
      <div class="vq-ptop"><span class="vq-pname">${esc(p.profile_id)}</span><span class="vq-pneed ${p.ready ? "done" : ""}">${need}</span></div>
      ${bar(p.profile_id, "yes", p.positive, 15)}
      ${bar(p.profile_id, "no", p.negative_plus_near_miss, 15)}
      ${bar(p.profile_id, "artists", p.distinct_positive_artists, 3)}
    </div>`;
  }).join("");
  return `<div class="vq-panel"><h3>Closest to ready <span style="float:right;letter-spacing:0;text-transform:none" title="a profile auto-classifies once it has 15 yes + 15 no examples from 3+ distinct artists">needs 15 yes · 15 no · 3 artists</span></h3>${cards || '<div class="vq-slogempty">—</div>'}</div>`;
}

// ── This-session verdict log (R13). FE state only — the permanent record is
// the tags themselves; row-level undo reverses the verdict via the same
// endpoints as the toast undo. ──
function vqSessionLogHtml() {
  const rows = state.vqSession.map((e, i) => `
    <div class="vq-slogrow"><span class="when">${e.hh}</span>
      <span class="what"><span class="tk">${esc(e.track)}</span> &nbsp;${
        e.kind === "nfm" ? '<span class="nf">✗ not for me</span>' : `<span class="ap">${esc(e.text)}</span>`}</span>
      <a onclick="vqLogUndo(${i})" title="reverse this verdict — removes applied tags / restores rating / undismisses">undo</a></div>`).join("");
  return `<div class="vq-panel vq-slog"><h3>This session · ${state.vqSession.length}</h3>
    <div class="scroll">${rows || '<div class="vq-slogempty">Your verdicts appear here as you go — with one-tap undo for each.</div>'}</div></div>`;
}

function vqLogAdd(t, kind, appliedTags) {
  const before = t._ratedThisCard ? (t._ratingBefore ?? null) : (t.personal_rating ?? null);
  const after = t.personal_rating ?? null;
  const ratedChanged = !!t._ratedThisCard && after !== before;
  const parts = (appliedTags || []).slice();
  if (ratedChanged) parts.push(after ? `${after}★` : "rating cleared");
  const d = new Date();
  state.vqSession.unshift({
    hh: `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`,
    pk: t.pk, track: `${t.title} — ${t.artist}`, kind,
    tags: (appliedTags || []).slice(), ratingBefore: before, ratingAfter: after,
    ratedChanged, text: parts.join(", ") || "committed",
  });
}

function vqLogRemoveByPk(pk) {
  const i = state.vqSession.findIndex(e => e.pk === pk);
  if (i >= 0) state.vqSession.splice(i, 1);
}

async function vqUndoVerdict(e) {
  if (e.kind === "nfm") {
    await api(`/api/tracks/${e.pk}/undismiss`, { method: "POST", body: "{}" });
  } else {
    // Clear the commit stamp FIRST (and abort the undo if it fails) — it is
    // what re-serves the track; tag/rating reversal below stays best-effort.
    await api(`/api/tracks/${e.pk}/verdict/uncommit`, { method: "POST", body: "{}" });
    for (const tag of e.tags) {
      try { await api(`/api/tracks/${e.pk}/tags/${encodeURIComponent(tag)}`, { method: "DELETE" }); }
      catch (err) {}   // tag may already be gone — undo stays best-effort per tag
    }
    if (e.ratedChanged) {
      await api(`/api/tracks/${e.pk}/rating`, { method: "PUT", body: JSON.stringify({ rating: e.ratingBefore }) });
    }
  }
  if (state.vqMeta) state.vqMeta.eligible_total = (state.vqMeta.eligible_total || 0) + 1;
  try { const r = await api("/api/reference/readiness"); state.vqReadiness = r.profiles || []; } catch (err) {}
}

async function vqLogUndo(i) {
  const e = state.vqSession[i]; if (!e) return;
  try { await vqUndoVerdict(e); } catch (err) { toast("Undo failed — try again"); return; }
  state.vqSession.splice(i, 1);
  toast("Undone — verdict reversed");
  renderVerdict();
}

function vqCard() { return state.vq[state.vqIndex]; }

// Is the player currently PLAYING this card's track? Drives the ▶/⏸ symbol.
function vqIsCardPlaying() {
  const t = vqCard();
  return !!(t && ytReady && currentPlayingPk() === t.pk
            && ytPlayer.getPlayerState
            && ytPlayer.getPlayerState() === YT.PlayerState.PLAYING);
}

function vqPlay() {
  const t = vqCard(); if (!t || !t.video_id) return;
  // Card track already loaded → same pause/resume toggle as the space bar
  // (loading again would restart the video). Otherwise load and play it.
  if (ytReady && currentPlayingPk() === t.pk) { togglePlay(); return; }
  state.vqPlayedPk = t.pk;
  play(t.video_id, `${t.artist} — ${t.title}`, t.pk, !!t.playback_video_id);
}

function vqToggleSugg() {
  state.vqSuggExpanded = !state.vqSuggExpanded;
  renderVerdict();
}

// + tag on the Review card (S2): the same family-gated palette + free-text flow
// as Library, wired to the queue's bookkeeping. A vocab match is a positive
// verdict for that profile (via the tags endpoint / reference derivation), so we
// refresh readiness and fire the +1 flash; every applied tag is tracked in
// _sel.added so it joins this card's commit log and its undo.
async function vqAddTag() {
  const t = vqCard(); if (!t) return;
  const bumped = [];
  await openTagModal(t.pk, t, {
    onChange: (tag, added, prof) => {
      const key = tag.toLowerCase();
      if (added) {
        t._sel.added.add(tag);
        if (prof) bumped.push(prof.profile_id);
      } else {
        for (const x of [...t._sel.added]) if (x.toLowerCase() === key) t._sel.added.delete(x);
      }
    },
    onClose: async () => {
      // A "+ new" profile created from inside the modal invalidates the chip
      // vocab (vqProfiles=null) — reload it before re-rendering or the chips
      // would come back empty.
      await vqEnsureProfiles();
      try { const r = await api("/api/reference/readiness"); state.vqReadiness = r.profiles || []; } catch (e) {}
      if (bumped.length) {
        state.vqBumped = bumped.slice();
        setTimeout(() => { state.vqBumped = []; if (state.view === "review") renderVerdict(); }, 2500);
      }
      renderVerdict();
    },
  });
}

async function vqAccept(i) {
  const t = vqCard(); const s = (t.suggestions || [])[i]; if (!s) return;
  try {
    await api(`/api/tracks/${t.pk}/tags`, { method: "POST", body: JSON.stringify({ tag: s.tag_name }) });
    t._sel.accepted.add(s.profile_id); t._sel.rejected.delete(s.profile_id);
    toast(`✓ ${s.tag_name}`); renderVerdict();
  } catch (e) { toast("Failed — try again"); }
}

async function vqReject(i) {
  const t = vqCard(); const s = (t.suggestions || [])[i]; if (!s) return;
  try {
    await api(`/api/tracks/${t.pk}/verdict/reject/${encodeURIComponent(s.profile_id)}`, { method: "POST", body: "{}" });
    t._sel.rejected.add(s.profile_id); t._sel.accepted.delete(s.profile_id);
    toast(`✕ not ${s.tag_name}`); renderVerdict();
  } catch (e) { toast("Failed — try again"); }
}

function vqPickFunc(tag) {
  const t = vqCard(); t._sel.func = (t._sel.func === tag) ? null : tag; renderVerdict();
}
function vqTogglePersonal(tag) {
  const t = vqCard();
  if (t._sel.personal.has(tag)) t._sel.personal.delete(tag); else t._sel.personal.add(tag);
  renderVerdict();
}
function vqPickEra(tag) {
  // Single-select toggle. _eraTouched marks a deliberate era verdict — an
  // UNTOUCHED prefill never counts as input (no free ride past the empty-
  // commit trap), but it IS committed alongside real input (confirm-or-correct).
  const t = vqCard();
  t._sel.era = (t._sel.era === tag) ? null : tag;
  t._eraTouched = true;
  renderVerdict();
}

// Anything counts as input — a rating alone is a valid commit (R7).
function vqInputMade(t) {
  return !!(t._sel.func || t._sel.personal.size || t._sel.accepted.size
            || t._sel.rejected.size || t._sel.added.size || t._ratedThisCard || t._eraTouched);
}

async function vqCommit() {
  const t = vqCard(); if (!t) return;
  if (!vqInputMade(t)) {   // R7: an empty commit is a no-op trap, not a Later
    toast("Nothing to commit — n = not for me, m = later");
    return;
  }
  const tags = [];
  if (t._sel.func) tags.push(t._sel.func);
  t._sel.personal.forEach(x => tags.push(x));
  if (t._sel.era) tags.push(t._sel.era);   // prefill-or-corrected — commit confirms it
  // Tags added via the card's "+ tag" (S2) — already applied, but re-posting is
  // idempotent and this folds them into the commit's log + undo. Dedupe so a tag
  // that was both chip-picked and +tag-added isn't double-listed.
  t._sel.added.forEach(x => { if (!tags.some(y => y.toLowerCase() === x.toLowerCase())) tags.push(x); });
  try {
    for (const tag of tags) {
      await api(`/api/tracks/${t.pk}/tags`, { method: "POST", body: JSON.stringify({ tag }) });
    }
  } catch (e) { toast("Some tags failed — moving on"); }
  // Persistent commit stamp (2026-07-05): the track never re-serves in either
  // lens. Stamped even if a tag post failed — the user judged the track.
  try { await api(`/api/tracks/${t.pk}/verdict/commit`, { method: "POST", body: "{}" }); }
  catch (e) {}
  const acceptedNames = [...t._sel.accepted].map(pid => {
    const s = (t.suggestions || []).find(x => x.profile_id === pid); return s ? s.tag_name : pid;
  });
  const applied = tags.concat(acceptedNames);
  vqLogAdd(t, "commit", applied);
  const logText = state.vqSession[0].text;

  const prevReady = new Set((state.vqReadiness || []).filter(p => p.ready).map(p => p.profile_id));
  state.vqBumped = applied.slice();   // locked vocab: profile_id == tag_name
  state.vqLastAdv = applied.join(", ");
  state.vqIndex++;
  if (state.vqMeta) state.vqMeta.eligible_total = Math.max(0, (state.vqMeta.eligible_total || 1) - 1);
  try { const r = await api("/api/reference/readiness"); state.vqReadiness = r.profiles || []; } catch (e) {}
  renderVerdict();

  const newlyReady = (state.vqReadiness || []).filter(p => p.ready && !prevReady.has(p.profile_id));
  if (newlyReady.length) {
    // The celebration outranks the undo toast (single slot) — the session log
    // keeps per-row undo available either way.
    toast(`🎉 ${newlyReady.map(p => p.profile_id).join(", ")} is ready to auto-classify`);
  } else {
    undoToast(`Committed: ${logText}`, vqUndoLastCommit);
  }
  setTimeout(() => {
    state.vqBumped = [];
    if (state.view === "review") renderVerdict();
  }, 2500);
}

async function vqUndoLastCommit() {
  const e = state.vqSession[0];
  if (!e || e.kind !== "commit") return;
  try { await vqUndoVerdict(e); } catch (err) { toast("Undo failed — try again"); return; }
  state.vqSession.shift();
  const prev = state.vq[state.vqIndex - 1];
  if (prev && prev.pk === e.pk) {
    state.vqIndex--;
    prev.personal_rating = e.ratingBefore;
    prev._ratedThisCard = false; delete prev._ratingBefore;
  }
  renderVerdict();
}

// Later — a deferral, not a judgement. Auto-returns after 14 days (R6).
async function vqSkip() {
  const t = vqCard(); if (!t) return;
  try { await api(`/api/tracks/${t.pk}/verdict/skip`, { method: "POST", body: "{}" }); } catch (e) {}
  state.vqLastAdv = "";
  state.vqIndex++;
  if (state.vqMeta) {
    state.vqMeta.eligible_total = Math.max(0, (state.vqMeta.eligible_total || 1) - 1);
    state.vqMeta.skipped_total = (state.vqMeta.skipped_total || 0) + 1;
  }
  toast("Later — returns automatically in 14 days");
  renderVerdict();
}

// Not for me — a judgement ("stop showing me this"). Leaves BOTH lenses,
// recoverable via the undo toast, the session log, or Library → Dismissed.
async function vqDismiss() {
  const t = vqCard(); if (!t) return;
  try { await api(`/api/tracks/${t.pk}/dismiss`, { method: "POST", body: "{}" }); }
  catch (e) { toast("Failed — try again"); return; }
  vqLogAdd(t, "nfm", []);
  state.vqLastAdv = "";
  state.vqIndex++;
  if (state.vqMeta) state.vqMeta.eligible_total = Math.max(0, (state.vqMeta.eligible_total || 1) - 1);
  renderVerdict();
  undoToast("Not for me — won't show again", async () => {
    await api(`/api/tracks/${t.pk}/undismiss`, { method: "POST", body: "{}" });
    vqLogRemoveByPk(t.pk);
    if (state.vq[state.vqIndex - 1] && state.vq[state.vqIndex - 1].pk === t.pk) state.vqIndex--;
    if (state.vqMeta) state.vqMeta.eligible_total = (state.vqMeta.eligible_total || 0) + 1;
    renderVerdict();
  });
}

// One-tap rating on the card. Does NOT retire the card — retirement is
// commit/dismiss/skip only — but a newly-rated track leaves the Newest lens,
// so the drain count drops by one there.
async function vqRate(rating) {
  const t = vqCard(); if (!t) return;
  const cur = t.personal_rating || 0;
  const newRating = (cur === rating) ? null : rating;  // tap same star = clear
  try {
    await api(`/api/tracks/${t.pk}/rating`, { method: "PUT", body: JSON.stringify({ rating: newRating }) });
  } catch (e) { toast("Failed — try again"); return; }
  if (!t._ratedThisCard) { t._ratedThisCard = true; t._ratingBefore = cur || null; }
  t.personal_rating = newRating;
  toast(newRating ? ["", "Like", "Really like", "Love", "Perfect ✦"][newRating] : "Rating cleared");
  if (state.reviewSort === "newest" && state.vqMeta) {
    if (newRating && !cur) state.vqMeta.eligible_total = Math.max(0, (state.vqMeta.eligible_total || 1) - 1);
    else if (!newRating && cur) state.vqMeta.eligible_total = (state.vqMeta.eligible_total || 0) + 1;
  }
  renderVerdict();
}

// R10 + R14: playlist-chip removal on the Review card. When the chip was the
// track's ONLY playlist the toast offers a one-tap "Not for me" — the two
// intents often coincide but stay independent (no forced coupling).
async function vqRemovePl(pk, playlistId) {
  const t = vqCard();
  toast("Removing…");
  try {
    const r = await api(`/api/tracks/${pk}/playlists/${encodeURIComponent(playlistId)}/remove`,
                        { method: "POST", body: "{}" });
    const mine = t && t.pk === pk;
    const wasLast = mine && (t.playlists || []).length === 1;
    if (mine) {
      t.playlists = (t.playlists || []).filter(p => p.playlist_id !== playlistId);
      renderVerdict();
    }
    if (wasLast) {
      undoToast(`Removed from ${r.playlist_name} — its only playlist. Also not for me?`,
                () => vqDismiss(), "✗ Not for me");
    } else {
      undoToast(`Removed from ${r.playlist_name}`, async () => {
        await api(`/api/removals/${r.removal_id}/undo`, { method: "POST", body: "{}" });
        if (mine) { t.playlists.push({ playlist_id: playlistId, playlist_name: r.playlist_name }); renderVerdict(); }
        toast("Re-added");
      });
    }
  } catch (e) { alert("Remove failed: " + e.message); }
}

async function vqUnskipAll() {
  try { const r = await api("/api/verdict/unskip-all", { method: "POST", body: "{}" }); toast(`Re-served ${r.restored}`); }
  catch (e) {}
  loadVerdict();
}

function vqReveal() { state.vqGuessFirst = false; renderVerdict(); state.vqGuessFirst = localStorage.getItem("vqGuessFirst") === "1"; }
function vqToggleGuess(on) {
  state.vqGuessFirst = on; localStorage.setItem("vqGuessFirst", on ? "1" : "0"); renderVerdict();
}

function vqShowTiebreakers() {
  const rules = (state.vqTiebreakers || []).map(t =>
    `<p style="margin:8px 0"><b>${esc(t.pair)}</b><br><span class="hint" style="font-size:12.5px">${esc(t.rule)}</span></p>`).join("");
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.onclick = (ev) => { if (ev.target === bg) bg.remove(); };
  bg.innerHTML = `<div class="modal"><h2>Tie-breaker rules</h2>${rules || '<p class="hint">—</p>'}
    <div class="btnrow"><button class="primary" onclick="this.closest('.modal-bg').remove()">Got it</button></div></div>`;
  document.body.appendChild(bg);
}

// ── Skim controls (R4): judging "where in a set" needs intro/drop/outro, not
// real-time playback. Player-time readout lives on the card (eyes are there).
function vqSeek(d) {
  if (!ytReady || !ytPlayer.getDuration) return;
  ytPlayer.seekTo(Math.max(0, (ytPlayer.getCurrentTime() || 0) + d), true);
  vqTickClock();
}
function vqQuarter(dir) {
  if (!ytReady || !ytPlayer.getDuration) return;
  const dur = ytPlayer.getDuration() || 0; if (!dur) return;
  let q = Math.round(((ytPlayer.getCurrentTime() || 0) / dur) * 4) / 4 + dir * 0.25;
  q = Math.max(0, Math.min(0.75, q));
  ytPlayer.seekTo(q * dur, true);
  vqTickClock();
}
function vqSeekClick(e) {
  if (!ytReady || !ytPlayer.getDuration) return;
  const r = e.currentTarget.getBoundingClientRect();
  ytPlayer.seekTo(((e.clientX - r.left) / r.width) * (ytPlayer.getDuration() || 0), true);
  vqTickClock();
}
let vqClockTimer = null;
function vqStartClock() {
  if (!vqClockTimer) vqClockTimer = setInterval(vqTickClock, 500);
}
function vqTickClock() {
  if (state.view !== "review") return;
  // Keep the card's ▶/⏸ honest — playback state changes outside our renders
  // (pause via space, track end, plays started elsewhere).
  const btn = $("vq-playbtn");
  if (btn) {
    const sym = vqIsCardPlaying() ? "⏸" : "▶";
    if (btn.textContent !== sym) btn.textContent = sym;
  }
  const timeEl = $("vq-time"), fill = $("vq-pfill");
  const t = vqCard();
  if (!timeEl || !t || !ytReady || !ytPlayer.getDuration) return;
  if (state.vqPlayedPk !== t.pk) return;   // player is on some other track
  const dur = ytPlayer.getDuration() || (t.duration_ms || 0) / 1000;
  if (!dur) return;
  const cur = ytPlayer.getCurrentTime() || 0;
  timeEl.textContent = `${fmtTime(cur)} / ${fmtTime(dur)}`;
  if (fill) fill.style.width = `${Math.min(100, (cur / dur) * 100)}%`;
}

// Keyboard (R3): full coverage — chips included. Review view only; never while
// typing or with a modal open.
//   q w e r t y u i o = functional chips (set order) · a s d f g h j k l = personal
//   7 8 9 0 - = era chips (70s/80s/90s/00s/modern — the number-row run)
//   z/x/c/v = 1–4★ · 1/2/3 accept · ⇧1/2/3 reject · n = not for me · m = later
//   b = reveal (guess-first) · space = play/pause · ⏎ = commit
//   ←/→ = ±30s · ↑/↓ = quarter jumps
document.addEventListener("keydown", (e) => {
  if (state.view !== "review") return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" || document.querySelector(".modal-bg")) return;
  const t = vqCard(); if (!t) return;
  if (e.code === "Space") { e.preventDefault(); togglePlay(); return; }
  if (e.code === "Enter") { e.preventDefault(); vqCommit(); return; }
  if (e.code === "ArrowLeft") { e.preventDefault(); vqSeek(-30); return; }
  if (e.code === "ArrowRight") { e.preventDefault(); vqSeek(30); return; }
  if (e.code === "ArrowUp") { e.preventDefault(); vqQuarter(1); return; }
  if (e.code === "ArrowDown") { e.preventDefault(); vqQuarter(-1); return; }
  const digit = { Digit1: 0, Digit2: 1, Digit3: 2 }[e.code];
  if (digit !== undefined) {
    e.preventDefault();
    if (e.shiftKey) vqReject(digit); else vqAccept(digit);
    return;
  }
  const eraIdx = { Digit7: 0, Digit8: 1, Digit9: 2, Digit0: 3, Minus: 4 }[e.code];
  if (eraIdx !== undefined && !e.shiftKey) {
    const p = vqProfilesByLayer("era")[eraIdx];
    if (p) { e.preventDefault(); vqPickEra(p.tag_name); }
    return;
  }
  if (e.shiftKey) return;
  const k = e.key.toLowerCase();
  if (k === "n") { e.preventDefault(); vqDismiss(); return; }
  if (k === "m") { e.preventDefault(); vqSkip(); return; }
  if (k === "b") { if (state.vqGuessFirst) vqReveal(); return; }
  const star = { z: 1, x: 2, c: 3, v: 4 }[k];
  if (star) { e.preventDefault(); vqRate(star); return; }
  const fi = VQ_FKEYS.indexOf(k);
  if (fi > -1) {
    const p = vqProfilesByLayer("functional")[fi];
    if (p) { e.preventDefault(); vqPickFunc(p.tag_name); }
    return;
  }
  const pi = VQ_PKEYS.indexOf(k);
  if (pi > -1) {
    const p = vqProfilesByLayer("personal")[pi];
    if (p) { e.preventDefault(); vqTogglePersonal(p.tag_name); }
    return;
  }
});


