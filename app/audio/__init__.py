"""Phase 3 (Ears) — audio-source discovery, compute-node API, vector store.

The heavy ML stack (torch/Essentia/KeyFinder/CLAP) never lives here — it runs
only on the Mac compute node (compute_node/, requirements-compute.txt). This
package handles the server side: lawful-source discovery, the claim/result
API, the Qdrant wrapper, and prompt seeding.
"""
