"""Job 7 — nightly healthcheck + YTM auth probe (start of window).

Wraps scripts/healthcheck.py (imported, not subprocessed) and adds one cheap
authenticated ytmusicapi call. YTM cookie auth dies silently otherwise — the
probe makes it die loudly at breakfast (distinct ntfy alert) instead of at
sync time.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent.parent / "scripts")


def run_probe() -> dict:
    """Returns {'healthcheck': 0|1|'error', 'ytm_auth': 'ok'|'failed'|...}."""
    result: dict = {}

    try:
        if _SCRIPTS_DIR not in sys.path:
            sys.path.insert(0, _SCRIPTS_DIR)
        import healthcheck  # scripts/healthcheck.py

        result["healthcheck"] = healthcheck.run_healthcheck(quiet=True)
    except Exception as e:  # noqa: BLE001
        logger.exception("healthcheck import/run failed")
        result["healthcheck"] = "error"
        result["healthcheck_error"] = str(e)

    try:
        from app.ingestion.ytm_adapter import YouTubeMusicAdapter

        adapter = YouTubeMusicAdapter()
        adapter.client.get_library_playlists(limit=1)
        result["ytm_auth"] = "ok"
    except Exception as e:  # noqa: BLE001
        logger.warning("YTM auth probe failed: %s", e)
        result["ytm_auth"] = "failed"
        result["ytm_auth_error"] = str(e)[:200]
        try:
            from app.observability import notify
            notify(
                "YTM cookies expired — run build_ytm_browser_auth.py on the "
                "Mac and scp oauth.json to the server.",
                title="Music Intel — YTM auth",
                tags="rotating_light",
            )
        except Exception:  # noqa: BLE001 — alerting must never raise
            pass

    return result
