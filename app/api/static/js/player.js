// ── Playback ─────────────────────────────
// Phone:   deep-link opens the YouTube Music app (audio engine).
// Desktop: official YouTube embed in the player bar.

// Desktop: a real queue player via the YouTube IFrame API (continuous play,
// auto-advance, transport controls, seek). Mobile: deep-link out (a web player
// can't background-play on Android — that wall is tracked separately).
let ytPlayer = null, ytReady = false, pendingPlay = null, pendingCue = null, progTimer = null, progTick = 0;

function onYouTubeIframeAPIReady() {
  ytPlayer = new YT.Player("ytplayer", {
    height: "158", width: "280",
    playerVars: { playsinline: 1, rel: 0 },
    events: {
      onReady: () => {
        ytReady = true;
        if (pendingPlay) { const p = pendingPlay; pendingPlay = null; loadQ(p.index); }
        else if (pendingCue) doCue();
      },
      onStateChange: onPlayerState,
      onError: onPlayerError,
    },
  });
}

// YouTube refuses a subset of tracks in the embed — most often YTM "Art Tracks"
// (the auto-generated "<artist> - Topic" audio uploads) which are licensed for
// YTM but not embeddable (error 101/150), plus removed/private (100) and bad
// id / HTML5 errors (2/5). Rather than leave YouTube's grey "Video unavailable"
// box, surface a one-tap "Open in YTM" (+ Skip when a next track exists).
async function onPlayerError(e) {
  const it = (state.queue || [])[state.qIndex];
  const vid = it && (it.playingId || it.videoId);
  if (it && it.pk && vid && !it._triedResolve) {
    it._triedResolve = true;                 // one auto-resolve per track
    try {
      const r = await fetch(`/api/tracks/${encodeURIComponent(it.pk)}/embed-failed`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ video_id: vid, error_code: e && e.data }),
      });
      if (r.ok) {
        const j = await r.json();
        if (j.resolved && j.video_id) {
          it.playingId = j.video_id;         // now the embeddable audio version
          hideUnavailable();
          ytPlayer.loadVideoById({ videoId: j.video_id });
          return;                            // played in-app, no grey box
        }
      }
    } catch (_) { /* fall through to the panel */ }
  }
  showUnavailable();
}

function showUnavailable() {
  const it = (state.queue || [])[state.qIndex]; if (!it) return;
  const vid = it.playingId || it.videoId;
  const box = $("pb-unavail");
  if (box) {
    $("pbu-ytm").href = `https://music.youtube.com/watch?v=${vid}`;
    $("pbu-skip").hidden = state.qIndex + 1 >= state.queue.length;
    box.hidden = false;
  }
  // Compact mode hides the video box, so mark the label + toast too. loadQ
  // rebuilds #np on the next track, which clears the marker on its own.
  if ($("np") && !$("np-unavail")) {
    $("np").insertAdjacentHTML("beforeend",
      ' <span id="np-unavail" style="color:var(--danger);font-size:11px">· not playable here</span>');
  }
  const hasNext = state.qIndex + 1 < (state.queue || []).length;
  toast(hasNext ? "Can't play here — Open in YTM, or press Next"
                : "Can't play here — Open in YTM");
}

function hideUnavailable() {
  const box = $("pb-unavail"); if (box) box.hidden = true;
  const w = $("np-unavail"); if (w) w.remove();
}

function onPlayerState(e) {
  if (e.data === YT.PlayerState.ENDED) { nextTrack(); return; }
  const btn = $("pb-play");
  if (e.data === YT.PlayerState.PLAYING) { hideUnavailable(); btn.innerHTML = '<i class="ti ti-player-pause"></i>'; startProg(); }
  else if (e.data === YT.PlayerState.PAUSED) { btn.innerHTML = '<i class="ti ti-player-play"></i>'; }
  else if (e.data === YT.PlayerState.CUED) {
    // Restored-after-refresh state: show the saved position, wait for a tap.
    btn.innerHTML = '<i class="ti ti-player-play"></i>';
    try {
      const d = ytPlayer.getDuration() || 0, c = ytPlayer.getCurrentTime() || 0;
      $("pb-cur").textContent = fmtTime(c); $("pb-dur").textContent = fmtTime(d);
      $("pb-fill").style.width = d ? `${(c / d) * 100}%` : "0";
    } catch (err) {}
  }
}

// ── Refresh persistence: the hero survives a page reload, paused at the
//    saved position (browsers block un-gestured audio autoplay). ──
function savePlayerState(pos) {
  try {
    if (!(state.queue || []).length || state.qIndex < 0) {
      localStorage.removeItem("playerState"); return;
    }
    localStorage.setItem("playerState", JSON.stringify({
      queue: state.queue, qIndex: state.qIndex, pos: pos || 0,
    }));
  } catch (e) {}
}

function restorePlayer() {
  if (state.isMobile) return;
  let s;
  try { s = JSON.parse(localStorage.getItem("playerState") || "null"); }
  catch (e) { return; }
  if (!s || !(s.queue || []).length) return;
  state.queue = s.queue;
  state.qIndex = Math.min(Math.max(s.qIndex, 0), s.queue.length - 1);
  const item = state.queue[state.qIndex];
  const vid = item.playingId || item.videoId;   // keep the video/audio choice made pre-refresh
  $("playerbar").classList.add("visible");
  $("np").innerHTML = `<b>${esc(item.label)}</b>` +
    (item.ext ? ' <span style="color:var(--accent);font-size:11px">· extended</span>'
     : vid !== item.videoId ? ' <span style="color:var(--accent);font-size:11px">· video</span>' : "");
  $("np-ytm").href = `https://music.youtube.com/watch?v=${vid}`;
  $("pb-art").src = `https://i.ytimg.com/vi/${vid}/mqdefault.jpg`;
  updateHeroMeta(item.pk);
  pendingCue = { videoId: vid, start: s.pos || 0 };
  if (ytReady) doCue();
}

function doCue() {
  if (!pendingCue || !ytReady) return;
  ytPlayer.cueVideoById({ videoId: pendingCue.videoId, startSeconds: pendingCue.start });
  pendingCue = null;
}

// Tracks visible in the current view, in display order — that's the queue.
function currentViewTracks() {
  return state.tracks || [];
}
// Prefer an explicitly-set playback id (e.g. an extended version), else the song.
function playableId(t) { return t.playback_video_id || t.ytm_track_id || null; }

// Track-row play button: start a continuous queue from this track through the
// rest of the visible list.
function playFromList(pk) {
  const list = currentViewTracks();
  const playable = list.filter(playableId);
  const idx = playable.findIndex(t => t.track_pk === pk);
  if (idx < 0) {
    const t = list.find(x => x.track_pk === pk);
    if (t) play(playableId(t), `${t.canonical_artist} — ${t.canonical_title}`, pk, !!t.playback_video_id);
    return;
  }
  // Playing a list REPLACES the persistent queue (queue = set crate, one
  // object). One-off single plays (the fallback above) leave the crate alone.
  state.pq = playable.slice();
  persistQueue(); renderQueue();
  _startQueue(playable.map(qItem), idx);
}

// Low-level single-item play (used by Home/now-playing/recent callers).
function play(videoId, label, pk, ext) {
  if (!videoId) return;
  _startQueue([{ videoId, label, pk: pk || null, ext: !!ext }], 0);
}

function _startQueue(queue, index) {
  if (state.isMobile) {
    window.location.href = `https://music.youtube.com/watch?v=${queue[index].videoId}`;
    return;
  }
  state.queue = queue; state.qIndex = index;
  $("playerbar").classList.add("visible");
  if (!ytReady) { pendingPlay = { index }; return; }
  loadQ(index);
}

// Prefer-videos toggle: play-time priority is pinned extended > official
// video (toggle on) > audio. Counterparts resolve lazily server-side, one
// YTM lookup per track ever; results cached here per session too.
function toggleVidPref() {
  state.preferVideos = !state.preferVideos;
  localStorage.setItem("preferVideos", state.preferVideos ? "1" : "0");
  $("pb-vidpref").classList.toggle("on", state.preferVideos);
  toast(state.preferVideos
    ? "Official videos on — applies from the next track"
    : "Back to pure audio from the next track");
}

async function resolveOfficial(pk) {
  if (!pk) return null;
  if (pk in state.ovCache) return state.ovCache[pk];
  try {
    const d = await api(`/api/tracks/${pk}/official-video`);
    state.ovCache[pk] = d.official_video_id || null;
  } catch (e) { return null; }   // transient — don't cache, retry next play
  return state.ovCache[pk];
}

async function loadQ(i) {
  if (i < 0 || i >= state.queue.length) return;
  hideUnavailable();   // clear any "can't play here" notice from the prior track
  state.qIndex = i;
  const item = state.queue[i];
  let vid = item.videoId;
  if (state.preferVideos && !item.ext) {
    const ov = await resolveOfficial(item.pk);
    if (state.queue[state.qIndex] !== item) return;   // user skipped mid-lookup
    if (ov) vid = ov;
  }
  item.playingId = vid;
  ytPlayer.loadVideoById(vid);
  $("np").innerHTML = `<b>${esc(item.label)}</b>` +
    (item.ext ? ' <span style="color:var(--accent);font-size:11px">· extended</span>'
     : vid !== item.videoId ? ' <span style="color:var(--accent);font-size:11px">· video</span>' : "");
  $("np-ytm").href = `https://music.youtube.com/watch?v=${vid}`;
  // Hero extras: art from the playing video's thumbnail (no storage needed),
  // stars + tag row fetched per track change.
  $("pb-art").src = `https://i.ytimg.com/vi/${vid}/mqdefault.jpg`;
  updateHeroMeta(item.pk);
  highlightPlaying(item.pk);
  renderQueue();
  savePlayerState(0);
  // Prefetch the next track's counterpart so auto-advance stays gapless.
  const nx = state.queue[i + 1];
  if (state.preferVideos && nx && !nx.ext) resolveOfficial(nx.pk);
}

async function updateHeroMeta(pk) {
  $("pb-stars").innerHTML = ""; $("pb-tags").innerHTML = "";
  if (!pk) return;
  try {
    const t = await api(`/api/tracks/${pk}`);
    if (currentPlayingPk() !== pk) return;   // user skipped while we fetched
    $("pb-stars").innerHTML = starButtons(t);
    const tags = (t.tags || []).map(g =>
      `<span class="tag ${g.tag_type === "private_manual" ? "manual" : "auto"}">${esc(g.tag)}</span>`).join("");
    $("pb-tags").innerHTML = tags + `<button class="addtag" onclick="addTag('${pk}')">+ tag</button>`;
  } catch (e) {}
}

function expandVideo() {
  const w = $("ytplayer-wrap");
  if (document.fullscreenElement) document.exitFullscreen();
  else if (w.requestFullscreen) w.requestFullscreen();
}

function nextTrack() {
  if (state.qIndex < (state.queue || []).length - 1) loadQ(state.qIndex + 1);
}
function prevTrack() {
  if (ytReady && ytPlayer.getCurrentTime && ytPlayer.getCurrentTime() > 3) { ytPlayer.seekTo(0, true); return; }
  if (state.qIndex > 0) loadQ(state.qIndex - 1);
}
function togglePlay() {
  if (!ytReady) return;
  const s = ytPlayer.getPlayerState();
  if (s === YT.PlayerState.PLAYING) ytPlayer.pauseVideo(); else ytPlayer.playVideo();
}

function startProg() {
  if (progTimer) return;
  progTimer = setInterval(() => {
    if (!ytReady || !ytPlayer.getDuration) return;
    const d = ytPlayer.getDuration() || 0, c = ytPlayer.getCurrentTime() || 0;
    $("pb-fill").style.width = d ? `${(c / d) * 100}%` : "0";
    $("pb-cur").textContent = fmtTime(c);
    $("pb-dur").textContent = fmtTime(d);
    if (++progTick % 10 === 0) savePlayerState(c);   // persist position ~every 5s
  }, 500);
}
function fmtTime(s) { s = Math.floor(s || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }

function highlightPlaying(pk) {
  document.querySelectorAll(".track.playing, .midrow.playing").forEach(el => el.classList.remove("playing"));
  if (pk) {
    const el = document.getElementById("tr-" + pk); if (el) el.classList.add("playing");
    const mr = document.getElementById("mr-" + pk); if (mr) mr.classList.add("playing");
  }
}

function closePlayer() {
  if (ytReady && ytPlayer.stopVideo) ytPlayer.stopVideo();
  if (progTimer) { clearInterval(progTimer); progTimer = null; }
  state.queue = []; state.qIndex = -1;
  $("playerbar").classList.remove("visible");
  $("pb-art").removeAttribute("src");
  $("pb-stars").innerHTML = ""; $("pb-tags").innerHTML = "";
  localStorage.removeItem("playerState");
  highlightPlaying(null);
  renderQueue();   // drop the 'current' ring; the crate itself persists
}

