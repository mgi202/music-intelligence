const state = {
  q: "", rating: "", untagged: false, tags: [], tagMode: "and", sourcePlaylist: "", playlistSort: "name_asc", pendingVersions: false,
  sort: "added_desc", grandTotal: 0,
  offset: 0, limit: 100, total: 0,
  tracks: [], allTags: [], vocab: [], sourcePlaylists: [],
  matchesSub: "duplicates", reviewPairs: [], view: "home",
  // Home hub state
  plTree: [], treeExpanded: false, homeNode: null, homeTracks: [],
  midTitle: "", midSub: "", pq: [], qMirror: null,
  preferVideos: localStorage.getItem("preferVideos") === "1", ovCache: {},
  isMobile: /Android|iPhone|iPad/i.test(navigator.userAgent),
  queue: [], qIndex: -1,
  // Review queue (merged Inbox + Verdict surface)
  vq: [], vqIndex: 0, vqSel: null, vqReadiness: null, vqLastAdv: "",
  vqGuessFirst: localStorage.getItem("vqGuessFirst") === "1", vqRevealed: false,
  reviewSort: localStorage.getItem("reviewSort") === "newest" ? "newest" : "training",
  // Review refinement (2026-07-03): playlist filter (persisted), this-session
  // verdict log, readiness-bar flash targets. Session state resets on reload.
  vqPlFilter: localStorage.getItem("reviewPlaylist") || "",
  vqSession: [], vqBumped: [],
  // Library facet: Dismissed ("Not for me") filter (R11)
  dismissed: false,
};

const $ = (id) => document.getElementById(id);
const esc = (s) => (s || "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// One shared timer so a quick toast (e.g. "Removing…") can't auto-hide a later
// toast/undo that replaced it. Both toast() and undoToast() clear it first.
let toastTimer;
function toast(msg) {
  const t = $("toast"); clearTimeout(toastTimer);
  t.style.pointerEvents = "";   // plain toasts stay click-through
  t.textContent = msg; t.classList.add("show");
  toastTimer = setTimeout(() => t.classList.remove("show"), 1600);
}

async function api(path, opts) {
  const r = await fetch(path, opts ? { headers: {"Content-Type":"application/json"}, ...opts } : undefined);
  if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || r.statusText); }
  return r.json();
}

