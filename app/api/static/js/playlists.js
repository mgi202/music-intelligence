// ── Source-playlist editing: cull tracks from your own YTM playlists ──
function refreshActiveView() {
  if (state.view === "library") loadTracks();
  else if (state.view === "home") loadHome();
}

function undoToast(msg, onUndo, actionLabel = "Undo") {
  const t = $("toast");
  clearTimeout(toastTimer);   // cancel any pending auto-hide from an earlier toast
  t.innerHTML = `${esc(msg)} <button class="undobtn" id="undobtn">${esc(actionLabel)}</button>`;
  t.style.pointerEvents = "auto";   // the toast normally ignores taps — let this one catch them
  t.classList.add("show");
  const hide = () => { t.classList.remove("show"); t.style.pointerEvents = ""; clearTimeout(toastTimer); };
  $("undobtn").onclick = async () => {
    hide();
    try { await onUndo(); } catch (e) { alert("Undo failed: " + e.message); }
  };
  toastTimer = setTimeout(hide, 6000);
}

// Tap a playlist chip's name → filter the Library to that playlist.
function filterPlaylist(playlistId) {
  state.sourcePlaylist = playlistId;
  if (state.view !== "library") switchView("library");   // switchView reloads + syncs dropdown
  else { loadSourcePlaylists(); loadTracks(); }
}

// Tap a chip's × → remove the track from that YTM playlist, with one-tap Undo.
async function removeFromPlaylist(pk, playlistId) {
  toast("Removing…");
  try {
    const r = await api(`/api/tracks/${pk}/playlists/${encodeURIComponent(playlistId)}/remove`,
                        { method: "POST", body: "{}" });
    refreshActiveView();
    undoToast(`Removed from ${r.playlist_name}`, async () => {
      await api(`/api/removals/${r.removal_id}/undo`, { method: "POST", body: "{}" });
      refreshActiveView(); toast("Re-added");
    });
  } catch (e) { alert("Remove failed: " + e.message); }
}

async function removeFromAllPlaylists(pk) {
  if (!confirm("Remove this track from ALL your YTM playlists?\n\nIt's also blocked from auto-generated playlists so your rules won't re-add it.")) return;
  toast("Removing from all…");
  try {
    const r = await api(`/api/tracks/${pk}/playlists/remove-all`, { method: "POST", body: "{}" });
    refreshActiveView();
    const n = (r.removed || []).length, f = (r.failed || []).length;
    toast(`Removed from ${n} playlist${n !== 1 ? "s" : ""}${f ? ` · ${f} read-only skipped` : ""}`);
  } catch (e) { alert("Failed: " + e.message); }
}

// ── Playlists ────────────────────────────

async function loadPlaylists() {
  const rules = await api("/api/playlists");
  $("playlists").innerHTML =
    `<button class="newpl" onclick="editRule(null)">+ New playlist rule</button>` +
    (rules.length ? rules.map(r => {
      const e = r.rule_json.eligibility || {};
      const desc = [
        e.tags_any?.length ? "any of: " + e.tags_any.join(", ") : "",
        e.tags_all?.length ? "all of: " + e.tags_all.join(", ") : "",
        e.tags_none?.length ? "none of: " + e.tags_none.join(", ") : "",
      ].filter(Boolean).join(" · ") || "all tracks";
      return `
      <div class="pl">
        <div class="name">${esc(r.playlist_name)} ${r.enabled ? "" : "· <span style='color:var(--dim)'>disabled</span>"}</div>
        <div class="sub">${r.ranking_mode} · ${desc}${r.max_tracks ? " · max " + r.max_tracks : ""}
          ${r.last_synced_at ? "<br>last synced " + r.last_synced_at.slice(0, 16).replace("T", " ") : "<br>never synced"}</div>
        <div class="health" id="health-${r.rule_id}">…</div>
        <div class="actions">
          <button onclick="previewRule('${r.rule_id}', '${esc(r.playlist_name)}')">Preview · Why?</button>
          <button class="primary" onclick="syncRule('${r.rule_id}')">Sync to YTM</button>
          <button onclick="showSnapshots('${r.rule_id}', '${esc(r.playlist_name)}')">Undo…</button>
          <button onclick='editRule(${JSON.stringify(r).replace(/'/g, "&#39;")})'>Edit</button>
          <button class="danger" onclick="deleteRule('${r.rule_id}')">Delete</button>
        </div>
      </div>`;
    }).join("") : '<div class="empty">No playlist rules yet.</div>');
  rules.forEach(r => loadHealth(r.rule_id));
}

async function loadHealth(ruleId) {
  const el = $(`health-${ruleId}`);
  if (!el) return;
  try {
    const h = await api(`/api/playlists/${ruleId}/health`);
    const bits = [`${h.compiled_count} tracks`];
    if (h.average_rating != null) bits.push(`avg ★${h.average_rating}`);
    if (h.unrated_count) bits.push(`${h.unrated_count} unrated`);
    if (h.weak_metadata_count) bits.push(`${h.weak_metadata_count} weak meta`);
    if (h.missing_from_ytm_count) bits.push(`${h.missing_from_ytm_count} missing`);
    if (h.pending_dedup_count) bits.push(`${h.pending_dedup_count} dup?`);
    if (h.held) { el.classList.add("held"); el.textContent = "⚠ HELD — " + h.sync_held_reason + " · " + bits.join(" · "); }
    else el.textContent = bits.join(" · ");
  } catch (e) { el.textContent = ""; }
}

async function showSnapshots(ruleId, name) {
  const snaps = await api(`/api/playlists/${ruleId}/snapshots`);
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.onclick = (ev) => { if (ev.target === bg) bg.remove(); };
  bg.innerHTML = `<div class="modal"><h2>${esc(name)} — snapshots</h2>
    ${snaps.length ? snaps.map(s => `<div class="pl">
        <div class="sub">${s.taken_at.slice(0,16).replace("T"," ")} · ${s.reason} · ${s.track_count} tracks</div>
        <div class="actions"><button class="primary" onclick="restoreSnap(${s.snapshot_id})">Restore this</button></div>
      </div>`).join("") : '<div class="empty">No snapshots yet.</div>'}
    <div class="btnrow"><button onclick="this.closest('.modal-bg').remove()">Close</button></div>
  </div>`;
  document.body.appendChild(bg);
}
async function restoreSnap(id) {
  if (!confirm("Restore this snapshot? Current state is snapshotted first so this is undoable.")) return;
  try { await api(`/api/snapshots/${id}/restore`, { method: "POST", body: "{}" }); toast("Restored ✓"); document.querySelector(".modal-bg")?.remove(); loadPlaylists(); }
  catch (e) { alert("Restore failed: " + e.message); }
}

function editRule(rule) {
  const e = rule?.rule_json?.eligibility || {};
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.onclick = (ev) => { if (ev.target === bg) bg.remove(); };
  bg.innerHTML = `
    <div class="modal">
      <h2>${rule ? "Edit" : "New"} playlist rule</h2>
      <label>Playlist name</label>
      <input id="m-name" value="${esc(rule?.playlist_name || "")}" placeholder="Late Night Drive">
      <label>Ranking mode</label>
      <select id="m-mode">
        ${["mood","dj_mix","discovery","utility"].map(m =>
          `<option ${rule?.ranking_mode === m ? "selected" : ""}>${m}</option>`).join("")}
      </select>
      <label>Must have ANY of these tags</label>
      <input id="m-any" value="${esc((e.tags_any || []).join(", "))}" placeholder="dub-techno, deep-techno">
      <div class="hint">comma-separated</div>
      <label>Must have ALL of these tags</label>
      <input id="m-all" value="${esc((e.tags_all || []).join(", "))}">
      <label>Must have NONE of these tags</label>
      <input id="m-none" value="${esc((e.tags_none || []).join(", "))}">
      <label>Minimum rating (optional)</label>
      <select id="m-minrating">
        <option value="">—</option>
        ${[1,2,3,4].map(n => `<option value="${n}" ${rule?.rule_json?.eligibility?.min_rating === n ? "selected" : ""}>${"★".repeat(n)}+</option>`).join("")}
      </select>
      <label>Max tracks (optional)</label>
      <input id="m-max" type="number" value="${rule?.max_tracks || ""}" placeholder="50">
      <div class="btnrow">
        <button onclick="this.closest('.modal-bg').remove()">Cancel</button>
        <button class="primary" id="m-save">Save</button>
      </div>
    </div>`;
  document.body.appendChild(bg);
  $("m-save").onclick = async () => {
    const split = (id) => $(id).value.split(",").map(s => s.trim().toLowerCase()).filter(Boolean);
    const eligibility = { tags_any: split("m-any"), tags_all: split("m-all"), tags_none: split("m-none") };
    const minR = $("m-minrating").value;
    if (minR) eligibility.min_rating = parseInt(minR);
    const body = {
      playlist_name: $("m-name").value.trim(),
      ranking_mode: $("m-mode").value,
      rule_json: { eligibility },
      max_tracks: $("m-max").value ? parseInt($("m-max").value) : null,
      enabled: true,
    };
    if (!body.playlist_name) return alert("Name required");
    await api(rule ? `/api/playlists/${rule.rule_id}` : "/api/playlists",
              { method: rule ? "PUT" : "POST", body: JSON.stringify(body) });
    bg.remove(); loadPlaylists(); toast("Saved");
  };
}

async function previewRule(ruleId, name) {
  const data = await api(`/api/playlists/${ruleId}/preview`);
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.onclick = (ev) => { if (ev.target === bg) bg.remove(); };
  bg.innerHTML = `<div class="modal"><h2>${esc(name)} — ${data.tracks.length} tracks</h2>
    ${data.tracks.length ? data.tracks.map((t, i) => `
      <div class="track">
        <div class="row1">
          <div class="meta" onclick="this.closest('.track').querySelector('.evidence').style.display = this.closest('.track').querySelector('.evidence').style.display==='none'?'block':'none'">
            <div class="title">${i + 1}. ${esc(t.canonical_title)}${t.personal_rating ? " " + "★".repeat(t.personal_rating) : ""}</div>
            <div class="artist">${esc(t.canonical_artist)} · score ${t.score} <span style="color:var(--accent)">— Why?</span></div>
          </div>
        </div>
        <div class="evidence" style="display:none">${evidenceHtml(t.evidence)}</div>
      </div>`).join("") : '<div class="empty">No tracks match.</div>'}
    <div class="btnrow"><button onclick="this.closest('.modal-bg').remove()">Close</button></div>
  </div>`;
  document.body.appendChild(bg);
}

function evidenceHtml(ev) {
  if (!ev) return "";
  const comps = ev.score_components || {};
  const max = Math.max(0.0001, ...Object.values(comps).map(Math.abs));
  const bars = Object.entries(comps).map(([k, v]) =>
    `<div>${esc(k)} <b>${(+v).toFixed(3)}</b></div><div class="bar"><span style="width:${Math.max(0, v / max * 100)}%"></span></div>`
  ).join("");
  const m = ev.matched || {};
  const matched = [
    m.tags_any && m.tags_any.length ? "tags: " + m.tags_any.join(", ") : "",
    m.tags_all && m.tags_all.length ? "all: " + m.tags_all.join(", ") : "",
    m.min_rating ? "min ★" + m.min_rating : "",
    "status: " + (m.status || "?"),
  ].filter(Boolean).join(" · ");
  return `<div><b>Score components</b></div>${bars}<div style="margin-top:6px">${esc(matched)}</div>`;
}

async function syncRule(ruleId) {
  toast("Syncing…");
  try { await api(`/api/playlists/${ruleId}/sync`, { method: "POST", body: "{}" }); toast("Synced to YTM ✓"); loadPlaylists(); }
  catch (err) { alert("Sync failed: " + err.message); }
}

async function deleteRule(ruleId) {
  if (!confirm("Delete this rule? (The YTM playlist itself is not deleted.)")) return;
  await api(`/api/playlists/${ruleId}`, { method: "DELETE" });
  loadPlaylists();
}

