# Compute node — Phase 3 (Ears), Dockerised

The heavy audio work (download → Essentia signal + TF model heads → KeyFinder
→ CLAP → post → delete) runs in a Docker container **on the Mac**, never on
the VPS. yt-dlp egresses via the home connection (the reason the node lives
here); server calls ride the tailnet via an in-container Tailscale node.

> **Why linux/amd64, not arm64?** essentia-tensorflow has never published a
> Linux aarch64 wheel (PyPI: manylinux x86_64 + macOS only), and the native
> Apple-Silicon pip path is broken (MTG/essentia#1486). Docker Desktop runs
> the amd64 image under Rosetta 2, which supports the AVX instructions
> TensorFlow needs. **Enable it once:** Docker Desktop → Settings → General →
> "Use Rosetta for x86_64/amd64 emulation on Apple Silicon".

## One-time setup

1. Install **Docker Desktop for Mac** (Apple Silicon build) and enable the
   Rosetta option above.
2. Create the config:

   ```bash
   cd "<this repo>/lean-headless-sync-engine"
   cp compute_node/.env.example compute_node/.env
   # fill in: AUDIO_NODE_TOKEN (same as server .env) and TS_AUTHKEY
   ```

   `TS_AUTHKEY`: Tailscale admin console → Settings → Keys → Generate auth
   key — **Reusable + Ephemeral**, tagged (e.g. `tag:compute-node`). The
   container joins the tailnet as `mis-compute-node` and self-cleans when
   it's been offline a while.

## Run an extraction session (the one-liner)

```bash
docker compose -f compute_node/docker-compose.yml up
```

First run builds the image (~30–60 min — it bakes in torch, all pinned
Essentia TF models and the 1.9 GB CLAP checkpoint; later runs start in
seconds). The entrypoint verifies the server is reachable
(`/api/health` over the tailnet) **before** claiming anything, then claims
batches of lawful candidates, processes them, posts results, and exits when
the queue is empty. Safe to interrupt: claims expire after
`AUDIO_CLAIM_LEASE_MINUTES` (server-side, default 60) and re-queue by
themselves.

Reprocess stale vectors after a model bump (explicit, never automatic):

```bash
MIS_REPROCESS=1 docker compose -f compute_node/docker-compose.yml up
```

## Embed the tag-profile prompts (once, and after prompt edits)

```bash
docker compose -f compute_node/docker-compose.yml run --rm compute-node \
  python /app/compute_node/embed_prompts.py
```

## What gets computed (locked set, 2026-07-13)

- **Signal (base Essentia):** bpm + beat grid, key + strength, danceability,
  energy, LUFS loudness, dynamic range, onset rate, dissonance, spectral
  centroid, HPCP fingerprint, chords, intro/outro/first-drop/breakdown +
  energy-curve structure.
- **Model heads (one EffNet-Discogs embedding per track):** acousticness,
  instrumentalness, approachability, engagement, mood/genre/theme/instrument
  probabilities → `audio_inferred` tag suggestions. Valence/arousal come from
  the emomusic head on MusiCNN (the one justified second backbone).
- **speechiness stays NULL** — the model zoo has no speech/music
  discriminator, and the locked rule is honest NULLs over bad proxies.
- **CLAP:** 512-dim embedding → Qdrant ("sounds like this", kNN classifier).

## Guarantees

- Audio is downloaded to a temp dir and deleted in a `finally` — a job that
  cannot delete its audio kills the agent loudly. Nothing audio-shaped
  survives a job, ever.
- The agent only downloads URLs the server handed it, and the server only
  hands out candidates whose `lawful_basis` is not `unknown`.
- Only server API calls use the tailnet proxy; downloads take the direct
  route (home-IP egress).
- A model bump (Essentia manifest or CLAP) marks other rows stale on the
  server — it never triggers automatic reprocessing.
