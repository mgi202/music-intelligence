"""
One-shot: embed the tag-profile CLAP text prompts and ship the vectors to
the server (vector_query_profiles). The server never imports CLAP.

Run (inside the compute-node container — see compute_node/README.md):
    docker compose -f compute_node/docker-compose.yml run --rm compute-node \
        python /app/compute_node/embed_prompts.py

Re-run whenever prompts change — upserts are idempotent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

SERVER = os.environ.get("MIS_SERVER", "").rstrip("/")
TOKEN = os.environ.get("AUDIO_NODE_TOKEN", "")
MODELS_DIR = os.environ.get("MIS_MODELS_DIR", "")
PROXY = os.environ.get("MIS_PROXY", "")
CLAP_MODEL = "laion/clap-htsat-fused"
CLAP_CKPT_NAME = "630k-audioset-fusion-best.pt"

_PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None


def main() -> None:
    if not SERVER or not TOKEN:
        print("FATAL: set MIS_SERVER and AUDIO_NODE_TOKEN", file=sys.stderr)
        sys.exit(1)

    headers = {"X-Audio-Node-Token": TOKEN}
    profiles = requests.get(f"{SERVER}/api/audio/prompts", headers=headers,
                            timeout=60, proxies=_PROXIES).json()["profiles"]
    if not profiles:
        print("No profiles carry prompts — nothing to embed.")
        return
    print(f"Embedding prompts for {len(profiles)} profiles "
          f"({CLAP_MODEL}, first load takes a minute)…")

    import laion_clap
    import numpy as np
    model = laion_clap.CLAP_Module(enable_fusion=True)
    ckpt = Path(MODELS_DIR or "") / "clap" / CLAP_CKPT_NAME
    if MODELS_DIR and ckpt.is_file():
        model.load_ckpt(str(ckpt))     # baked into the image — no download
    else:
        model.load_ckpt()

    texts, keys = [], []
    for p in profiles:
        for kind in ("positive", "negative"):
            prompt = p.get(f"{kind}_prompt")
            if prompt:
                texts.append(prompt)
                keys.append((p["profile_id"], p["tag_name"], kind, prompt))

    embs = model.get_text_embedding(texts, use_tensor=False)
    payload = {
        "model_version": CLAP_MODEL,
        "embeddings": [
            {"profile_id": pid, "name": name, "kind": kind,
             "query_text": prompt,
             "vector": np.asarray(vec, dtype=float).tolist()}
            for (pid, name, kind, prompt), vec in zip(keys, embs)
        ],
    }
    resp = requests.post(f"{SERVER}/api/audio/prompt-embeddings",
                         json=payload, headers=headers, timeout=120,
                         proxies=_PROXIES)
    resp.raise_for_status()
    print(f"Stored {resp.json()['stored']} prompt embeddings on the server.")


if __name__ == "__main__":
    main()
