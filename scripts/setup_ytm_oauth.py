"""
YTM OAuth setup — run this once before using the system.

Generates oauth.json in the project root via the ytmusicapi browser flow.

Usage:
    python scripts/setup_ytm_oauth.py
"""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

OUTPUT_FILE = os.getenv("YTM_OAUTH_FILE", "oauth.json")


def main():
    try:
        from ytmusicapi import YTMusic, setup_oauth
    except ImportError:
        print("ERROR: ytmusicapi not installed. Run: pip install -r requirements.txt")
        sys.exit(1)

    print("YouTube Music OAuth Setup")
    print("=" * 40)
    print(f"This will open a browser window for Google authentication.")
    print(f"After authenticating, credentials will be saved to: {OUTPUT_FILE}")
    print()

    setup_oauth(filepath=OUTPUT_FILE, open_browser=True)

    print()
    print(f"✓ OAuth credentials saved to {OUTPUT_FILE}")
    print("You can now run the system.")


if __name__ == "__main__":
    main()
