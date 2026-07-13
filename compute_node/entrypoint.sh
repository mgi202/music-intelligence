#!/bin/sh
# MIS compute node entrypoint: (optional) in-container Tailscale, then a hard
# server-reachability check BEFORE any work — the main integration risk is the
# container not seeing the tailnet, so fail fast and loudly here.
set -eu

: "${MIS_SERVER:?set MIS_SERVER (e.g. http://100.77.32.111:8080)}"
: "${AUDIO_NODE_TOKEN:?set AUDIO_NODE_TOKEN (must match the server .env)}"

if [ -n "${TS_AUTHKEY:-}" ]; then
    echo "[entrypoint] starting tailscaled (userspace networking)…"
    mkdir -p /var/lib/tailscale
    tailscaled \
        --state=/var/lib/tailscale/tailscaled.state \
        --tun=userspace-networking \
        --socks5-server=localhost:1055 \
        >/var/log/tailscaled.log 2>&1 &

    i=0
    until tailscale up --authkey="${TS_AUTHKEY}" \
            --hostname="${TS_HOSTNAME:-mis-compute-node}" \
            --timeout=30s 2>/dev/null; do
        i=$((i + 1))
        if [ "$i" -ge 5 ]; then
            echo "[entrypoint] FATAL: tailscale up failed after $i attempts" >&2
            tail -n 50 /var/log/tailscaled.log >&2 || true
            exit 1
        fi
        sleep 2
    done
    # Server calls (and ONLY server calls — agent.py keeps downloads direct so
    # yt-dlp egresses via the home connection) ride the tailnet SOCKS proxy.
    export MIS_PROXY="socks5h://localhost:1055"
    echo "[entrypoint] tailscale up as $(tailscale ip -4 2>/dev/null || echo '?')"
else
    echo "[entrypoint] TS_AUTHKEY not set — assuming ${MIS_SERVER} is reachable directly"
fi

echo "[entrypoint] probing ${MIS_SERVER}/api/health …"
if [ -n "${MIS_PROXY:-}" ]; then
    curl -fsS --max-time 20 --proxy "${MIS_PROXY}" "${MIS_SERVER}/api/health" >/dev/null
else
    curl -fsS --max-time 20 "${MIS_SERVER}/api/health" >/dev/null
fi
echo "[entrypoint] server reachable — starting: $*"

exec "$@"
