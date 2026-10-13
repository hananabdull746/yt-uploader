#!/usr/bin/env python3
"""Download the YT Uploader OAuth token.pickle through the local SSH tunnel.

Usage: python fetch_token_pickle.py [--base-url http://localhost:8000] [--output token.pickle]
Keep the SSH tunnel open while this script runs. The downloaded pickle is a
sensitive credential file; do not share it or load one from an untrusted source.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


class CSRFParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "input":
            return
        values = dict(attrs)
        if values.get("name") == "csrf" and values.get("value"):
            self.token = values["value"]


def csrf_from(html: str) -> str:
    parser = CSRFParser()
    parser.feed(html)
    if not parser.token:
        raise RuntimeError("Could not find a CSRF token; sign in page may have changed")
    return parser.token


def main() -> int:
    parser = argparse.ArgumentParser(description="Download token.pickle from YT Uploader through your SSH tunnel")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Dashboard URL (default: http://localhost:8000)")
    parser.add_argument("--output", default="token.pickle", help="Output path (default: token.pickle)")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        parser.error("--base-url must be an absolute HTTP or HTTPS URL")
    if parsed.scheme == "http" and parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("For HTTP, use localhost through the SSH tunnel; non-local URLs must use HTTPS")

    output = Path(args.output)
    if output.exists() and input(f"{output} exists. Overwrite it? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 1

    username = input("YT Uploader username: ").strip()
    password = getpass.getpass("YT Uploader password: ")
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    try:
        with opener.open(base + "/login", timeout=20) as response:
            login_html = response.read().decode("utf-8", "replace")
        login_payload = urlencode({"username": username, "password": password, "csrf": csrf_from(login_html)}).encode()
        login_request = Request(base + "/login", data=login_payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
        with opener.open(login_request, timeout=20) as response:
            if urlsplit(response.geturl()).path == "/login":
                raise RuntimeError("Login failed. Check the username and password.")

        with opener.open(base + "/settings", timeout=20) as response:
            settings_html = response.read().decode("utf-8", "replace")
        export_payload = urlencode({"csrf": csrf_from(settings_html)}).encode()
        export_request = Request(base + "/oauth/export-pickle", data=export_payload,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
        with opener.open(export_request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"Export failed with HTTP {response.status}")
            data = response.read(1_000_001)
    except HTTPError as exc:
        print(f"Request failed: HTTP {exc.code}", file=sys.stderr)
        return 1
    except (URLError, TimeoutError) as exc:
        print(f"Could not reach the dashboard: {exc}", file=sys.stderr)
        print("Check that the SSH tunnel is open and the app is listening on localhost:8000.", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if len(data) > 1_000_000 or not data.startswith(b"\x80"):
        print("Server response is not a supported token.pickle file.", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    try:
        os.chmod(output, 0o600)
    except OSError:
        pass
    print(f"Saved {len(data)} bytes to {output.resolve()}")
    print("This file contains a Google refresh token. Store it securely and never share it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
