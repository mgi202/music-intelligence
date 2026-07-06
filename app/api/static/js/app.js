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
  vq: [], vqLoaded: false, vqIndex: 0, vqSel: null, vqReadiness: null, vqLastAdv: "",
  vqGuessFirst: localStorage.getItem("vqGuessFirst") === "1", vqRevealed: false,
  reviewSort: localStorage.getItem("reviewSort") === "newest" ? "newest" : "training",
  // Review refinement (2026-07-03): playlist filter (persisted), this-session
  // verdict log, readiness-bar flash targets. Session state resets on reload.
  vqPlFilter: localStorage.getItem("reviewPlaylist") || "",
  vqSession: [], vqBumped: [],
  // Suggestions "+ n more" expander (S1) — collapsed per card, tracked by pk.
  vqSuggExpanded: false, vqSuggCardPk: null,
  // Library facet: Dismissed ("Not for me") filter (R11)
  dismissed: false,
};

const $ = (id) => document.getElementById(id);
const esc = (s) => (s || "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Safe for a value embedded as a JS-string argument inside an inline
// onclick="fn('…')": the browser HTML-decodes the attribute first, THEN the JS
// parser runs, so a bare apostrophe (e.g. the era "early 10's") would close the
// string early and the handler would silently no-op. JS-escape (\ and ') first,
// then HTML-escape the result so the entity survives attribute decoding. Prefer
// data-attributes + delegation for new code; jsarg keeps existing inline
// handlers correct.
const jsarg = (s) => esc((s || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'"));

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

