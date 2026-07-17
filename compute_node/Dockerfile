# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# MIS compute node (Phase 3 — Ears): full Essentia + TensorFlow + CLAP stack.
#
# ARCHITECTURE NOTE (2026-07-13): this image is linux/amd64, NOT linux/arm64.
# essentia-tensorflow has never published a Linux aarch64 wheel (verified on
# PyPI: manylinux x86_64 + macOS only), so an arm64 container cannot pip-install
# the TF stack. On Apple Silicon, Docker Desktop runs this image under Rosetta 2
# (Settings → General → "Use Rosetta for x86_64/amd64 emulation" — required;
# Rosetta supports the AVX instructions TensorFlow needs, plain QEMU does not).
#
# yt-dlp inside the container egresses via the Mac's home connection (Docker
# NAT) — the reason the node lives on the Mac. Server calls go over the tailnet
# via an in-container tailscaled SOCKS proxy (see entrypoint.sh).
#
# Build context is the REPO ROOT (see compute_node/docker-compose.yml).
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: build libkeyfinder + keyfinder-cli (not packaged in Debian) ────
FROM --platform=linux/amd64 debian:bookworm-slim AS keyfinder-build
RUN apt-get update && apt-get install -y --no-install-recommends \
        git cmake make g++ pkg-config ca-certificates \
        libfftw3-dev libavcodec-dev libavformat-dev libavutil-dev \
        libswresample-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 2.2.8 \
        https://github.com/mixxxdj/libkeyfinder.git /src/libkeyfinder \
    && cmake -S /src/libkeyfinder -B /src/libkeyfinder/build \
         -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_BUILD_TYPE=Release \
         -DBUILD_TESTING=OFF \
    && cmake --build /src/libkeyfinder/build --parallel \
    && cmake --install /src/libkeyfinder/build
# keyfinder-cli switched from a Makefile to CMake upstream (found 2026-07-17,
# first real build) — same deps (libav* + pkg-config), already in this stage.
RUN git clone --depth 1 \
        https://github.com/evanpurkhiser/keyfinder-cli.git /src/keyfinder-cli \
    && cmake -S /src/keyfinder-cli -B /src/keyfinder-cli/build \
         -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /src/keyfinder-cli/build --parallel \
    && cmake --install /src/keyfinder-cli/build

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM --platform=linux/amd64 python:3.11-slim-bookworm

# ffmpeg (yt-dlp decode + keyfinder-cli's libav runtime), curl for the model
# downloads + health probe, fftw for libkeyfinder.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libfftw3-double3 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Tailscale — the container becomes its own tailnet node (userspace mode).
RUN curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg \
        -o /usr/share/keyrings/tailscale-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/debian bookworm main" \
        > /etc/apt/sources.list.d/tailscale.list \
    && apt-get update && apt-get install -y --no-install-recommends tailscale \
    && rm -rf /var/lib/apt/lists/*

COPY --from=keyfinder-build /usr/local/bin/keyfinder-cli /usr/local/bin/
COPY --from=keyfinder-build /usr/local/lib/libkeyfinder* /usr/local/lib/
RUN ldconfig && keyfinder-cli 2>&1 | head -1 || true

# Python stack. Torch first from the CPU index (the default index would pull
# multi-GB CUDA builds), then the pinned compute requirements.
RUN pip install --no-cache-dir torch==2.3.1 torchaudio==2.3.1 \
        --index-url https://download.pytorch.org/whl/cpu
COPY requirements-compute.txt /tmp/requirements-compute.txt
RUN pip install --no-cache-dir -r /tmp/requirements-compute.txt

# ── Pretrained models, baked into an image layer (pinned 2026-07-13) ─────────
# One EffNet-Discogs embedding serves every head except valence/arousal
# (emomusic exists only for MusiCNN — the justified second backbone).
# Each .pb ships with its .json metadata (input/output node names + classes);
# the agent reads the .json at runtime, so the pair must stay together.
ENV MIS_MODELS_DIR=/models
RUN set -e; mkdir -p /models/essentia; cd /models/essentia; \
    base=https://essentia.upf.edu/models; \
    for f in \
      feature-extractors/discogs-effnet/discogs-effnet-bs64-1 \
      feature-extractors/musicnn/msd-musicnn-1 \
      classification-heads/mood_acoustic/mood_acoustic-discogs-effnet-1 \
      classification-heads/voice_instrumental/voice_instrumental-discogs-effnet-1 \
      classification-heads/mood_happy/mood_happy-discogs-effnet-1 \
      classification-heads/mood_sad/mood_sad-discogs-effnet-1 \
      classification-heads/mood_aggressive/mood_aggressive-discogs-effnet-1 \
      classification-heads/mood_relaxed/mood_relaxed-discogs-effnet-1 \
      classification-heads/mood_party/mood_party-discogs-effnet-1 \
      classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1 \
      classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1 \
      classification-heads/mtg_jamendo_instrument/mtg_jamendo_instrument-discogs-effnet-1 \
      classification-heads/approachability/approachability_regression-discogs-effnet-1 \
      classification-heads/engagement/engagement_regression-discogs-effnet-1 \
      classification-heads/emomusic/emomusic-msd-musicnn-2 \
    ; do \
      echo "fetching $f"; \
      curl -fsSL --retry 8 --retry-delay 10 --retry-all-errors \
           --connect-timeout 30 -O "$base/$f.pb"; \
      curl -fsSL --retry 8 --retry-delay 10 --retry-all-errors \
           --connect-timeout 30 -O "$base/$f.json"; \
    done
    # --retry-all-errors: essentia.upf.edu intermittently drops connections
    # (empty reply / stalls, seen 2026-07-17); plain --retry skips those.

# CLAP fused checkpoint (~1.9 GB) — baked in so the container never downloads
# at runtime. agent.py loads it from $MIS_MODELS_DIR/clap/.
RUN mkdir -p /models/clap \
    && curl -fsSL --retry 5 --retry-delay 10 --retry-all-errors \
        -o /models/clap/630k-audioset-fusion-best.pt \
        https://huggingface.co/lukewys/laion_clap/resolve/main/630k-audioset-fusion-best.pt

# torchvision: not used directly, but laion_clap imports it transitively
# (clap_module.utils → torchvision.ops.misc) — found at first build 2026-07-17.
# 0.18.1 is the matched release for torch 2.3.1. Deliberately a late layer so
# fixing it didn't invalidate the cached model/CLAP download layers above.
RUN pip install --no-cache-dir torchvision==0.18.1 \
        --index-url https://download.pytorch.org/whl/cpu

# ── Build-time verification (acceptance criterion 1) ─────────────────────────
# import essentia.standard AND the TensorflowPredict* algorithms must resolve,
# plus every downloaded graph must load. A broken wheel fails the BUILD, not
# the first job.
RUN python - <<'EOF'
import json, pathlib
import essentia
import essentia.standard as es
for algo in ("TensorflowPredictEffnetDiscogs", "TensorflowPredictMusiCNN",
             "TensorflowPredict2D"):
    assert hasattr(es, algo), f"essentia.standard.{algo} missing"
models = sorted(pathlib.Path("/models/essentia").glob("*.pb"))
assert len(models) == 15, f"expected 15 model graphs, found {len(models)}"
for pb in models:
    meta = json.loads(pb.with_suffix(".json").read_text())
    assert meta.get("schema", {}).get("outputs"), f"{pb.name}: no schema outputs"
import laion_clap  # noqa: F401  (import only — ckpt load happens at runtime)
import yt_dlp      # noqa: F401
print("BUILD VERIFY OK: essentia", essentia.__version__,
      "| TF algorithms present |", len(models), "graphs + metadata")
EOF

COPY compute_node/ /app/compute_node/
RUN chmod +x /app/compute_node/entrypoint.sh
WORKDIR /app

ENTRYPOINT ["/app/compute_node/entrypoint.sh"]
CMD ["python", "/app/compute_node/agent.py"]
