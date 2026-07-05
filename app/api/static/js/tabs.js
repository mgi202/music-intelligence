// ── Tabs ──
$("tab-home").onclick = () => switchView("home");
$("tab-library").onclick = () => switchView("library");
$("tab-review").onclick = () => switchView("review");
$("tab-playlists").onclick = () => switchView("playlists");
$("tab-matches").onclick = () => switchView("matches");
$("tab-tags").onclick = () => switchView("tags");
$("tab-training").onclick = () => switchView("training");

// Seek by clicking the player progress bar.
$("pb-track").onclick = (e) => {
  if (!ytReady || !ytPlayer.getDuration) return;
  const r = e.currentTarget.getBoundingClientRect();
  ytPlayer.seekTo(((e.clientX - r.left) / r.width) * (ytPlayer.getDuration() || 0), true);
};

const VIEWS = ["home", "library", "review", "playlists", "matches", "tags", "training"];
function switchView(v) {
  state.view = v;
  VIEWS.forEach(name => {
    $(name).style.display = name === v ? "" : "none";
    const tab = $("tab-" + name);
    if (tab) tab.classList.toggle("active", name === v);
  });
  // Library-only header controls: search, facet row, summary bar.
  // The ⟳ reload button stays visible on every tab.
  const lib = v === "library";
  $("search").style.display = lib ? "" : "none";
  $("facetrow").style.display = lib ? "" : "none";
  $("sumbar").style.display = lib ? "" : "none";
  // Home-only compact search. The player hero renders only on Home — on
  // other tabs it's hidden but audio keeps playing (locked decision).
  $("homesearch").style.display = v === "home" ? "" : "none";
  $("playerbar").classList.toggle("hero-mode", v === "home");
  if (v === "home") { loadHome(); startHomePolling(); } else { stopHomePolling(); }
  if (v === "library") { loadTracks(); loadSourcePlaylists(); loadGrandTotal(); renderFacetRow(); }
  if (v === "review") loadVerdict();
  if (v === "playlists") loadPlaylists();
  if (v === "matches") loadMatches();
  if (v === "tags") loadTags();
  if (v === "training") loadTraining();
}

