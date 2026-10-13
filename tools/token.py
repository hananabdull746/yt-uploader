#!/usr/bin/env python3
"""Create a Google OAuth token.pickle locally for YT Uploader.

1. Create an OAuth client of type Desktop app in Google Cloud Console.
2. Copy this file to tools/token.local.py and paste that client's ID and secret into the local copy.
3. Install the dependency: python -m pip install google-auth-oauthlib
4. Run: python tools/token.local.py
5. Sign in in the browser; token.pickle is written to the current directory.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# This script is named token.py. Remove its directory from imports so it cannot
# shadow Python's standard-library `token` module while OAuth libraries load.
_script_dir = Path(__file__).resolve().parent
_filtered_path = []
for _entry in sys.path:
    try:
        if Path(_entry or os.getcwd()).resolve() == _script_dir:
            continue
    except OSError:
        pass
    _filtered_path.append(_entry)
sys.path[:] = _filtered_path

# The OAuth callback is a loopback HTTP listener on this same computer.
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

import pickle

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    raise SystemExit("Missing dependency. Install it with: python -m pip install google-auth-oauthlib")

# Paste credentials for an OAuth client whose type is Desktop app.
CLIENT_ID = "PASTE_DESKTOP_CLIENT_ID_HERE"
CLIENT_SECRET = "PASTE_DESKTOP_CLIENT_SECRET_HERE"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    if "PASTE_" in CLIENT_ID or "PASTE_" in CLIENT_SECRET:
        raise SystemExit("Edit this local script and paste your Desktop OAuth client ID and secret first.")

    client_config = {
        "installed": {
            "client_id": CLIENT_ID.strip(),
            "client_secret": CLIENT_SECRET.strip(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    credentials = flow.run_local_server(
        host="localhost",
        port=0,
        open_browser=True,
        authorization_prompt_message="Opening Google sign-in in your browser: {url}",
        success_message="Google sign-in complete. You may close this browser tab.",
        access_type="offline",
        prompt="consent",
    )
    if not credentials.refresh_token:
        raise SystemExit("Google did not return a refresh token. Revoke this app's access in your Google Account and run the script again.")

    output = Path.cwd() / "token.pickle"
    if output.exists() and input(f"{output} already exists. Replace it? [y/N] ").strip().lower() != "y":
        print("Cancelled; existing token file was not changed.")
        return 1
    with output.open("wb") as f:
        pickle.dump(credentials, f, protocol=pickle.HIGHEST_PROTOCOL)
    try:
        os.chmod(output, 0o600)
    except OSError:
        pass
    print(f"Saved credentials to {output}")
    print("Upload this token.pickle in YT Uploader → Connections & settings. Keep the file private; it contains a refresh token.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
