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
                              _configure_oauth_redirect_transport, YOUTUBE_SCOPE, download_best_thumbnail)
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


def test_download_best_thumbnail_fallback(monkeypatch, tmp_path):
    attempts = []
    def fake_get(url, timeout, headers):
        attempts.append(url)
        if "maxresdefault" in url:
            return SimpleNamespace(status_code=404, content=b"")
        if "sddefault" in url:
            return SimpleNamespace(status_code=404, content=b"")
        if "hqdefault" in url:
            return SimpleNamespace(status_code=200, content=b"x" * 1500)
        return SimpleNamespace(status_code=404, content=b"")

    monkeypatch.setattr("app.integrations.requests.get", fake_get)
    result = download_best_thumbnail("testvid123", tmp_path)
    assert result is not None
    assert result.is_file()
    assert result.stat().st_size == 1500
    assert any("maxresdefault" in a for a in attempts)
    assert any("hqdefault" in a for a in attempts)


def test_youtube_uploader_set_thumbnail(monkeypatch, tmp_path):
    captured = {}
    def fake_post(url, params, headers, data, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["data"] = data.read()
        return SimpleNamespace(status_code=200, json=lambda: {"kind": "youtube#thumbnailSetResponse"})

    monkeypatch.setattr("app.integrations.requests.post", fake_post)
    uploader = YouTubeUploader.__new__(YouTubeUploader)
    uploader._access_token = lambda: "fake-token"
    uploader.timeout = 15

    thumb_file = tmp_path / "thumb-sample.jpg"
    thumb_file.write_bytes(b"image-data-bytes")

    assert uploader.set_thumbnail("vid999", thumb_file) is True
    assert captured["params"]["videoId"] == "vid999"
    assert captured["params"]["uploadType"] == "media"
    assert captured["headers"]["Authorization"] == "Bearer fake-token"
    assert captured["headers"]["Content-Type"] == "image/jpeg"
    assert captured["data"] == b"image-data-bytes"


def test_database_initialization_includes_thumbnail_path(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    conn = db.connect()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    conn.close()
    assert "thumbnail_path" in columns


def test_users_table_and_rbac(tmp_path):
    from app.security import hash_password, verify_password
    db = Database(tmp_path / "test_users.db")
    db.initialize()

    # Create users with different roles
    user_id = db.create_user("creator_user", hash_password("SecretPass123!"), role="shorts_only")
    assert user_id > 0

    user = db.get_user_by_username("creator_user")
    assert user is not None
    assert user["role"] == "shorts_only"
    assert verify_password("SecretPass123!", user["password_hash"])
    assert not verify_password("WrongPass", user["password_hash"])

    # Update role to both
    db.update_user_role(user_id, "both")
    user_updated = db.get_user_by_id(user_id)
    assert user_updated["role"] == "both"

    # Update password
    db.update_user_password(user_id, hash_password("NewSecret456!"))
    user_pw_updated = db.get_user_by_id(user_id)
    assert verify_password("NewSecret456!", user_pw_updated["password_hash"])

    # List all users
    all_users = db.get_all_users()
    assert any(u["username"] == "creator_user" for u in all_users)

    # Delete user
    assert db.delete_user(user_id) is True
    assert db.get_user_by_id(user_id) is None


def test_three_part_transcript_splitting():
    from app.ai_shorts import ViralHookDetector
    detector = ViralHookDetector()

    # Create 90 simulated words spanning 90 seconds
    words = []
    for sec in range(90):
        words.append({
            "word": f"word_{sec}",
            "start": float(sec),
            "end": float(sec) + 0.8,
        })

    parts = detector.split_into_three_parts(words, total_duration=90.0)
    assert len(parts) == 3
    assert parts[0]["part_num"] == 1
    assert "Beginning" in parts[0]["label"]
    assert parts[1]["part_num"] == 2
    assert "Middle" in parts[1]["label"]
    assert parts[2]["part_num"] == 3
    assert "Climax" in parts[2]["label"]

    # Verify chronological bounds
    assert parts[0]["start"] < parts[1]["start"] < parts[2]["start"]
    assert len(parts[0]["words"]) > 0
    assert len(parts[1]["words"]) > 0
    assert len(parts[2]["words"]) > 0


def test_kinetic_subtitle_ass_generation(tmp_path):
    from app.ai_shorts import KineticSubtitleGenerator
    words = [
        {"word": "Look", "start": 10.0, "end": 10.4},
        {"word": "at", "start": 10.5, "end": 10.8},
        {"word": "this", "start": 10.9, "end": 11.3},
        {"word": "incredible", "start": 11.4, "end": 12.0},
        {"word": "viral", "start": 12.1, "end": 12.5},
        {"word": "moment", "start": 12.6, "end": 13.0},
    ]

    out_file = tmp_path / "subtitles.ass"
    res_path = KineticSubtitleGenerator.generate_ass(
        words=words,
        clip_start=10.0,
        clip_end=15.0,
        output_path=out_file,
        font_name="Montserrat",
        style_name="hormozi_yellow",
    )

    assert res_path.is_file()
    content = res_path.read_text(encoding="utf-8")

    assert "[Script Info]" in content
    assert "PlayResX: 1080" in content
    assert "PlayResY: 1920" in content
    assert "MarginV: 380" in content or ",380," in content  # Safety zone
    assert "\\fscx112\\fscy112" in content  # Kinetic pop animation
    assert "LOOK" in content  # Uppercase conversion
    assert "MOMENT" in content


def test_database_shorts_columns_and_job_types(tmp_path):
    db = Database(tmp_path / "test_shorts.db")
    db.initialize()
    conn = db.connect()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    conn.close()

    assert "job_type" in columns
    assert "clip_start" in columns
    assert "clip_end" in columns
    assert "parent_video_id" in columns
    assert "hook_summary" in columns


def test_video_engine_command_construction(monkeypatch, tmp_path):
    from app.video_engine import VideoEngine
    dummy_video = tmp_path / "dummy.mp4"
    dummy_video.write_bytes(b"dummy")
    out_video = tmp_path / "short.mp4"

    monkeypatch.setattr(VideoEngine, "get_ffmpeg_binary", lambda: "ffmpeg")

    executed_cmd = []
    def fake_subprocess_run(cmd, **kwargs):
        executed_cmd.extend(cmd)
        out_video.write_bytes(b"rendered_short")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("subprocess.run", fake_subprocess_run)

    # Test blur layout
    VideoEngine.render_short_clip(
        input_video=dummy_video,
        output_path=out_video,
        start_seconds=10.0,
        end_seconds=40.0,
        layout="blur",
    )
    assert "-threads" in executed_cmd
    assert "2" in executed_cmd
    assert "-preset" in executed_cmd
    assert "veryfast" in executed_cmd
    assert "-filter_complex" in executed_cmd
    assert "boxblur" in " ".join(executed_cmd)


def test_user_oauth_token_storage_and_cascade(tmp_path):
    db = Database(tmp_path / "test_user_oauth.db")
    db.initialize()

    # Create a user
    user_id = db.create_user("creator_yt", "Pass123456!", role="shorts_only")
    assert user_id > 0

    # Store per-user channel
    enc_creds = encrypt_secret('{"refresh_token":"user-refresh-token"}')
    db.set_user_oauth(user_id, enc_creds, channel_title="Creator Tech", channel_id="UC1234567890")

    token_info = db.get_user_oauth(user_id)
    assert token_info is not None
    assert token_info["channel_title"] == "Creator Tech"
    assert token_info["channel_id"] == "UC1234567890"
    assert decrypt_secret(token_info["credentials_enc"]) == '{"refresh_token":"user-refresh-token"}'

    # Check get_all_users list includes channel
    users = db.get_all_users()
    matched = [u for u in users if u["id"] == user_id][0]
    assert matched["has_channel"] == 1
    assert matched["channel_title"] == "Creator Tech"

    # Delete user and verify cascade
    db.delete_user(user_id)
    assert db.get_user_oauth(user_id) is None


def test_worker_routes_to_user_specific_channel(monkeypatch, tmp_path):
    db = Database(tmp_path / "test_worker_user.db")
    db.initialize()

    now = int(time.time())
    # System channel
    db.execute("INSERT INTO oauth_tokens(id,credentials_enc,connected_at,updated_at) VALUES(1,?,?,?)",
               (encrypt_secret(json.dumps({"token":"sys-tok","refresh_token":"sys-ref",
                                          "token_uri":"https://oauth2.googleapis.com/token",
                                          "client_id":"id","client_secret":"sec","scopes":[YOUTUBE_SCOPE]})), now, now))

    # User 2 channel
    user_id = db.create_user("user_creator", "SecretPass123!", role="shorts_only")
    db.set_user_oauth(user_id, encrypt_secret(json.dumps({"token":"user-tok","refresh_token":"user-ref",
                                                          "token_uri":"https://oauth2.googleapis.com/token",
                                                          "client_id":"id","client_secret":"sec","scopes":[YOUTUBE_SCOPE]})),
                      channel_title="User Channel", channel_id="UCUSER123")

    worker = Worker(db)

    # Job owned by user 2
    job_with_user = {"id": 1, "user_id": user_id, "title": "User Video"}
    uploader_user = worker._get_uploader_for_job(job_with_user)
    assert uploader_user.credentials.refresh_token == "user-ref"

    # Job without user_id (system fallback)
    job_system = {"id": 2, "user_id": None, "title": "System Video"}
    uploader_sys = worker._get_uploader_for_job(job_system)
    assert uploader_sys.credentials.refresh_token == "sys-ref"


def test_discovery_and_review_prompt_contract():
    from app.ai_shorts import DISCOVERY_PROMPT_TEMPLATE, REVIEW_PROMPT_TEMPLATE
    # Verify Discovery prompt placeholders
    assert "{{searchBudget}}" in DISCOVERY_PROMPT_TEMPLATE
    assert "{{targetCount}}" in DISCOVERY_PROMPT_TEMPLATE
    assert "{{minDuration}}" in DISCOVERY_PROMPT_TEMPLATE
    assert "{{maxDuration}}" in DISCOVERY_PROMPT_TEMPLATE
    assert "{{targetDuration}}" in DISCOVERY_PROMPT_TEMPLATE
    assert "{{excludedRangesInstruction}}" in DISCOVERY_PROMPT_TEMPLATE

    # Verify Review prompt 5 dimensions & evidence fields
    assert "hookStrength" in REVIEW_PROMPT_TEMPLATE
    assert "engagement" in REVIEW_PROMPT_TEMPLATE
    assert "standaloneClarity" in REVIEW_PROMPT_TEMPLATE
    assert "payoff" in REVIEW_PROMPT_TEMPLATE
    assert "specificity" in REVIEW_PROMPT_TEMPLATE
    assert "hookEvidence" in REVIEW_PROMPT_TEMPLATE
    assert "payoffEvidence" in REVIEW_PROMPT_TEMPLATE
    assert "problems" in REVIEW_PROMPT_TEMPLATE


def test_podcast_intro_detection_logic():
    from app.ai_shorts import ViralHookDetector

    intro_words = [
        {"word": "Welcome", "start": 0.5, "end": 1.0},
        {"word": "back", "start": 1.1, "end": 1.4},
        {"word": "to", "start": 1.5, "end": 1.7},
        {"word": "the", "start": 1.8, "end": 1.9},
        {"word": "show.", "start": 2.0, "end": 2.5},
        {"word": "Today's", "start": 2.8, "end": 3.2},
        {"word": "sponsor", "start": 3.3, "end": 3.9},
        {"word": "is", "start": 4.0, "end": 4.1},
        {"word": "SuperProduct.", "start": 4.2, "end": 5.0},
        {"word": "Use", "start": 5.2, "end": 5.5},
        {"word": "promo", "start": 5.6, "end": 6.0},
        {"word": "code", "start": 6.1, "end": 6.4},
        {"word": "SAVE20.", "start": 6.5, "end": 7.0},
        {"word": "Now", "start": 45.0, "end": 45.4},
        {"word": "let's", "start": 45.5, "end": 45.8},
        {"word": "talk", "start": 45.9, "end": 46.2},
        {"word": "about", "start": 46.3, "end": 46.5},
        {"word": "quantum", "start": 46.6, "end": 47.0},
        {"word": "computing.", "start": 47.1, "end": 48.0},
    ]

    intro_end = ViralHookDetector.detect_podcast_intro_end(intro_words, total_duration=300.0)
    assert intro_end >= 35.0

    # Clean words with no intro keywords in short clip
    clean_words = [
        {"word": "The", "start": 0.5, "end": 0.9},
        {"word": "secret", "start": 1.0, "end": 1.5},
        {"word": "to", "start": 1.6, "end": 1.8},
        {"word": "building", "start": 1.9, "end": 2.4},
        {"word": "great", "start": 2.5, "end": 2.9},
        {"word": "software", "start": 3.0, "end": 3.6},
        {"word": "is", "start": 3.7, "end": 3.9},
        {"word": "discipline.", "start": 4.0, "end": 4.7},
    ]
    assert ViralHookDetector.detect_podcast_intro_end(clean_words, total_duration=45.0) == 0.0


def test_viral_hook_detector_multi_clip_guarantees_minimum_five(monkeypatch):
    from app.ai_shorts import ViralHookDetector

    words = []
    # Generate 5 minutes of dummy timestamped speech
    for sec in range(0, 300, 2):
        words.append({
            "word": f"word_{sec}",
            "start": float(sec),
            "end": float(sec + 1.5),
        })

    detector = ViralHookDetector(groq_key="mock", gemini_key="mock")

    # Mock discovery to return candidate cuts
    def mock_llm(prompt):
        if "You are an editor finding compelling" in prompt:
            return {
                "clips": [
                    {"id": "cut_1", "start_seconds": 35.0, "end_seconds": 75.0, "title": "Clip 1 #Shorts", "description": "Desc 1", "hookText": "Opening hook 1", "concludingText": "Ending text 1"},
                    {"id": "cut_2", "start_seconds": 90.0, "end_seconds": 130.0, "title": "Clip 2 #Shorts", "description": "Desc 2", "hookText": "Opening hook 2", "concludingText": "Ending text 2"},
                ]
            }
        elif "You are a skeptical editor" in prompt:
            return {
                "reviews": [
                    {"id": "cut_1", "hookStrength": 88, "engagement": 85, "standaloneClarity": 90, "payoff": 85, "specificity": 82, "problems": [], "hookEvidence": "Opening hook 1", "payoffEvidence": "Ending text 1", "reason": "Exceptional moment"},
                    {"id": "cut_2", "hookStrength": 78, "engagement": 75, "standaloneClarity": 80, "payoff": 80, "specificity": 75, "problems": [], "hookEvidence": "Opening hook 2", "payoffEvidence": "Ending text 2", "reason": "Solid insight"},
                ]
            }
        return {}

    monkeypatch.setattr(detector, "_call_llm", mock_llm)

    clips = detector.detect_clips(words, total_duration=300.0, video_title="Full Podcast", target_count=5, skip_intro=True)
    assert len(clips) >= 5
    # Verify all clips have necessary fields
    for c in clips:
        assert c["start"] >= 0.0
        assert c["end"] > c["start"]
        assert c["duration"] >= 20.0
        assert c["title"].endswith("#Shorts")
        assert "virality_score" in c


def test_kinetic_subtitle_generator_styles_and_positions(tmp_path):
    from app.ai_shorts import KineticSubtitleGenerator

    words = [
        {"word": "Unbelievable", "start": 1.0, "end": 1.6},
        {"word": "results", "start": 1.7, "end": 2.2},
        {"word": "happened", "start": 2.3, "end": 2.8},
        {"word": "today.", "start": 2.9, "end": 3.5},
    ]

    for style in ("hormozi_yellow", "neon_cyan", "viral_mint", "classic_white", "bold_red"):
        for pos in ("lower_third", "center", "bottom"):
            sub_file = tmp_path / f"sub_{style}_{pos}.ass"
            generated = KineticSubtitleGenerator.generate_ass(
                words=words, clip_start=0.0, clip_end=5.0,
                output_path=sub_file, font_name="Montserrat",
                style_name=style, position=pos
            )
            assert generated.is_file()
            content = generated.read_text(encoding="utf-8")
            assert "[Script Info]" in content
            assert "[V4+ Styles]" in content
            assert "[Events]" in content
            assert "UNBELIEVABLE" in content


def test_video_engine_face_tracking_resilience(tmp_path):
    from app.video_engine import VideoEngine

    # Test on a dummy path (should not crash, gracefully returns 0.5 center ratio)
    dummy_video = tmp_path / "non_existent.mp4"
    ratio = VideoEngine.detect_face_center_ratio(dummy_video, start_seconds=0.0, duration=10.0)
    assert ratio == 0.5


def test_normalize_youtube_url():
    from app.integrations import normalize_youtube_url, extract_youtube_id

    # standard watch URL with tracking and query params
    url1 = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=120s&list=PL12345678"
    assert extract_youtube_id(url1) == "dQw4w9WgXcQ"
    assert normalize_youtube_url(url1) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    # short youtu.be URL with tracking param
    url2 = "https://youtu.be/dQw4w9WgXcQ?si=abcdef12345"
    assert extract_youtube_id(url2) == "dQw4w9WgXcQ"
    assert normalize_youtube_url(url2) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    # shorts URL
    url3 = "https://youtube.com/shorts/dQw4w9WgXcQ?feature=share"
    assert extract_youtube_id(url3) == "dQw4w9WgXcQ"
    assert normalize_youtube_url(url3) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    # raw ID
    assert extract_youtube_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert normalize_youtube_url("dQw4w9WgXcQ") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_provider_format_fallback(monkeypatch, tmp_path):
    calls = []
    def fake_get(url, params, timeout):
        calls.append(params["format"])
        if params["format"] == "1080":
            # Simulate 1080 failure
            return SimpleNamespace(ok=True, status_code=200, json=lambda: {"success": 0, "text": "Cipher/format not available"})
        # 720 succeeds
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"success": 1, "id": "job-720-ok", "info": {"title": "Fallback Video"}})

    monkeypatch.setattr("app.integrations.requests.get", fake_get)
    provider = DownloadProvider("test-key", download_dir=tmp_path)
    job_id, title = provider.create_job("https://youtu.be/dQw4w9WgXcQ?si=test", format_id="1080")
    assert job_id == "job-720-ok"
    assert title == "Fallback Video"
    assert calls == ["1080", "720"]


def test_provider_login_required_error(monkeypatch, tmp_path):
    def fake_get(url, params, timeout):
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"success": 0, "text": "This content requires login and cannot be downloaded."})

    monkeypatch.setattr("app.integrations.requests.get", fake_get)
    provider = DownloadProvider("test-key", download_dir=tmp_path)
    with pytest.raises(IntegrationError) as exc_info:
        provider.create_job("https://youtu.be/dQw4w9WgXcQ", format_id="1080")
    assert not exc_info.value.retryable
    assert "Age-Restricted" in str(exc_info.value)
    assert "Video-Download API rejected video" in str(exc_info.value)




