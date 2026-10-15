from __future__ import annotations

import io
import json
import os
import pickle
import re
import secrets
import shutil
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

# The operator explicitly requested OAuthlib transport validation be disabled.
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

import requests
from google.auth import _regional_access_boundary_utils as _rab
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
PROVIDER_BASE = "https://p.savenow.to"
ALLOWED_DOWNLOAD_HOSTS = {"p.savenow.to"}
YT_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


class IntegrationError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


MAX_TOKEN_PICKLE_BYTES = 1_000_000
_ALLOWED_CREDENTIAL_PICKLE_GLOBALS = {
    ("google.oauth2.credentials", "Credentials"): Credentials,
    ("google.auth._regional_access_boundary_utils", "_RegionalAccessBoundaryManager"): _rab._RegionalAccessBoundaryManager,
    ("google.auth._regional_access_boundary_utils", "_RegionalAccessBoundaryData"): _rab._RegionalAccessBoundaryData,
    ("google.auth._regional_access_boundary_utils", "_RegionalAccessBoundaryRefreshManager"): _rab._RegionalAccessBoundaryRefreshManager,
    ("datetime", "datetime"): datetime,
    ("datetime", "timedelta"): timedelta,
    ("datetime", "timezone"): timezone,
    ("builtins", "set"): set,
    ("builtins", "frozenset"): frozenset,
}


class _CredentialUnpickler(pickle.Unpickler):
    """Only construct google-auth credential data; refuse all other pickle globals."""

    def find_class(self, module: str, name: str):
        allowed = _ALLOWED_CREDENTIAL_PICKLE_GLOBALS.get((module, name))
        if allowed is None:
            raise pickle.UnpicklingError("Unsupported pickle global")
        return allowed

    def persistent_load(self, pid):
        raise pickle.UnpicklingError("Persistent pickle references are not supported")


def import_google_credentials_pickle(payload: bytes) -> str:
    """Safely import a compatible token.pickle and return authorized-user JSON."""
    if not payload or len(payload) > MAX_TOKEN_PICKLE_BYTES:
        raise IntegrationError("token.pickle is empty or exceeds the 1 MB import limit")
    try:
        credentials = _CredentialUnpickler(io.BytesIO(payload)).load()
    except Exception as exc:
        raise IntegrationError("Unsupported or invalid token.pickle; use a Google-auth Credentials file") from exc
    if type(credentials) is not Credentials:
        raise IntegrationError("token.pickle must contain a Google-auth Credentials object")
    if not credentials.refresh_token or not credentials.client_id or not credentials.client_secret:
        raise IntegrationError("The credential file must include a refresh token and OAuth client credentials")
    token_uri = urlparse(credentials.token_uri or "")
    if token_uri.scheme != "https" or token_uri.netloc not in {"oauth2.googleapis.com", "accounts.google.com"} or token_uri.path not in {"/token", "/o/oauth2/token"}:
        raise IntegrationError("The token file has an unsupported OAuth token endpoint")
    scopes = set(credentials.granted_scopes or credentials.scopes or [])
    if YOUTUBE_SCOPE not in scopes:
        raise IntegrationError("The token file must include the youtube.upload OAuth scope")
    return credentials.to_json()


@dataclass
class FeedVideo:
    video_id: str
    title: str
    description: str
    published: str
    channel_title: str


def normalize_channel_id(value: str) -> str:
    text = value.strip()
    if re.fullmatch(r"UC[a-zA-Z0-9_-]{22}", text):
        return text
    parsed = urlparse(text if "://" in text else "https://" + text)
    if parsed.hostname and parsed.hostname.lower() in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        match = re.match(r"^/channel/(UC[a-zA-Z0-9_-]{22})(?:/|$)", parsed.path)
        if match:
            return match.group(1)
    raise ValueError("Enter a YouTube channel ID (UC… with 24 characters) or a /channel/UC… URL. Handle URLs require a YouTube API key and are not accepted here.")


def fetch_channel_feed(channel_id: str, timeout: int = 20) -> tuple[str, list[FeedVideo]]:
    response = requests.get(YOUTUBE_FEED.format(channel_id), timeout=timeout,
                            headers={"User-Agent": "YT-Uploader/1.0 (+self-hosted authorized publisher)"})
    if response.status_code in (429, 500, 502, 503, 504):
        raise IntegrationError(f"YouTube feed returned HTTP {response.status_code}", retryable=True)
    if response.status_code != 200:
        raise IntegrationError(f"YouTube feed returned HTTP {response.status_code}")
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise IntegrationError("YouTube returned an unreadable channel feed", retryable=True) from exc
    channel = root.findtext("atom:title", default="", namespaces=YT_NS)
    entries: list[FeedVideo] = []
    for entry in root.findall("atom:entry", YT_NS):
        video_id = entry.findtext("yt:videoId", default="", namespaces=YT_NS)
        if not video_id:
            continue
        entries.append(FeedVideo(video_id=video_id,
                                 title=entry.findtext("atom:title", default="Untitled video", namespaces=YT_NS),
                                 description=entry.findtext("media:group/media:description", default="", namespaces={**YT_NS, "media": "http://search.yahoo.com/mrss/"}),
                                 published=entry.findtext("atom:published", default="", namespaces=YT_NS),
                                 channel_title=channel))
    return channel or channel_id, entries


class DownloadProvider:
    """Adapter for https://video-download-api.com/api/docs (SaveNow-compatible API)."""
    def __init__(self, api_key: str, timeout: int = 30, max_bytes: int = 4 * 1024**3,
                 download_dir: str | Path = "./downloads", base_url: str = PROVIDER_BASE,
                 poll_interval: float = 4.0, max_polls: int = 900):
        self.api_key, self.timeout, self.max_bytes = api_key, timeout, max_bytes
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        self.poll_interval, self.max_polls = poll_interval, max_polls

    def create_job(self, video_url: str) -> tuple[str, str]:
        try:
            response = requests.get(f"{self.base_url}/ajax/download.php",
                                   params={"url": video_url, "format": "mp4", "apikey": self.api_key,
                                           "allow_extended_duration": "0"}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise IntegrationError(f"Download provider request failed: {exc}", retryable=True) from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise IntegrationError(f"Download provider returned HTTP {response.status_code}", retryable=True)
        if not response.ok:
            raise IntegrationError(f"Download provider rejected the request (HTTP {response.status_code})")
        try:
            data = response.json()
        except ValueError as exc:
            raise IntegrationError("Download provider returned invalid JSON", retryable=True) from exc
        job_id = str(data.get("id", ""))
        if data.get("success") not in (True, 1, "1") or not job_id:
            reason = data.get("text") or data.get("message") or "Provider did not return a job id"
            raise IntegrationError(str(reason)[:500])
        return job_id, str((data.get("info") or {}).get("title") or "")

    def poll_job(self, provider_job_id: str) -> str | None:
        try:
            response = requests.get(f"{self.base_url}/ajax/progress.php", params={"id": provider_job_id}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise IntegrationError(f"Download status request failed: {exc}", retryable=True) from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise IntegrationError(f"Download provider returned HTTP {response.status_code}", retryable=True)
        if not response.ok:
            raise IntegrationError(f"Download provider status check failed (HTTP {response.status_code})")
        try:
            data = response.json()
        except ValueError as exc:
            raise IntegrationError("Download provider returned invalid status JSON", retryable=True) from exc
        progress = int(data.get("progress") or 0)
        if progress < 1000:
            return None
        url = data.get("download_url")
        if data.get("success") not in (True, 1, "1") or not url:
            raise IntegrationError(str(data.get("text") or data.get("message") or "Provider marked download unsuccessful")[:500])
        parsed = urlparse(str(url))
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
            raise IntegrationError("Provider returned an untrusted download URL; only HTTPS URLs from p.savenow.to are accepted")
        return str(url)

    def download(self, download_url: str, job_id: int, free_bytes: int | None = None) -> Path:
        parsed = urlparse(download_url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
            raise IntegrationError("Refusing to download from a non-allowlisted host")
        available = shutil.disk_usage(self.download_dir).free if free_bytes is None else free_bytes
        if available < min(self.max_bytes, 64 * 1024**2):
            raise IntegrationError("Insufficient free disk space for a video download")
        destination = self.download_dir / f"job-{job_id}-{int(time.time())}.mp4.part"
        final_path = destination.with_suffix("")
        total = 0
        try:
            with requests.get(download_url, stream=True, timeout=(self.timeout, 120), allow_redirects=False) as response:
                if response.is_redirect:
                    location = response.headers.get("Location", "")
                    redirect_url = requests.compat.urljoin(download_url, location)
                    redirect = urlparse(redirect_url)
                    if redirect.scheme != "https" or redirect.hostname not in ALLOWED_DOWNLOAD_HOSTS:
                        raise IntegrationError("Download provider redirected to an untrusted host")
                    response.close()
                    with requests.get(redirect_url, stream=True, timeout=(self.timeout, 120), allow_redirects=False) as redirected:
                        return self._stream_response(redirected, destination, final_path)
                return self._stream_response(response, destination, final_path)
        except requests.RequestException as exc:
            destination.unlink(missing_ok=True)
            raise IntegrationError(f"Media download failed: {exc}", retryable=True) from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def _stream_response(self, response: requests.Response, destination: Path, final_path: Path) -> Path:
        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise IntegrationError(f"Media server returned HTTP {response.status_code}", retryable=retryable)
        length = int(response.headers.get("Content-Length", "0") or 0)
        if length > self.max_bytes:
            raise IntegrationError("Video exceeds the configured maximum download size")
        if length and shutil.disk_usage(self.download_dir).free < length:
            raise IntegrationError("Not enough free disk space for this video")
        written = 0
        with destination.open("wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > self.max_bytes:
                    raise IntegrationError("Video exceeded the configured maximum download size while streaming")
                file.write(chunk)
        if written == 0:
            raise IntegrationError("Provider returned an empty media file")
        destination.replace(final_path)
        return final_path

    def wait_and_download(self, provider_job_id: str, job_id: int,
                          progress: Callable[[int], None] | None = None) -> Path:
        for _ in range(self.max_polls):
            url = self.poll_job(provider_job_id)
            if url:
                return self.download(url, job_id)
            if progress:
                progress(min(95, int(_ / max(1, self.max_polls) * 95)))
            time.sleep(self.poll_interval)
        raise IntegrationError("Timed out waiting for the provider download to finish", retryable=True)


def _configure_oauth_redirect_transport(redirect_uri: str) -> None:
    """Unconditionally permit OAuthlib's insecure transport mode as requested."""
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


def oauth_authorization_url(client_id: str, client_secret: str, redirect_uri: str, state: str) -> tuple[str, str]:
    """Build the Google consent URL and return its PKCE verifier for session storage."""
    _configure_oauth_redirect_transport(redirect_uri)
    code_verifier = secrets.token_urlsafe(64)
    flow = Flow.from_client_config({"web": {"client_id": client_id, "client_secret": client_secret,
          "auth_uri": "https://accounts.google.com/o/oauth2/auth",
          "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [redirect_uri]}},
          scopes=[YOUTUBE_SCOPE], state=state, redirect_uri=redirect_uri,
          code_verifier=code_verifier, autogenerate_code_verifier=False)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    return url, code_verifier


def oauth_exchange(client_id: str, client_secret: str, redirect_uri: str, state: str,
                   callback_url: str, code_verifier: str) -> str:
    """Exchange the authorization code using the verifier paired with its PKCE challenge."""
    _configure_oauth_redirect_transport(redirect_uri)
    if not code_verifier:
        raise IntegrationError("OAuth PKCE verifier is missing. Start Google sign-in again.")
    flow = Flow.from_client_config({"web": {"client_id": client_id, "client_secret": client_secret,
          "auth_uri": "https://accounts.google.com/o/oauth2/auth",
          "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [redirect_uri]}},
          scopes=[YOUTUBE_SCOPE], state=state, redirect_uri=redirect_uri,
          code_verifier=code_verifier, autogenerate_code_verifier=False)
    flow.fetch_token(authorization_response=callback_url)
    credentials = flow.credentials
    if not credentials.refresh_token:
        raise IntegrationError("Google did not return a refresh token. Revoke the app from your Google account and connect again.")
    return credentials.to_json()


def load_credentials(credentials_json: str) -> Credentials:
    try:
        return Credentials.from_authorized_user_info(json.loads(credentials_json), scopes=[YOUTUBE_SCOPE])
    except (ValueError, KeyError, TypeError) as exc:
        raise IntegrationError("Stored Google OAuth credentials are invalid") from exc


class YouTubeUploader:
    def __init__(self, credentials_json: str, on_credentials: Callable[[str], None] | None = None,
                 timeout: int = 60, chunk_size: int = 8 * 1024 * 1024):
        self.credentials = load_credentials(credentials_json)
        self.on_credentials = on_credentials
        self.timeout = timeout
        self.chunk_size = chunk_size

    def _access_token(self) -> str:
        if not self.credentials.valid:
            if not self.credentials.refresh_token:
                raise IntegrationError("Google authorization expired or was revoked; reconnect the destination account")
            try:
                self.credentials.refresh(GoogleRequest())
            except Exception as exc:
                raise IntegrationError(f"Google token refresh failed: {exc}") from exc
            if self.on_credentials:
                self.on_credentials(self.credentials.to_json())
        if not self.credentials.token:
            raise IntegrationError("Google access token is unavailable; reconnect the destination account")
        return self.credentials.token

    def upload(self, file_path: str | Path, metadata: dict, privacy: str,
               saved_session: str | None = None, save_session: Callable[[str | None], None] | None = None,
               on_progress: Callable[[int], None] | None = None,
               on_upload_start: Callable[[], None] | None = None) -> str:
        path = Path(file_path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise IntegrationError("The media file is missing or empty")
        if privacy not in {"private", "unlisted", "public"}:
            raise IntegrationError("Invalid YouTube visibility setting")
        total = path.stat().st_size
        body = {"snippet": {"title": metadata["title"][:100], "description": metadata.get("description", "")[:5000],
                            "tags": metadata.get("tags", [])[:500], "categoryId": str(metadata.get("category_id", "22"))},
                "status": {"privacyStatus": privacy}}
        session_url = saved_session
        headers = {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}
        if not session_url:
            if on_upload_start:
                on_upload_start()
            headers.update({"Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Length": str(total),
                            "X-Upload-Content-Type": "video/mp4"})
            try:
                response = requests.post("https://www.googleapis.com/upload/youtube/v3/videos",
                      params={"uploadType": "resumable", "part": "snippet,status", "notifySubscribers": "false"},
                      headers=headers, json=body, timeout=self.timeout)
            except requests.RequestException as exc:
                raise IntegrationError(f"Could not initialize YouTube upload: {exc}", retryable=True) from exc
            if response.status_code in (403, 400):
                raise IntegrationError(f"YouTube rejected upload initialization: {response.text[:600]}")
            if not response.ok or not response.headers.get("Location"):
                raise IntegrationError(f"YouTube upload initialization failed (HTTP {response.status_code})", retryable=response.status_code >= 500)
            session_url = response.headers["Location"]
            if save_session:
                save_session(session_url)
        uploaded, completed_video_id = self._probe(session_url, total, headers)
        if completed_video_id:
            if save_session:
                save_session(None)
            if on_progress:
                on_progress(100)
            return completed_video_id
        with path.open("rb") as file:
            file.seek(uploaded)
            while uploaded < total:
                chunk = file.read(min(self.chunk_size, total - uploaded))
                if not chunk:
                    raise IntegrationError("Unexpected end of media file during upload")
                start, end = uploaded, uploaded + len(chunk) - 1
                try:
                    response = requests.put(session_url, data=chunk, headers={**headers,
                        "Content-Type": "video/mp4", "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{total}"}, timeout=self.timeout)
                except requests.RequestException:
                    uploaded, completed_video_id = self._probe(session_url, total, headers)
                    if completed_video_id:
                        if save_session:
                            save_session(None)
                        if on_progress:
                            on_progress(100)
                        return completed_video_id
                    file.seek(uploaded)
                    continue
                if response.status_code in (200, 201):
                    result = response.json()
                    video_id = result.get("id")
                    if not video_id:
                        raise IntegrationError("YouTube completed upload but returned no video ID")
                    if save_session:
                        save_session(None)
                    if on_progress:
                        on_progress(100)
                    return video_id
                if response.status_code == 308:
                    uploaded = self._range_end(response.headers.get("Range"))
                    file.seek(uploaded)
                    if on_progress:
                        on_progress(min(99, int(uploaded * 100 / total)))
                    continue
                if response.status_code >= 500 or response.status_code == 429:
                    uploaded, completed_video_id = self._probe(session_url, total, headers)
                    if completed_video_id:
                        if save_session:
                            save_session(None)
                        if on_progress:
                            on_progress(100)
                        return completed_video_id
                    file.seek(uploaded)
                    continue
                raise IntegrationError(f"YouTube upload failed (HTTP {response.status_code}): {response.text[:500]}")
        raise IntegrationError("YouTube upload ended without a completion response", retryable=True)

    @staticmethod
    def _range_end(value: str | None) -> int:
        if not value:
            return 0
        match = re.search(r"bytes=0-(\d+)", value)
        return int(match.group(1)) + 1 if match else 0

    def _probe(self, session_url: str, total: int, headers: dict[str, str]) -> tuple[int, str | None]:
        response = requests.put(session_url, headers={**headers, "Content-Length": "0", "Content-Range": f"bytes */{total}"},
                                timeout=self.timeout)
        if response.status_code == 308:
            return self._range_end(response.headers.get("Range")), None
        if response.status_code in (200, 201):
            try:
                video_id = response.json().get("id")
            except (ValueError, AttributeError):
                video_id = None
            if video_id:
                return total, str(video_id)
            raise IntegrationError("YouTube reports the upload complete but returned no video ID; reconcile before retrying")
        if response.status_code >= 500 or response.status_code == 429:
            raise IntegrationError(f"Unable to verify resumable upload state (HTTP {response.status_code})", retryable=True)
        if response.status_code in (404, 410):
            raise IntegrationError("Resumable upload session expired; review the job before starting a new upload")
        return 0, None
