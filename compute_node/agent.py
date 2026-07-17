"""
Mac compute-node agent (Phase 3 — Ears).

Claims lawful audio-extraction jobs from the VPS over Tailscale, downloads
each source to a TEMP file (yt-dlp, 128 kbps M4A), extracts the LOCKED
measurement set (base-Essentia signal descriptors, Essentia-TensorFlow model
heads, structure segmentation, Camelot key via keyfinder-cli, 512-dim CLAP
embedding), posts the result back, and DELETES the audio in a finally block.
Audio is a temporary compute artefact — nothing audio-shaped survives a job,
succeed or fail. A job that cannot delete its temp file fails loudly and
stops the loop.

Runs on demand inside the Docker container (see compute_node/README.md for
the one-liner) or, degraded, in a bare venv without the TF stack.

Environment:
  MIS_SERVER            e.g. http://100.77.32.111:8080  (required)
  AUDIO_NODE_TOKEN      shared secret, must match the server .env (required)
  MIS_BATCH             jobs per claim (default 4)
  MIS_REPROCESS         "1" = claim stale-vector reprocess jobs instead
  MIS_MODELS_DIR        dir holding essentia/*.pb(+.json) and clap/*.pt —
                        without it the TF model stage is skipped (honest NULLs)
  MIS_PROXY             SOCKS proxy for SERVER calls only (in-container
                        tailscaled); downloads stay on the direct route so
                        yt-dlp egresses via the home connection

This file is NEVER imported by the server or tests — the heavy ML stack
(torch/CLAP/Essentia) exists only in the container image / Mac venv.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

SERVER = os.environ.get("MIS_SERVER", "").rstrip("/")
TOKEN = os.environ.get("AUDIO_NODE_TOKEN", "")
BATCH = int(os.environ.get("MIS_BATCH", "4"))
REPROCESS = os.environ.get("MIS_REPROCESS", "0") == "1"
PACE_SECONDS = float(os.environ.get("MIS_PACE_SECONDS", "5"))
MODELS_DIR = os.environ.get("MIS_MODELS_DIR", "")
PROXY = os.environ.get("MIS_PROXY", "")

# 2.0 (2026-07-13): locked measurement set + Essentia-TF model heads.
# valence/arousal come from the emomusic regression head (record here so a
# future dataset switch is visible in extractor_version).
EXTRACTOR_VERSION = "mis-compute-node/2.0 (av=emomusic)"
CLAP_MODEL = "laion/clap-htsat-fused"
CLAP_CKPT_NAME = "630k-audioset-fusion-best.pt"

# Essentia model manifest (pinned 2026-07-13 from essentia.upf.edu/models).
# Every head is paired with the backbone it was trained on — mixing produces
# silent garbage. One EffNet-Discogs embedding serves all heads except
# valence/arousal, whose emomusic head only exists for MusiCNN (the justified
# second backbone, 3 MB). Bump ESSENTIA_MODELS_VERSION on ANY change here —
# the server's stale-first policy keys on it.
ESSENTIA_MODELS_VERSION = "effnet-bs64-1+heads-1+emomusic-msd-musicnn-2"
EMBEDDING_MODELS = {
    "effnet": "discogs-effnet-bs64-1.pb",
    "musicnn": "msd-musicnn-1.pb",
}
# head key: (model file, backbone, kind, target)
#   kind 'binary'      → mean softmax P(positive class)
#   kind 'multilabel'  → mean sigmoid activations per class
#   kind 'regression'  → mean scalar 0..1
#   kind 'regression2d'→ mean [valence, arousal] on the 1..9 emomusic scale
# target ('feature', name) writes an audio_features column;
# target ('prediction', group) feeds the audio_inferred tag pipeline.
HEAD_MODELS = {
    "mood_acoustic": ("mood_acoustic-discogs-effnet-1.pb", "effnet",
                      "binary", ("feature", "acousticness")),
    "voice_instrumental": ("voice_instrumental-discogs-effnet-1.pb", "effnet",
                           "binary", ("feature", "instrumentalness")),
    "mood_happy": ("mood_happy-discogs-effnet-1.pb", "effnet",
                   "binary", ("prediction", "mood")),
    "mood_sad": ("mood_sad-discogs-effnet-1.pb", "effnet",
                 "binary", ("prediction", "mood")),
    "mood_aggressive": ("mood_aggressive-discogs-effnet-1.pb", "effnet",
                        "binary", ("prediction", "mood")),
    "mood_relaxed": ("mood_relaxed-discogs-effnet-1.pb", "effnet",
                     "binary", ("prediction", "mood")),
    "mood_party": ("mood_party-discogs-effnet-1.pb", "effnet",
                   "binary", ("prediction", "mood")),
    "genre": ("genre_discogs400-discogs-effnet-1.pb", "effnet",
              "multilabel", ("prediction", "genre")),
    "moodtheme": ("mtg_jamendo_moodtheme-discogs-effnet-1.pb", "effnet",
                  "multilabel", ("prediction", "moodtheme")),
    "instrument": ("mtg_jamendo_instrument-discogs-effnet-1.pb", "effnet",
                   "multilabel", ("prediction", "instrument")),
    "approachability": ("approachability_regression-discogs-effnet-1.pb",
                        "effnet", "regression", ("feature", "approachability")),
    "engagement": ("engagement_regression-discogs-effnet-1.pb", "effnet",
                   "regression", ("feature", "engagement")),
    "emomusic": ("emomusic-msd-musicnn-2.pb", "musicnn",
                 "regression2d", ("feature", "valence+arousal")),
}
# NOTE speechiness: the Essentia model zoo has no speech/music discriminator
# (voice_instrumental separates singing from instrumental, which is a
# different question). Per the locked rule — honest NULL, never a proxy —
# speechiness stays NULL until a clean model exists.

# Cap the per-group probabilities shipped for audit — full genre400 output
# would be ~10 KB of JSON per track for labels that are all ≈0.
PREDICTION_TOP_N = {"genre": 20, "moodtheme": 20, "instrument": 15, "mood": 5}

_clap = None        # lazy singleton — model load takes ~30s
_tf_stack = None    # lazy singleton — essentia-TF graphs, loaded once


def die(msg: str) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def api(path: str, payload: dict | None = None, method: str = "POST") -> dict:
    """Server call with bounded retries on transient failures.

    A multi-day session WILL see the server restart under it (first run
    2026-07-17: a mid-deploy 502 killed the whole agent) — connection errors
    and 5xx get exponential backoff, up to ~8.5 min total. 4xx stays fatal:
    an auth or contract error must fail loudly, not loop.
    """
    headers = {"X-Audio-Node-Token": TOKEN}
    url = f"{SERVER}{path}"
    # Server calls go via the tailnet proxy when configured; downloads never do.
    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    delays = (5, 15, 30, 60, 120, 240)     # ~8.5 min of server downtime covered
    for attempt, delay in enumerate((*delays, None)):
        try:
            if method == "GET":
                r = requests.get(url, headers=headers, timeout=60,
                                 proxies=proxies)
            else:
                r = requests.post(url, json=payload, headers=headers,
                                  timeout=120, proxies=proxies)
            if r.status_code >= 500 and delay is not None:
                raise requests.ConnectionError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            if delay is None:
                raise
            print(f"  ~ server hiccup on {path} ({e}) — retry in {delay}s "
                  f"[{attempt + 1}/{len(delays)}]")
            time.sleep(delay)


# ─────────────────────────────────────────────────────────────────────────────
# Download (lawful sources only — the server's claim already gated on
# lawful_basis; this agent never invents its own URLs)
# ─────────────────────────────────────────────────────────────────────────────

def download(source_url: str, tmpdir: str) -> str:
    """Fetch the source to a temp M4A/audio file. Returns the file path."""
    out_tmpl = str(Path(tmpdir) / "audio.%(ext)s")
    if source_url.lower().endswith((".m4a", ".mp3", ".ogg", ".wav", ".aac")):
        # Direct file URL (e.g. iTunes preview) — plain HTTP fetch.
        ext = source_url.rsplit(".", 1)[-1].split("?")[0]
        path = str(Path(tmpdir) / f"audio.{ext}")
        with requests.get(source_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        return path
    # Page URL (e.g. Bandcamp track page) — yt-dlp, audio only, 128 kbps M4A.
    import yt_dlp
    opts = {
        "format": "bestaudio/best",
        "outtmpl": out_tmpl,
        "quiet": True,
        "noplaylist": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
            "preferredquality": "128",
        }],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([source_url])
    files = sorted(Path(tmpdir).glob("audio.*"))
    if not files:
        raise RuntimeError("yt-dlp produced no output file")
    return str(files[0])


# ─────────────────────────────────────────────────────────────────────────────
# Feature extraction — Essentia preferred, librosa fallback, honest Nones
# ─────────────────────────────────────────────────────────────────────────────

_CAMELOT = {  # (tonic, 'major'|'minor') -> camelot
    ("B", "major"): "1B", ("F#", "major"): "2B", ("C#", "major"): "3B",
    ("G#", "major"): "4B", ("D#", "major"): "5B", ("A#", "major"): "6B",
    ("F", "major"): "7B", ("C", "major"): "8B", ("G", "major"): "9B",
    ("D", "major"): "10B", ("A", "major"): "11B", ("E", "major"): "12B",
    ("G#", "minor"): "1A", ("D#", "minor"): "2A", ("A#", "minor"): "3A",
    ("F", "minor"): "4A", ("C", "minor"): "5A", ("G", "minor"): "6A",
    ("D", "minor"): "7A", ("A", "minor"): "8A", ("E", "minor"): "9A",
    ("B", "minor"): "10A", ("F#", "minor"): "11A", ("C#", "minor"): "12A",
}
_ENHARMONIC = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}

FEATURE_KEYS = (
    "bpm", "bpm_confidence", "musical_key", "musical_scale", "camelot_key",
    "valence", "arousal", "danceability", "energy", "acousticness",
    "instrumentalness", "loudness_lufs", "dynamic_range", "speechiness",
    "onset_rate", "key_strength", "dissonance", "spectral_centroid",
    "approachability", "engagement",
)


def to_camelot(tonic: str, scale: str) -> str | None:
    tonic = _ENHARMONIC.get(tonic, tonic)
    return _CAMELOT.get((tonic, scale.lower()))


def keyfinder_key(path: str) -> tuple[str, str, str] | None:
    """(tonic, scale, version) via keyfinder-cli, or None if unavailable."""
    try:
        out = subprocess.run(
            ["keyfinder-cli", "-n", "standard", path],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode != 0:
            return None
        raw = out.stdout.strip()          # e.g. "Ab minor" or "C major"
        parts = raw.split()
        if len(parts) == 2 and parts[1].lower() in ("major", "minor"):
            return parts[0], parts[1].lower(), "keyfinder-cli"
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _warn(stage: str, e: Exception) -> None:
    print(f"    ! {stage} failed ({type(e).__name__}: {e}) — honest NULL")


def compute_structure(y, sr: int, beat_conf: float | None) -> dict | None:
    """Locked measures 15/16 — intro/outro/first-drop/breakdown + energy curve
    from a smoothed RMS envelope. Deliberately simple v1 heuristics (0.5s
    windows, 2s smoothing); structure_confidence stays a modest 0.5 so nothing
    downstream over-trusts them."""
    import numpy as np
    hop = int(sr * 0.5)                       # 0.5 s windows
    n = len(y) // hop
    if n < 16:                                # < 8 s of audio — no structure
        return None
    rms = np.sqrt(np.mean(np.square(
        y[: n * hop].reshape(n, hop).astype("float64")), axis=1))
    k = 4                                     # 2 s moving average
    sm = np.convolve(rms, np.ones(k) / k, mode="same")
    peak = float(np.percentile(sm, 95))
    if peak <= 0:
        return None
    e = np.clip(sm / peak, 0.0, 1.0)

    def first_sustained(arr, thresh, frames):
        run = 0
        for i, v in enumerate(arr):
            run = run + 1 if v >= thresh else 0
            if run >= frames:
                return i - frames + 1
        return None

    i_in = first_sustained(e, 0.5, 4)
    intro_seconds = round(i_in * 0.5, 1) if i_in is not None else None
    i_out = first_sustained(e[::-1], 0.5, 4)
    outro_seconds = round(i_out * 0.5, 1) if i_out is not None else None

    # Breakdowns: sustained (≥6 s) low-energy stretches in the middle 60%.
    breakdown_count, run = 0, 0
    for i in range(int(n * 0.2), int(n * 0.8)):
        if e[i] < 0.4:
            run += 1
        else:
            if run >= 12:
                breakdown_count += 1
            run = 0
    if run >= 12:
        breakdown_count += 1

    # First drop: a sharp (+0.25 in ≤1 s) rise after ≥4 s below median energy,
    # within the first 60% of the track. None when the track never does that.
    med = float(np.median(e))
    first_drop_seconds = None
    low_run = 0
    for i in range(2, int(n * 0.6)):
        low_run = low_run + 1 if e[i - 1] < med else 0
        if low_run >= 8 and e[i] - e[i - 2] > 0.25:
            first_drop_seconds = round(i * 0.5, 1)
            break

    x = np.linspace(0.0, 1.0, n)
    slope = float(np.polyfit(x, e, 1)[0])
    h = 8                                     # 4 s change horizon
    diff = e[h:] - e[:-h]
    return {
        "intro_seconds": intro_seconds,
        "outro_seconds": outro_seconds,
        "breakdown_count": breakdown_count,
        "first_drop_seconds": first_drop_seconds,
        "peak_energy_position": round(float(np.argmax(sm)) / n, 3),
        "energy_stability": round(
            float(np.clip(1.0 - np.std(e) / (np.mean(e) + 1e-9), 0.0, 1.0)), 3),
        "energy_slope_signed": round(float(np.tanh(slope)), 3),
        "energy_rise_score": round(float(np.clip(diff.max(), 0, 1)), 3),
        "energy_drop_score": round(float(np.clip(-diff.min(), 0, 1)), 3),
        "beat_grid_confidence": beat_conf,
        "structure_confidence": 0.5,
    }


def extract_features(path: str) -> tuple[dict, dict, dict]:
    """(features, versions, extras). Prefers Essentia; degrades to librosa;
    every unavailable signal is an honest None, never a guess. extras carries
    the non-scalar locked measures: beat_positions, chords, hpcp, structure."""
    features: dict = {c: None for c in FEATURE_KEYS}
    versions: dict = {"extractor": EXTRACTOR_VERSION}
    extras: dict = {"beat_positions": None, "chords": None, "hpcp": None,
                    "structure": None}

    essentia_ok = False
    try:
        import essentia
        import essentia.standard as es
        import numpy as np
        versions["essentia"] = essentia.__version__
        audio = es.MonoLoader(filename=path, sampleRate=44100)()

        try:
            bpm, ticks, conf, _, _ = es.RhythmExtractor2013(
                method="multifeature")(audio)
            features["bpm"] = round(float(bpm), 2)
            features["bpm_confidence"] = round(float(conf) / 5.32, 3)  # 0..1
            extras["beat_positions"] = [round(float(t), 3) for t in ticks]
        except Exception as e:  # noqa: BLE001
            _warn("rhythm", e)

        try:
            key, scale, kconf = es.KeyExtractor()(audio)
            features["musical_key"], features["musical_scale"] = key, scale
            features["key_strength"] = round(float(kconf), 3)
        except Exception as e:  # noqa: BLE001
            _warn("key", e)

        try:
            features["danceability"] = round(
                min(1.0, float(es.Danceability()(audio)[0]) / 3.0), 3)
        except Exception as e:  # noqa: BLE001
            _warn("danceability", e)
        features["energy"] = round(
            min(1.0, float((audio ** 2).mean()) * 20.0), 3)

        try:
            features["loudness_lufs"] = round(
                float(es.LoudnessEBUR128()(
                    es.StereoMuxer()(audio, audio))[2]), 2)
        except Exception as e:  # noqa: BLE001
            _warn("loudness", e)

        try:
            dc, _ = es.DynamicComplexity()(audio)
            features["dynamic_range"] = round(float(dc), 2)
        except Exception as e:  # noqa: BLE001
            _warn("dynamic-range", e)

        try:
            _, rate = es.OnsetRate()(audio)
            features["onset_rate"] = round(float(rate), 3)
        except Exception as e:  # noqa: BLE001
            _warn("onset-rate", e)

        try:
            features["spectral_centroid"] = round(
                float(es.SpectralCentroidTime(sampleRate=44100)(audio)), 1)
        except Exception as e:  # noqa: BLE001
            _warn("spectral-centroid", e)

        # Frame pass: HPCP fingerprint + dissonance + chords (one loop).
        try:
            win = es.Windowing(type="blackmanharris62")
            spectrum = es.Spectrum()
            peaks = es.SpectralPeaks(orderBy="frequency", minFrequency=20,
                                     maxFrequency=8000)
            hpcp_algo = es.HPCP()
            diss_algo = es.Dissonance()
            hpcps, diss_vals = [], []
            for frame in es.FrameGenerator(audio, frameSize=4096,
                                           hopSize=2048):
                freqs, mags = peaks(spectrum(win(frame)))
                hpcps.append(hpcp_algo(freqs, mags))
                if len(freqs) > 1:
                    diss_vals.append(diss_algo(freqs, mags))
            if diss_vals:
                features["dissonance"] = round(float(np.mean(diss_vals)), 3)
            if hpcps:
                extras["hpcp"] = [round(float(v), 4)
                                  for v in np.mean(np.array(hpcps), axis=0)]
                try:
                    chords, _strengths = es.ChordsDetection(hopSize=2048)(
                        np.array(hpcps, dtype="float32"))
                    extras["chords"] = _encode_chords(chords,
                                                      hop_seconds=2048 / 44100)
                except Exception as e:  # noqa: BLE001
                    _warn("chords", e)
        except Exception as e:  # noqa: BLE001
            _warn("hpcp/dissonance", e)

        try:
            extras["structure"] = compute_structure(
                np.asarray(audio), 44100, features["bpm_confidence"])
        except Exception as e:  # noqa: BLE001
            _warn("structure", e)
        essentia_ok = True
    except ImportError:
        pass

    if not essentia_ok:
        import librosa
        import numpy as np
        versions["librosa"] = librosa.__version__
        y, sr = librosa.load(path, sr=22050, mono=True)
        tempo = librosa.feature.tempo(y=y, sr=sr)
        features["bpm"] = round(float(tempo[0]), 2)
        features["bpm_confidence"] = 0.5  # librosa gives no confidence
        rms = librosa.feature.rms(y=y)[0]
        features["energy"] = round(min(1.0, float(np.mean(rms)) * 8.0), 3)
        features["dynamic_range"] = round(
            float(20 * np.log10((np.percentile(rms, 95) + 1e-9)
                                / (np.percentile(rms, 10) + 1e-9))), 2)
        try:
            onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time")
            duration = len(y) / sr
            if duration > 0:
                features["onset_rate"] = round(len(onsets) / duration, 3)
        except Exception as e:  # noqa: BLE001
            _warn("onset-rate", e)
        try:
            features["spectral_centroid"] = round(
                float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr))), 1)
        except Exception as e:  # noqa: BLE001
            _warn("spectral-centroid", e)
        try:
            _tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
            extras["beat_positions"] = [round(float(t), 3) for t in beats]
        except Exception as e:  # noqa: BLE001
            _warn("beat-track", e)
        # Krumhansl-Schmuckler key estimate from mean chroma.
        chroma = np.mean(librosa.feature.chroma_cqt(y=y, sr=sr), axis=1)
        major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                          2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
        minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                          2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
        notes = ["C", "C#", "D", "D#", "E", "F",
                 "F#", "G", "G#", "A", "A#", "B"]
        best = (None, None, -2.0)
        for i in range(12):
            rolled = np.roll(chroma, -i)
            cm = np.corrcoef(rolled, major)[0, 1]
            cn = np.corrcoef(rolled, minor)[0, 1]
            if cm > best[2]:
                best = (notes[i], "major", cm)
            if cn > best[2]:
                best = (notes[i], "minor", cn)
        features["musical_key"], features["musical_scale"] = best[0], best[1]
        if best[2] > 0:
            features["key_strength"] = round(min(1.0, float(best[2])), 3)
        try:
            extras["structure"] = compute_structure(
                y, sr, features["bpm_confidence"])
        except Exception as e:  # noqa: BLE001
            _warn("structure", e)

    # KeyFinder wins for the Camelot key when present (more accurate).
    kf = keyfinder_key(path)
    if kf:
        tonic, scale, ver = kf
        features["musical_key"], features["musical_scale"] = tonic, scale
        versions["keyfinder"] = ver
    if features["musical_key"] and features["musical_scale"]:
        features["camelot_key"] = to_camelot(
            features["musical_key"], features["musical_scale"])
    return features, versions, extras


def _encode_chords(chords, hop_seconds: float) -> dict:
    """Run-length encode the per-frame chord sequence into segments plus a
    duration-ranked summary — compact enough for a JSON column."""
    segments = []
    for i, c in enumerate(chords):
        c = str(c)
        if segments and segments[-1]["chord"] == c:
            segments[-1]["end"] = round((i + 1) * hop_seconds, 1)
        else:
            segments.append({"chord": c,
                             "start": round(i * hop_seconds, 1),
                             "end": round((i + 1) * hop_seconds, 1)})
    totals: dict[str, float] = {}
    for s in segments:
        totals[s["chord"]] = totals.get(s["chord"], 0.0) + s["end"] - s["start"]
    summary = sorted(totals, key=totals.get, reverse=True)[:4]
    return {"segments": segments, "summary": summary}


# ─────────────────────────────────────────────────────────────────────────────
# Model-derived features — Essentia pretrained TF heads over ONE EffNet-Discogs
# embedding (+ MusiCNN solely for the emomusic valence/arousal head)
# ─────────────────────────────────────────────────────────────────────────────

def _model_meta(mdir: Path, model_file: str) -> dict:
    return json.loads((mdir / model_file).with_suffix(".json").read_text())


def _model_io(meta: dict, purpose: str) -> tuple[str | None, str | None]:
    """(input, output) node names from a model-zoo metadata JSON. Falls back
    to the last declared output when no output carries the wanted purpose."""
    schema = meta.get("schema", {})
    inputs = schema.get("inputs", [])
    outputs = schema.get("outputs", [])
    inp = inputs[0]["name"] if inputs else None
    out = None
    for o in outputs:
        if o.get("output_purpose") == purpose:
            out = o["name"]
            break
    if out is None and outputs:
        out = outputs[-1]["name"]
    return inp, out


def _load_tf_stack():
    """Singleton: Essentia-TF availability + model dir. Graphs themselves are
    built (once) on first use and cached in this dict — never per track."""
    global _tf_stack
    if _tf_stack is not None:
        return _tf_stack
    _tf_stack = False
    if not MODELS_DIR:
        print("  ! MIS_MODELS_DIR unset — model-derived features skipped")
        return False
    mdir = Path(MODELS_DIR) / "essentia"
    if not mdir.is_dir():
        print(f"  ! {mdir} missing — model-derived features skipped")
        return False
    try:
        import essentia.standard as es
        for algo in ("TensorflowPredictEffnetDiscogs",
                     "TensorflowPredictMusiCNN", "TensorflowPredict2D"):
            getattr(es, algo)
    except (ImportError, AttributeError) as e:
        print(f"  ! essentia-tensorflow unavailable ({e}) — model features skipped")
        return False
    _tf_stack = {"es": es, "dir": mdir, "graphs": {}}
    return _tf_stack


def _embedding(stack, backbone: str, audio16):
    """Frame-wise embeddings from a backbone, model loaded once."""
    es, mdir, graphs = stack["es"], stack["dir"], stack["graphs"]
    key = f"emb:{backbone}"
    if key not in graphs:
        model_file = EMBEDDING_MODELS[backbone]
        meta = _model_meta(mdir, model_file)
        _, out = _model_io(meta, "embeddings")
        cls = (es.TensorflowPredictEffnetDiscogs if backbone == "effnet"
               else es.TensorflowPredictMusiCNN)
        graphs[key] = cls(graphFilename=str(mdir / model_file), output=out)
    return graphs[key](audio16)


def _head(stack, head_key: str):
    """(TensorflowPredict2D graph, classes) for a head, loaded once."""
    es, mdir, graphs = stack["es"], stack["dir"], stack["graphs"]
    if head_key not in graphs:
        model_file = HEAD_MODELS[head_key][0]
        meta = _model_meta(mdir, model_file)
        inp, out = _model_io(meta, "predictions")
        kwargs = {"graphFilename": str(mdir / model_file), "output": out}
        if inp:
            kwargs["input"] = inp
        graphs[head_key] = (es.TensorflowPredict2D(**kwargs),
                            meta.get("classes") or [])
    return graphs[head_key]


def _positive_index(head_key: str, classes: list) -> int:
    """Index of the positive class in a binary head ('acoustic' in
    ['acoustic','non_acoustic'], 'instrumental' in ['instrumental','voice'])."""
    wanted = {"mood_acoustic": "acoustic",
              "voice_instrumental": "instrumental"}.get(
        head_key, head_key.removeprefix("mood_"))
    for i, c in enumerate(classes):
        if str(c).lower() == wanted:
            return i
    return 0


def model_features(path: str, features: dict, versions: dict) -> dict | None:
    """Run the pinned TF heads over ONE embedding per backbone. Fills the
    model-derived audio_features columns in place and returns the predictions
    dict for the audio_inferred tag pipeline (None when the stack is absent).
    Every head failure degrades to an honest NULL for its own outputs only."""
    stack = _load_tf_stack()
    if not stack:
        return None
    import numpy as np
    es = stack["es"]

    # The zoo heads all expect 16 kHz mono input.
    audio16 = es.MonoLoader(filename=path, sampleRate=16000,
                            resampleQuality=4)()
    embeddings: dict[str, object] = {}
    for backbone in ("effnet", "musicnn"):
        try:
            embeddings[backbone] = _embedding(stack, backbone, audio16)
        except Exception as e:  # noqa: BLE001
            _warn(f"{backbone} embedding", e)

    predictions: dict[str, dict] = {}
    for head_key, (model_file, backbone, kind, (t_kind, t_name)) \
            in HEAD_MODELS.items():
        emb = embeddings.get(backbone)
        if emb is None:
            continue
        try:
            graph, classes = _head(stack, head_key)
            mean = np.asarray(graph(emb)).mean(axis=0)
            if kind == "binary":
                p = float(mean[_positive_index(head_key, classes)])
                if t_kind == "feature":
                    features[t_name] = round(p, 3)
                else:
                    label = head_key.removeprefix("mood_")
                    predictions.setdefault(t_name, {})[label] = round(p, 4)
            elif kind == "multilabel":
                order = np.argsort(mean)[::-1][:PREDICTION_TOP_N.get(t_name, 20)]
                predictions[t_name] = {
                    str(classes[i]) if i < len(classes) else f"class_{i}":
                        round(float(mean[i]), 4)
                    for i in order}
            elif kind == "regression":
                features[t_name] = round(float(np.clip(mean.flat[0], 0, 1)), 3)
            elif kind == "regression2d":
                # emomusic: [valence, arousal] on a 1..9 scale → 0..1.
                names = [str(c).lower() for c in classes] or ["valence",
                                                              "arousal"]
                vals = np.asarray(mean).flatten()
                for name, v in zip(names, vals):
                    if name in ("valence", "arousal"):
                        features[name] = round(
                            float(np.clip((v - 1.0) / 8.0, 0, 1)), 3)
        except Exception as e:  # noqa: BLE001
            _warn(f"head {head_key}", e)

    if embeddings:
        versions["essentia"] = (f"{versions.get('essentia', 'unknown')}"
                                f"+{ESSENTIA_MODELS_VERSION}")
    return predictions or None


def clap_embedding(path: str) -> list[float]:
    """512-dim CLAP audio embedding (laion/clap-htsat-fused)."""
    global _clap
    import librosa
    import numpy as np
    if _clap is None:
        import laion_clap
        _clap = laion_clap.CLAP_Module(enable_fusion=True)
        ckpt = Path(MODELS_DIR or "") / "clap" / CLAP_CKPT_NAME
        if MODELS_DIR and ckpt.is_file():
            _clap.load_ckpt(str(ckpt))     # baked into the image — no download
        else:
            _clap.load_ckpt()              # default fused checkpoint (downloads)
    y, _ = librosa.load(path, sr=48000, mono=True)
    emb = _clap.get_audio_embedding_from_data(x=y[None, :], use_tensor=False)
    vec = np.asarray(emb[0], dtype=float).tolist()
    if len(vec) != 512:
        raise RuntimeError(f"CLAP returned {len(vec)}-dim vector, expected 512")
    return vec


# ─────────────────────────────────────────────────────────────────────────────
# Job loop
# ─────────────────────────────────────────────────────────────────────────────

def process_job(job: dict) -> dict:
    """One job: download → extract → build result. Temp audio ALWAYS deleted."""
    tmpdir = tempfile.mkdtemp(prefix="mis-audio-")
    audio_path = None
    try:
        print(f"  ↓ {job['artist']} — {job['title']}  [{job['lawful_basis']}]")
        audio_path = download(job["source_url"], tmpdir)
        features, versions, extras = extract_features(audio_path)
        predictions = model_features(audio_path, features, versions)
        vector = clap_embedding(audio_path)
        versions["clap"] = CLAP_MODEL
        return {
            "candidate_id": job["candidate_id"],
            "status": "ok",
            "features": features,
            "camelot_key": features.get("camelot_key"),
            "clap_vector": vector,
            "model_versions": versions,
            "beat_positions": extras.get("beat_positions"),
            "chords": extras.get("chords"),
            "hpcp": extras.get("hpcp"),
            "structure": extras.get("structure"),
            "predictions": predictions,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "candidate_id": job["candidate_id"],
            "status": "failed",
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        # No audio retention, EVER. Failure to delete is fatal by design.
        import shutil
        try:
            shutil.rmtree(tmpdir)
        except OSError as e:
            die(f"could not delete temp audio dir {tmpdir}: {e}")
        if Path(tmpdir).exists():
            die(f"temp audio dir survived deletion: {tmpdir}")


def main() -> None:
    if not SERVER:
        die("set MIS_SERVER (e.g. http://100.77.32.111:8080)")
    if not TOKEN:
        die("set AUDIO_NODE_TOKEN (must match the server .env)")

    total = ok = failed = 0
    while True:
        jobs = api("/api/audio/claim",
                   {"batch": BATCH, "reprocess": REPROCESS})["jobs"]
        if not jobs:
            print(f"No more jobs. Done: {ok} ok, {failed} failed, {total} total.")
            return
        for job in jobs:
            result = process_job(job)
            resp = api("/api/audio/result", result)
            total += 1
            if result["status"] == "ok":
                ok += 1
                print(f"  ✓ {job['track_pk']} → {resp.get('match_status')}"
                      f"  (+{resp.get('tags_written', 0)} audio tags)")
            else:
                failed += 1
                print(f"  ✗ {job['track_pk']}: {result.get('error')}")
            time.sleep(PACE_SECONDS)  # polite pacing


if __name__ == "__main__":
    main()
