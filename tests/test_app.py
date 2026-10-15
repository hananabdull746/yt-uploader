from __future__ import annotations

import json
import os
import pickle
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import main
from app.db import Database
from app.config import Settings
from app.integrations import (DownloadProvider, IntegrationError, YouTubeUploader,
                              import_google_credentials_pickle, normalize_channel_id, oauth_authorization_url, oauth_exchange,
                              _configure_oauth_redirect_transport, YOUTUBE_SCOPE)
from google.oauth2.credentials import Credentials
from app.security import SecretBox, decrypt_secret, encrypt_secret, new_csrf_token, valid_csrf, redact_sensitive
from app.worker import Worker


VALID_CHANNEL = "UCabcdefghijklmnopqrstuv"


def test_channel_id_and_channel_url_parsing():
    assert normalize_channel_id(VALID_CHANNEL) == VALID_CHANNEL
    assert normalize_channel_id(f"https://www.youtube.com/channel/{VALID_CHANNEL}/videos") == VALID_CHANNEL
    with pytest.raises(ValueError):
        normalize_channel_id("https://www.youtube.com/@somehandle")
    with pytest.raises(ValueError):
        normalize_channel_id("not-a-channel")


def test_secret_encryption_and_wrong_key_detection():
    key_a = "dGVzdC1mb3Itc2VjcmV0LWVuY3J5cHRpb24ta2V5"  # invalid Fernet; derives a key
    box = SecretBox(key_a)
    encrypted = box.encrypt("api-token-value")
    assert encrypted != "api-token-value"
    assert box.decrypt(encrypted) == "api-token-value"
    with pytest.raises(ValueError):
        SecretBox("different key material").decrypt(encrypted)


def test_csrf_token_validation():
    token = new_csrf_token()
    assert valid_csrf({"csrf": token}, token)
    assert not valid_csrf({"csrf": token}, "forged")
    assert not valid_csrf({}, token)


def test_database_job_claim_is_unique_and_approval_gated(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    now = int(time.time())
    source_id = db.execute("INSERT INTO sources(channel_id,title,created_at) VALUES(?,?,?)", (VALID_CHANNEL, "Source", now))
    db.execute("INSERT INTO jobs(source_id,source_channel_id,video_id,source_url,title,status,approved,created_at,updated_at) VALUES(?,?,?,?,?,'queued',0,?,?)",
               (source_id, VALID_CHANNEL, "video1234567", "https://www.youtube.com/watch?v=video1234567", "Video", now, now))
    assert db.claim_next_job() is None
    db.execute("UPDATE jobs SET approved=1 WHERE video_id=?", ("video1234567",))
    claimed = db.claim_next_job()
    assert claimed["status"] == "downloading"
    assert claimed["attempts"] == 1
    assert db.claim_next_job() is None
    db.execute("INSERT INTO seen_videos(video_id,source_id,seen_at) VALUES(?,?,?)", ("video1234567", source_id, now))
    with pytest.raises(Exception):
        db.execute("INSERT INTO seen_videos(video_id,source_id,seen_at) VALUES(?,?,?)", ("video1234567", source_id, now))


def test_retry_jobs_respect_backoff(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    now = int(time.time())
    db.execute("INSERT INTO jobs(source_channel_id,video_id,source_url,title,status,approved,next_attempt_at,created_at,updated_at) VALUES(?,?,?,?, 'retry',1,?,?,?)",
               (VALID_CHANNEL, "v12345678901", "https://youtube.com/watch?v=v12345678901", "Test", now + 180, now, now))
    assert db.claim_next_job() is None


def test_provider_create_job_matches_documented_contract(monkeypatch, tmp_path):
    seen = {}
    def fake_get(url, params, timeout):
        seen.update(url=url, params=params)
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"success": True, "id": "provider-123", "info": {"title": "Allowed video"}})
    monkeypatch.setattr("app.integrations.requests.get", fake_get)
    provider = DownloadProvider("secret", download_dir=tmp_path)
    job_id, title = provider.create_job("https://www.youtube.com/watch?v=video1234567")
    assert job_id == "provider-123"
    assert title == "Allowed video"
    assert seen["url"].endswith("/ajax/download.php")
    assert seen["params"]["apikey"] == "secret"
    assert seen["params"]["format"] == "1080"
    assert seen["params"]["allow_extended_duration"] == "0"


def test_provider_status_requires_success_and_safe_url(monkeypatch, tmp_path):
    provider = DownloadProvider("key", download_dir=tmp_path)
    monkeypatch.setattr("app.integrations.requests.get", lambda *a, **k: SimpleNamespace(ok=True, status_code=200, json=lambda: {"success": 0, "progress": 499}))
    assert provider.poll_job("job") is None
    monkeypatch.setattr("app.integrations.requests.get", lambda *a, **k: SimpleNamespace(ok=True, status_code=200, json=lambda: {"success": 1, "progress": 1000, "download_url": "https://p.savenow.to/file.mp4"}))
    assert provider.poll_job("job").startswith("https://p.savenow.to/")
    monkeypatch.setattr("app.integrations.requests.get", lambda *a, **k: SimpleNamespace(ok=True, status_code=200, json=lambda: {"success": 1, "progress": 1000, "download_url": "https://leo3.savenow.to/file.mp4"}))
    assert provider.poll_job("job").startswith("https://leo3.savenow.to/")
    monkeypatch.setattr("app.integrations.requests.get", lambda *a, **k: SimpleNamespace(ok=True, status_code=200, json=lambda: {"success": 1, "progress": 1000, "download_url": "http://127.0.0.1/admin"}))
    with pytest.raises(IntegrationError, match="untrusted"):
        provider.poll_job("job")


def test_provider_rejects_failed_terminal_response(monkeypatch, tmp_path):
    provider = DownloadProvider("key", download_dir=tmp_path)
    monkeypatch.setattr("app.integrations.requests.get", lambda *a, **k: SimpleNamespace(ok=True, status_code=200, json=lambda: {"success": 0, "progress": 1000, "text": "Not available"}))
    with pytest.raises(IntegrationError, match="Not available"):
        provider.poll_job("job")


def test_media_streaming_enforces_maximum_size(tmp_path):
    provider = DownloadProvider("key", max_bytes=3, download_dir=tmp_path)
    response = SimpleNamespace(headers={"Content-Length": "4"}, status_code=200)
    response.raise_for_status = lambda: None
    response.iter_content = lambda chunk_size: iter([b"four"])
    with pytest.raises(IntegrationError, match="maximum"):
        provider._stream_response(response, tmp_path / "large.part", tmp_path / "large")


def test_oauth_callback_is_localhost_independent_of_base_url():
    settings_with_ip = Settings(base_url="http://168.107.75.253:8000", app_port=8000)
    settings_with_domain = Settings(base_url="https://uploader.example", app_port=8443)
    assert settings_with_ip.oauth_redirect_uri == "http://localhost:8000/oauth/callback"
    assert settings_with_domain.oauth_redirect_uri == "http://localhost:8443/oauth/callback"


def test_oauth_transport_guard_is_disabled_for_any_uri(monkeypatch):
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    _configure_oauth_redirect_transport("http://localhost/oauth/callback")
    assert __import__("os").environ["OAUTHLIB_INSECURE_TRANSPORT"] == "1"
    _configure_oauth_redirect_transport("https://example.com/oauth/callback")
    _configure_oauth_redirect_transport("custom://callback/path")
    assert __import__("os").environ["OAUTHLIB_INSECURE_TRANSPORT"] == "1"


def _sample_google_credentials():
    return Credentials(token="test-access", refresh_token="test-refresh",
                       token_uri="https://oauth2.googleapis.com/token",
                       client_id="test-client.apps.googleusercontent.com",
                       client_secret="test-client-secret", scopes=[YOUTUBE_SCOPE])


def test_restricted_token_pickle_import_accepts_google_credentials_and_rejects_code():
    parsed = json.loads(import_google_credentials_pickle(pickle.dumps(_sample_google_credentials())))
    assert parsed["refresh_token"] == "test-refresh"
    with pytest.raises(IntegrationError, match="Unsupported or invalid"):
        import_google_credentials_pickle(pickle.dumps(os.system))


def test_token_pickle_upload_is_authenticated_and_stores_encrypted_credentials(application):
    client, db, _ = application
    login_page = client.get("/login")
    import re
    csrf = re.search(r'name="csrf" value="([^\"]+)"', login_page.text).group(1)
    denied = client.post("/oauth/import-pickle", data={"csrf": csrf}, follow_redirects=False)
    assert denied.status_code == 303
    client.post("/login", data={"username": "admin", "password": "Test-Only-Password-8Dq!x9", "csrf": csrf})
    settings_page = client.get("/settings")
    csrf = re.search(r'name="csrf" value="([^\"]+)"', settings_page.text).group(1)
    response = client.post("/oauth/import-pickle", data={"csrf": csrf},
                           files={"token_file": ("token.pickle", pickle.dumps(_sample_google_credentials()), "application/octet-stream")},
                           follow_redirects=False)
    assert response.status_code == 303
    stored = db.one("SELECT credentials_enc FROM oauth_tokens WHERE id=1")
    assert json.loads(decrypt_secret(stored["credentials_enc"]))["refresh_token"] == "test-refresh"


def test_oauth_url_has_state_scope_and_matching_pkce_verifier():
    from urllib.parse import parse_qs, urlsplit
    import base64
    import hashlib
    url, verifier = oauth_authorization_url("client-id", "client-secret", "http://localhost/callback", "random-state")
    params = parse_qs(urlsplit(url).query)
    assert params["state"] == ["random-state"]
    assert "youtube.upload" in url
    assert params["access_type"] == ["offline"]
    assert params["code_challenge_method"] == ["S256"]
    expected_challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert params["code_challenge"] == [expected_challenge]


def test_oauth_exchange_reuses_original_pkce_verifier(monkeypatch):
    from app import integrations
    captured = {}
    class FakeFlow:
        credentials = SimpleNamespace(refresh_token="refresh", to_json=lambda: "{}")
        def fetch_token(self, **kwargs):
            captured["fetch"] = kwargs
    def factory(*args, **kwargs):
        captured["flow"] = kwargs
        return FakeFlow()
    monkeypatch.setattr(integrations.Flow, "from_client_config", factory)
    verifier = "v" * 64
    assert oauth_exchange("client", "secret", "http://localhost/callback", "state",
                          "http://localhost/callback?code=abc&state=state", verifier) == "{}"
    assert captured["flow"]["code_verifier"] == verifier
    assert captured["flow"]["autogenerate_code_verifier"] is False
    assert captured["fetch"]["authorization_response"].startswith("http://localhost/")


def test_oauth_callback_passes_session_pkce_verifier(application, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    client, db, _ = application
    monkeypatch.setattr(main, "stored_setting", lambda key: {
        "google_client_id": "client-id", "google_client_secret": "client-secret"}.get(key))
    verifier = "p" * 64
    monkeypatch.setattr(main, "oauth_authorization_url",
                        lambda client_id, client_secret, redirect_uri, state:
                        (f"https://accounts.google.com/o/oauth2/auth?state={state}", verifier))
    captured = {}
    creds_json = json.dumps({"token": "access", "refresh_token": "refresh",
                             "token_uri": "https://oauth2.googleapis.com/token",
                             "client_id": "client-id", "client_secret": "client-secret",
                             "scopes": [YOUTUBE_SCOPE]})
    def exchange(client_id, client_secret, redirect_uri, state, callback_url, code_verifier):
        captured["verifier"] = code_verifier
        return creds_json
    monkeypatch.setattr(main, "oauth_exchange", exchange)
    login_page = client.get("/login")
    import re
    csrf = re.search(r'name="csrf" value="([^\"]+)"', login_page.text).group(1)
    client.post("/login", data={"username": "admin", "password": "Test-Only-Password-8Dq!x9", "csrf": csrf})
    settings_page = client.get("/settings")
    csrf = re.search(r'name="csrf" value="([^\"]+)"', settings_page.text).group(1)
    started = client.post("/oauth/start", data={"csrf": csrf}, follow_redirects=False)
    assert started.status_code == 302
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    completed = client.get("/oauth/callback", params={"code": "auth-code", "state": state}, follow_redirects=False)
    assert completed.status_code == 303
    assert captured["verifier"] == verifier
    assert db.one("SELECT id FROM oauth_tokens WHERE id=1") is not None


def test_upload_range_parsing():
    assert YouTubeUploader._range_end("bytes=0-8388607") == 8388608
    assert YouTubeUploader._range_end(None) == 0
    assert YouTubeUploader._range_end("other") == 0


def test_resumable_probe_returns_completed_video_id(monkeypatch):
    response = SimpleNamespace(status_code=201, headers={}, json=lambda: {"id": "destination123"})
    monkeypatch.setattr("app.integrations.requests.put", lambda *a, **k: response)
    uploader = YouTubeUploader.__new__(YouTubeUploader)
    uploader.timeout = 10
    uploaded, video_id = uploader._probe("https://upload.example/session", 100, {"Authorization": "Bearer token"})
    assert uploaded == 100
    assert video_id == "destination123"


def test_error_redaction_removes_urls_and_credentials():
    safe = redact_sensitive("download failed https://p.savenow.to/storage/x?token=secret api_key=secret Bearer abc")
    assert "https://" not in safe
    assert "token=secret" not in safe
    assert "api_key=secret" not in safe
    assert "Bearer abc" not in safe


def test_source_baseline_does_not_enqueue_existing_videos(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    source_id = db.execute("INSERT INTO sources(channel_id,title,created_at) VALUES(?,?,?)", (VALID_CHANNEL, "placeholder", int(time.time())))
    feed = [SimpleNamespace(video_id="video00000001", title="Old one", description="", published="", channel_title="Creator"),
            SimpleNamespace(video_id="video00000002", title="Old two", description="", published="", channel_title="Creator")]
    monkeypatch.setattr("app.worker.fetch_channel_feed", lambda *a, **k: ("Creator", feed))
    worker = Worker(db)
    worker.poll_sources()
    assert db.one("SELECT initialized FROM sources WHERE id=?", (source_id,))["initialized"] == 1
    assert db.one("SELECT COUNT(*) n FROM jobs")["n"] == 0
    feed.insert(0, SimpleNamespace(video_id="video00000003", title="New one", description="desc", published="", channel_title="Creator"))
    assert worker.poll_sources() == 1
    job = db.one("SELECT * FROM jobs WHERE video_id='video00000003'")
    assert job["title"] == "New one"
    assert job["approved"] == 0
    assert db.one("SELECT COUNT(*) n FROM jobs")["n"] == 1
    assert worker.poll_sources() == 0
    db.set_setting("manual_approval", "false")
    feed.insert(0, SimpleNamespace(video_id="video00000004", title="Auto-approved", description="", published="", channel_title="Creator"))
    assert worker.poll_sources() == 1
    assert db.one("SELECT approved FROM jobs WHERE video_id='video00000004'")["approved"] == 1


def test_worker_reuses_persisted_media_instead_of_redownloading(monkeypatch, tmp_path):
    from app.config import settings
    media_root = Path(settings.downloads_path).resolve()
    media_root.mkdir(parents=True, exist_ok=True)
    media = media_root / f"persisted-{time.time_ns()}.mp4"
    media.write_bytes(b"tiny-test-video")
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    now = int(time.time())
    job_id = db.execute("INSERT INTO jobs(source_channel_id,video_id,source_url,title,status,approved,media_path,created_at,updated_at) VALUES(?,?,?,?, 'downloading',1,?,?,?)",
                        (VALID_CHANNEL, "reusevideo12345", "https://youtube.com/watch?v=reusevideo12345", "Saved file", str(media), now, now))
    db.execute("INSERT INTO oauth_tokens(id,credentials_enc,connected_at,updated_at) VALUES(1,?,?,?)", (encrypt_secret("not-used-by-fake"), now, now))

    class FakeUploader:
        def __init__(self, *args, **kwargs):
            pass
        def upload(self, file_path, metadata, privacy, **kwargs):
            assert Path(file_path) == media
            assert privacy == "private"
            return "destination-video-id"

    class UnexpectedProvider:
        def __init__(self, *args, **kwargs):
            raise AssertionError("A saved media file should be reused without another provider request")

    monkeypatch.setattr("app.worker.YouTubeUploader", FakeUploader)
    monkeypatch.setattr("app.worker.DownloadProvider", UnexpectedProvider)
    worker = Worker(db)
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    worker._process(job)
    result = db.one("SELECT status,youtube_id,media_path FROM jobs WHERE id=?", (job_id,))
    assert result["status"] == "completed"
    assert result["youtube_id"] == "destination-video-id"
    assert result["media_path"] is None
    assert not media.exists()


def test_worker_recovery_never_blindly_requeues_uncertain_upload(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    now = int(time.time())
    db.execute("INSERT INTO jobs(source_channel_id,video_id,source_url,title,status,approved,upload_session_enc,created_at,updated_at) VALUES(?,?,?,?, 'uploading',1,'encrypted-session',?,?)",
               (VALID_CHANNEL, "uploaduncertain1", "https://youtube.com/watch?v=uploaduncertain1", "Test", now, now))
    db.execute("INSERT INTO jobs(source_channel_id,video_id,source_url,title,status,approved,created_at,updated_at) VALUES(?,?,?,?, 'downloading',1,?,?)",
               (VALID_CHANNEL, "downloadactive1", "https://youtube.com/watch?v=downloadactive1", "Test", now, now))
    Worker(db).recover_after_restart()
    assert db.one("SELECT status FROM jobs WHERE video_id='uploaduncertain1'")["status"] == "needs_review"
    assert db.one("SELECT status FROM jobs WHERE video_id='downloadactive1'")["status"] == "retry"


def test_dashboard_login_requires_csrf_and_authentication(application):
    client, db, _ = application
    assert str(client.get("/").url).endswith("/login")
    login_page = client.get("/login")
    csrf = client.cookies.get("yt_uploader_session")
    assert login_page.status_code == 200
    # Obtain the actual CSRF token from the rendered hidden form.
    import re
    token = re.search(r'name="csrf" value="([^"]+)"', login_page.text).group(1)
    bad = client.post("/login", data={"username": "admin", "password": "Test-Only-Password-8Dq!x9", "csrf": "bad"}, follow_redirects=False)
    assert bad.status_code == 403
    response = client.post("/login", data={"username": "admin", "password": "Test-Only-Password-8Dq!x9", "csrf": token}, follow_redirects=True)
    assert response.status_code == 200
    assert "Overview" in response.text
    assert client.get("/api/overview").status_code == 200


def test_settings_secrets_are_never_rendered(application):
    client, db, _ = application
    login_page = client.get("/login")
    import re
    token = re.search(r'name="csrf" value="([^"]+)"', login_page.text).group(1)
    client.post("/login", data={"username": "admin", "password": "Test-Only-Password-8Dq!x9", "csrf": token})
    secret = "provider-key-secret-123"
    db.set_setting("download_api_key", encrypt_secret(secret), is_secret=True)
    response = client.get("/settings")
    assert response.status_code == 200
    assert secret not in response.text
    assert "Configured" in response.text


def test_reconcile_requires_explicit_no_upload_confirmation(application):
    client, db, _ = application
    now = int(time.time())
    db.execute("INSERT INTO jobs(source_channel_id,video_id,source_url,title,status,approved,upload_session_enc,created_at,updated_at) VALUES(?,?,?,?, 'needs_review',1,'encrypted-session',?,?)",
               (VALID_CHANNEL, "reviewupload123", "https://youtube.com/watch?v=reviewupload123", "Review me", now, now))
    login_page = client.get("/login")
    import re
    token = re.search(r'name="csrf" value="([^"]+)"', login_page.text).group(1)
    client.post("/login", data={"username": "admin", "password": "Test-Only-Password-8Dq!x9", "csrf": token})
    queue_page = client.get("/queue")
    token = re.search(r'name="csrf" value="([^"]+)"', queue_page.text).group(1)
    response = client.post("/jobs/1/reconcile", data={"decision": "retry", "verified_no_upload": "", "csrf": token}, follow_redirects=False)
    assert response.status_code == 303
    assert db.one("SELECT status FROM jobs WHERE id=1")["status"] == "needs_review"
    response = client.post("/jobs/1/reconcile", data={"decision": "resume", "csrf": token}, follow_redirects=False)
    assert response.status_code == 303
    row = db.one("SELECT status,upload_session_enc FROM jobs WHERE id=1")
    assert row["status"] == "queued"
    assert row["upload_session_enc"] == "encrypted-session"
