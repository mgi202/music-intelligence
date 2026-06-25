"""
YTM OAuth setup — run this once before using the system.

Generates oauth.json in the project root via the ytmusicapi browser flow.

Usage:
    python scripts/setup_ytm_oauth.py

Requires YTM_CLIENT_ID and YTM_CLIENT_SECRET in .env (or environment).
Get these from Google Cloud Console:
  1. Create a project
  2. Enable YouTube Data API v3
  3. Credentials → Create OAuth client ID → Desktop app
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

OUTPUT_FILE = os.getenv("YTM_OAUTH_FILE", "oauth.json")
CLIENT_ID = os.getenv("YTM_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("YTM_CLIENT_SECRET", "")


def main():
    try:
        from ytmusicapi import setup_oauth
    except ImportError:
        print("ERROR: ytmusicapi not installed. Run: pip install -r requirements.txt")
        sys.exit(1)

    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: YTM_CLIENT_ID and YTM_CLIENT_SECRET must be set in .env")
        print("Get these from Google Cloud Console > APIs & Services > Credentials")
        sys.exit(1)

    print("YouTube Music OAuth Setup")
    print("=" * 40)
    print(f"Credentials will be saved to: {OUTPUT_FILE}")
    print()

    setup_oauth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        filepath=OUTPUT_FILE,
        open_browser=True,
    )

    print()
    print(f"✓ OAuth credentials saved to {OUTPUT_FILE}")
    print("You can now run the system.")


if __name__ == "__main__":
    main()
