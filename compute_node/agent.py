"""
Mac compute-node agent (Phase 3 — Ears).

Claims lawful audio-extraction jobs from the VPS over Tailscale, downloads
each source to a TEMP file (yt-dlp, 128 kbps M4A), extracts features
(Essentia when available, librosa fallback), the Camelot key (keyfinder-cli
when available), and a 512-dim CLAP embedding, posts the result back, and
DELETES the audio in a finally block. Audio is a temporary compute artefact —
nothing audio-shaped survives a job, succeed or fail. A job that cannot
delete its temp file fails loudly and stops the loop.

Runs on demand (not 24/7). See compute_node/README.md for the one-liner.

Environment:
  MIS_SERVER            e.g. http://100.77.32.111:8080  (required)
  AUDIO_NODE_TOKEN      shared secret, must match the server .env (required)
  MIS_BATCH             jobs per claim (default 4)
  MIS_REPROCESS         "1" = claim stale-vector reprocess jobs instead

This file is NEVER imported by the server or tests — the heavy ML stack
(torch/CLAP/Essentia) exists only in the Mac venv (requirements-compute.txt).
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

EXTRACTOR_VERSION = "mis-compute-node/1.0"
CLAP_MODEL = "laion/clap-htsat-fused"

_clap = None  # lazy singleton — model load takes ~30s


def die(msg: str) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def api(path: str, payload: dict | None = None, method: str = "POST") -> dict:
    headers = {"X-Audio-Node-Token": TOKEN}
    url = f"{SERVER}{path}"
    if method == "GET":
        r = requests.get(url, headers=headers, timeout=60)
    else:
        r = requests.post(url, json=payload, headers=headers, timeout=120)
    r.raise_for_status()
    return r.json()


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


def extract_features(path: str) -> tuple[dict, dict]:
    """(features, versions). Prefers Essentia; degrades to librosa; every
    unavailable signal is an honest None, never a guess."""
    features: dict = {c: None for c in (
        "bpm", "bpm_confidence", "musical_key", "musical_scale", "camelot_key",
        "valence", "arousal", "danceability", "energy", "acousticness",
        "instrumentalness", "loudness_lufs", "dynamic_range", "speechiness",
    )}
    versions: dict = {"extractor": EXTRACTOR_VERSION}

    essentia_ok = False
    try:
        import essentia
        import essentia.standard as es
        versions["essentia"] = essentia.__version__
        audio = es.MonoLoader(filename=path, sampleRate=44100)()
        bpm, _, conf, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
        features["bpm"] = round(float(bpm), 2)
        features["bpm_confidence"] = round(float(conf) / 5.32, 3)  # 0..1
        key, scale, kconf = es.KeyExtractor()(audio)
        features["musical_key"], features["musical_scale"] = key, scale
        features["danceability"] = round(
            min(1.0, float(es.Danceability()(audio)[0]) / 3.0), 3)
        features["energy"] = round(
            min(1.0, float((audio ** 2).mean()) * 20.0), 3)
        try:
            features["loudness_lufs"] = round(
                float(es.LoudnessEBUR128()(
                    es.StereoMuxer()(audio, audio))[2]), 2)
        except Exception:  # noqa: BLE001
            pass
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

    # KeyFinder wins for the Camelot key when present (more accurate).
    kf = keyfinder_key(path)
    if kf:
        tonic, scale, ver = kf
        features["musical_key"], features["musical_scale"] = tonic, scale
        versions["keyfinder"] = ver
    if features["musical_key"] and features["musical_scale"]:
        features["camelot_key"] = to_camelot(
            features["musical_key"], features["musical_scale"])
    return features, versions


def clap_embedding(path: str) -> list[float]:
    """512-dim CLAP audio embedding (laion/clap-htsat-fused)."""
    global _clap
    import librosa
    import numpy as np
    if _clap is None:
        import laion_clap
        _clap = laion_clap.CLAP_Module(enable_fusion=True)
        _clap.load_ckpt()  # default fused checkpoint
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
        features, versions = extract_features(audio_path)
        vector = clap_embedding(audio_path)
        versions["clap"] = CLAP_MODEL
        return {
            "candidate_id": job["candidate_id"],
            "status": "ok",
            "features": features,
            "camelot_key": features.get("camelot_key"),
            "clap_vector": vector,
            "model_versions": versions,
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
                print(f"  ✓ {job['track_pk']} → {resp.get('match_status')}")
            else:
                failed += 1
                print(f"  ✗ {job['track_pk']}: {result.get('error')}")
            time.sleep(PACE_SECONDS)  # polite pacing


if __name__ == "__main__":
    main()
