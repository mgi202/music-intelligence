# Mac compute node — Phase 3 (Ears)

The heavy audio work (download → Essentia/KeyFinder/CLAP → post → delete)
runs on the Mac, never on the VPS. Everything talks to the server over
Tailscale with a shared secret.

## One-time setup

```bash
cd "<this repo>/lean-headless-sync-engine"
python3 -m venv ~/.mis-compute-venv
~/.mis-compute-venv/bin/pip install -r requirements-compute.txt
brew install ffmpeg libkeyfinder keyfinder-cli   # ffmpeg required; keyfinder optional but better keys
```

Set the same `AUDIO_NODE_TOKEN` the server has in its `.env`.

## Embed the tag-profile prompts (once, and after prompt edits)

```bash
MIS_SERVER=http://100.77.32.111:8080 AUDIO_NODE_TOKEN=<token> \
  ~/.mis-compute-venv/bin/python compute_node/embed_prompts.py
```

## Run an extraction session (the one-liner)

```bash
MIS_SERVER=http://100.77.32.111:8080 AUDIO_NODE_TOKEN=<token> \
  ~/.mis-compute-venv/bin/python compute_node/agent.py
```

It claims batches of lawful candidates, processes them, posts results, and
exits when the queue is empty. Safe to interrupt: claims expire after
`AUDIO_CLAIM_LEASE_MINUTES` (default 60) and re-queue by themselves.

Reprocess stale vectors after a model bump (explicit, never automatic):

```bash
MIS_REPROCESS=1 MIS_SERVER=... AUDIO_NODE_TOKEN=... \
  ~/.mis-compute-venv/bin/python compute_node/agent.py
```

## Guarantees

- Audio is downloaded to a temp dir and deleted in a `finally` — a job that
  cannot delete its audio kills the agent loudly.
- The agent only ever downloads URLs the server handed it, and the server
  only hands out candidates whose `lawful_basis` is not `unknown`.
