#!/usr/bin/env python3
"""Offline channel-linking helper -- run this on YOUR OWN computer, not the VPS.

This is the "no SSH, no tunnel" path from the build spec's OAUTH_MODE=loopback
Advanced Linking flow: it runs a normal OAuth consent locally (your own
browser, your own machine), then writes a `token.json` you upload back into
the web panel at /channels/add -> Advanced linking -> "Upload token.json".

Setup (one-time, on your own machine -- NOT on the VPS):
    pip install google-auth-oauthlib google-api-python-client

Usage:
    python link_channel.py [path/to/client_secret.json]

If no path is given, you'll be prompted for one. Download that file from
Google Cloud Console -> APIs & Services -> Credentials -> your OAuth client ->
Download JSON. It must be a "Desktop app" type client for this script to
work without registering a redirect URI (run_local_server picks an ephemeral
port and Google allows that for Desktop-app clients automatically).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def main() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing dependencies. Run:\n    pip install google-auth-oauthlib google-api-python-client",
              file=sys.stderr)
        raise SystemExit(1)

    if len(sys.argv) > 1:
        client_secret_path = sys.argv[1]
    else:
        client_secret_path = input("Path to client_secret.json: ").strip().strip('"')

    path = Path(client_secret_path).expanduser()
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        raise SystemExit(1)

    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("type") == "service_account":
        print(
            "That's a service-account key, not an OAuth Client ID. Service accounts "
            "can't upload to YouTube -- download an OAuth Client ID JSON (Desktop app) "
            "from Google Cloud Console -> Credentials instead.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if "installed" not in raw and "web" not in raw:
        print("That doesn't look like an OAuth Client ID JSON (no 'installed' or 'web' key).",
              file=sys.stderr)
        raise SystemExit(1)

    print("Opening your browser to sign in and approve access...")
    flow = InstalledAppFlow.from_client_secrets_file(str(path), scopes=SCOPES)
    credentials = flow.run_local_server(port=0, prompt="select_account consent")

    out_path = Path.cwd() / "token.json"
    out_path.write_text(credentials.to_json(), encoding="utf-8")

    print(f"\nDone. Wrote {out_path}")
    print("Next step: open the web panel -> Channels -> Add a channel -> Advanced")
    print("linking -> \"Upload token.json\", and select this file.")
    print("\nTreat token.json like a password until you've uploaded it -- delete it")
    print("afterward if you don't need it anymore.")


if __name__ == "__main__":
    main()
