from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

from .config import settings
from .db import Database
from .integrations import IntegrationError, fetch_channel_feed, import_google_credentials_pickle, normalize_channel_id, oauth_authorization_url, oauth_exchange
from .security import decrypt_secret, encrypt_secret, hash_password, login_allowed, new_csrf_token, record_login_failure, clear_login_failures, valid_csrf, verify_password, redact_sensitive
from .worker import Worker

BASE_DIR = Path(__file__).resolve().parent
DB = Database(settings.database_path)
WORKER = Worker(DB)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

def _timestamp(value: int | None) -> str:
    if not value:
        return "—"
    from datetime import datetime
    try:
        return datetime.fromtimestamp(int(value)).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, OverflowError, OSError):
        return "—"

templates.env.filters["timestamp"] = _timestamp

DEFAULTS = {
    "privacy_status": "private",
    "category_id": "22",
    "description_prefix": "",
    "source_attribution": "Authorized republication",
    "upload_tags": "[]",
    "retry_limit": "5",
    "poll_interval_seconds": str(settings.poll_interval),
    "max_download_bytes": str(settings.max_download_bytes),
    "manual_approval": "true",
    "worker_enabled": "true" if settings.worker_enabled_on_start else "false",
}


def public_setting(key: str, default: str = "") -> str:
    value, secret = DB.get_setting(key)
    if value is None:
        return default
    if secret:
        return "configured" if value else ""
    return value


def stored_setting(key: str) -> str | None:
    value, secret = DB.get_setting(key)
    return decrypt_secret(value) if value and secret else value


def ensure_csrf(request: Request) -> str:
    if not request.session.get("csrf"):
        request.session["csrf"] = new_csrf_token()
    return str(request.session["csrf"])


def assert_csrf(request: Request, token: str | None) -> None:
    if not valid_csrf(request.session, token):
        raise HTTPException(status_code=403, detail="Invalid or expired CSRF token. Reload the page and try again.")


def require_login(request: Request) -> str:
    username = request.session.get("user")
    if not username:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return str(username)


def setting_value(key: str, default: str) -> str:
    value, _ = DB.get_setting(key)
    return value if value is not None else default


def initialize() -> None:
    DB.initialize()
    if not settings.admin_password or len(settings.admin_password) < 12:
        raise RuntimeError("Set ADMIN_PASSWORD to a unique value of at least 12 characters in the environment file")
    if not settings.session_secret or len(settings.session_secret) < 32:
        raise RuntimeError("Set SESSION_SECRET to a random value of at least 32 characters")
    if not settings.encryption_key:
        raise RuntimeError("Set ENCRYPTION_KEY (a Fernet key) before starting the app")
    if not DB.one("SELECT value FROM settings WHERE key='admin_password_hash'"):
        DB.set_setting("admin_password_hash", hash_password(settings.admin_password))
    for key, value in DEFAULTS.items():
        if DB.get_setting(key)[0] is None:
            DB.set_setting(key, value)
    # Do not leave plaintext admin password in process-level settings after initialization.
    DB.event("Application initialized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize()
    if WORKER.enabled():
        WORKER.start()
    try:
        yield
    finally:
        WORKER.stop()


app = FastAPI(title="YT Uploader", version="1.0.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret or "development-only-change-me", max_age=8 * 60 * 60,
                   same_site="lax", https_only=settings.cookie_secure, session_cookie="yt_uploader_session")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException):
    if exc.status_code == 303 and exc.headers and exc.headers.get("Location"):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if request.url.path.startswith("/api/") or request.url.path == "/health":
        return Response(content=json.dumps({"detail": exc.detail}), status_code=exc.status_code, media_type="application/json")
    return HTMLResponse(str(exc.detail), status_code=exc.status_code)


@app.get("/health")
def health():
    return {"status": "ok", "worker": WORKER.enabled(), "worker_heartbeat": WORKER.last_heartbeat,
            "database": "ok" if DB.one("SELECT 1 AS ok") else "error"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    csrf = ensure_csrf(request)
    return templates.TemplateResponse(request, "login.html", {"csrf": csrf, "error": error})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), csrf: str = Form(...)):
    assert_csrf(request, csrf)
    ip = request.client.host if request.client else "unknown"
    if not login_allowed(ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Wait five minutes and try again.")
    row = DB.one("SELECT value FROM settings WHERE key='admin_password_hash'")
    correct = row and username == settings.admin_username and verify_password(password, row["value"])
    if not correct:
        record_login_failure(ip)
        return RedirectResponse("/login?error=Invalid%20username%20or%20password", status_code=303)
    clear_login_failures(ip)
    request.session.clear()
    request.session["user"] = username
    request.session["csrf"] = new_csrf_token()
    DB.event("Administrator signed in")
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf: str = Form(...), username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def render_page(request: Request, page: str, message: str | None = None, error: str | None = None):
    username = require_login(request)
    message = message or request.query_params.get("message")
    error = error or request.query_params.get("error")
    csrf = ensure_csrf(request)
    counts = {row["status"]: row["n"] for row in DB.all("SELECT status,COUNT(*) n FROM jobs GROUP BY status")}
    data = {
        "request": request, "page": page, "username": username, "csrf": csrf, "message": message, "error": error,
        "sources": DB.all("SELECT * FROM sources ORDER BY created_at DESC"),
        "jobs": DB.all("SELECT j.*,COALESCE(s.title,j.source_title,j.source_channel_id) AS channel_title FROM jobs j LEFT JOIN sources s ON s.id=j.source_id ORDER BY j.created_at DESC LIMIT 200"),
        "events": DB.all("SELECT * FROM events ORDER BY created_at DESC LIMIT 100"),
        "counts": counts, "source_count": len(DB.all("SELECT id FROM sources")),
        "worker_enabled": WORKER.enabled(), "worker_heartbeat": WORKER.last_heartbeat,
        "db_path": str(settings.database_path), "configured_download_key": public_setting("download_api_key"),
        "configured_google_id": public_setting("google_client_id"), "configured_google_secret": public_setting("google_client_secret"),
        "oauth_connected": bool(DB.one("SELECT id FROM oauth_tokens WHERE id=1")),
        "oauth_redirect_uri": settings.oauth_redirect_uri,
        "privacy_status": setting_value("privacy_status", "private"),
        "category_id": setting_value("category_id", "22"),
        "description_prefix": setting_value("description_prefix", ""),
        "source_attribution": setting_value("source_attribution", "Authorized republication"),
        "upload_tags": setting_value("upload_tags", "[]"),
        "upload_tags_display": ", ".join(json.loads(setting_value("upload_tags", "[]")) if setting_value("upload_tags", "[]").startswith("[") else []),
        "retry_limit": setting_value("retry_limit", "5"),
        "poll_interval_seconds": setting_value("poll_interval_seconds", str(settings.poll_interval)),
        "max_download_mb": int(setting_value("max_download_bytes", str(settings.max_download_bytes))) // (1024 * 1024),
        "manual_approval": setting_value("manual_approval", "true") == "true",
        "source_authorization_required": True,
    }
    return templates.TemplateResponse(request, "dashboard.html", data)


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    return render_page(request, "overview")


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    return render_page(request, "sources")


@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request):
    return render_page(request, "queue")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return render_page(request, "settings")


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    return render_page(request, "logs")


@app.post("/sources")
def add_source(request: Request, channel: str = Form(...), authorization_note: str = Form(""), confirm_rights: str | None = Form(None), csrf: str = Form(...), username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    if confirm_rights != "yes":
        return RedirectResponse("/sources?error=Confirm%20you%20have%20permission%20to%20republish%20this%20channel%27s%20content", status_code=303)
    try:
        channel_id = normalize_channel_id(channel)
        channel_title, _ = fetch_channel_feed(channel_id, timeout=settings.request_timeout)
    except (ValueError, IntegrationError) as exc:
        return RedirectResponse("/sources?error=" + quote(str(exc)), status_code=303)
    if DB.one("SELECT id FROM sources WHERE channel_id=?", (channel_id,)):
        return RedirectResponse("/sources?error=That%20channel%20is%20already%20being%20monitored", status_code=303)
    now = int(time.time())
    try:
        source_id = DB.execute("INSERT INTO sources(channel_id,title,enabled,created_at,authorization_note) VALUES(?,?,1,?,?)",
                               (channel_id, channel_title, now, authorization_note.strip()[:1000]))
    except Exception:
        return RedirectResponse("/sources?error=Could%20not%20add%20source", status_code=303)
    DB.event(f"Source added: {channel_title}", source_id=source_id)
    return RedirectResponse("/sources?message=Source%20added.%20The%20first%20poll%20will%20establish%20a%20baseline%20and%20will%20not%20enqueue%20older%20videos", status_code=303)


@app.post("/sources/{source_id}/toggle")
def toggle_source(source_id: int, request: Request, csrf: str = Form(...), username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    source = DB.one("SELECT * FROM sources WHERE id=?", (source_id,))
    if not source:
        raise HTTPException(404, "Source not found")
    enabled = 0 if source["enabled"] else 1
    DB.execute("UPDATE sources SET enabled=? WHERE id=?", (enabled, source_id))
    DB.event("Source resumed" if enabled else "Source paused", source_id=source_id)
    return RedirectResponse("/sources", status_code=303)


@app.post("/sources/{source_id}/remove")
def remove_source(source_id: int, request: Request, csrf: str = Form(...), username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    source = DB.one("SELECT title FROM sources WHERE id=?", (source_id,))
    if not source:
        raise HTTPException(404, "Source not found")
    with DB.transaction(immediate=True) as conn:
        conn.execute("DELETE FROM seen_videos WHERE source_id=?", (source_id,))
        conn.execute("UPDATE jobs SET source_id=NULL WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        conn.execute("INSERT INTO events(severity,message,created_at) VALUES('info',?,?)",
                     (f"Source removed: {source['title']}; job history retained", int(time.time())))
    return RedirectResponse("/sources", status_code=303)


@app.post("/poll")
def poll_now(request: Request, csrf: str = Form(...), username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    try:
        count = WORKER.poll_sources()
        return RedirectResponse("/?message=" + quote(f"Poll complete. {count} new videos added to the queue."), status_code=303)
    except Exception as exc:
        return RedirectResponse("/?error=" + quote("Poll failed: " + redact_sensitive(exc, 300)), status_code=303)


@app.post("/worker/toggle")
def toggle_worker(request: Request, csrf: str = Form(...), username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    enabled = not WORKER.enabled()
    WORKER.set_enabled(enabled)
    if enabled:
        WORKER.start()
    return RedirectResponse("/?message=" + quote("Automation resumed" if enabled else "Automation paused; queued jobs are preserved"), status_code=303)


@app.post("/jobs/{job_id}/approve")
def approve_job(job_id: int, request: Request, csrf: str = Form(...), username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    changed = DB.execute("UPDATE jobs SET approved=1,status='queued',next_attempt_at=NULL,updated_at=? WHERE id=? AND status IN ('queued','retry')",
                         (int(time.time()), job_id))
    if not changed:
        return RedirectResponse("/queue?error=Job%20cannot%20be%20approved%20in%20its%20current%20state", status_code=303)
    DB.event("Administrator approved video for processing", job_id=job_id)
    return RedirectResponse("/queue?message=Job%20approved", status_code=303)


@app.post("/jobs/{job_id}/skip")
def skip_job(job_id: int, request: Request, csrf: str = Form(...), username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    changed = DB.execute("UPDATE jobs SET status='skipped',stage='skipped',approved=0,updated_at=? WHERE id=? AND status NOT IN ('completed','uploading')",
                         (int(time.time()), job_id))
    if not changed:
        return RedirectResponse("/queue?error=Job%20cannot%20be%20skipped%20in%20its%20current%20state", status_code=303)
    DB.event("Job skipped by administrator", job_id=job_id)
    return RedirectResponse("/queue", status_code=303)


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, request: Request, csrf: str = Form(...), username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    job = DB.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or job["status"] not in {"failed", "needs_review", "skipped"}:
        return RedirectResponse("/queue?error=Job%20is%20not%20eligible%20for%20manual%20retry", status_code=303)
    # An upload that reached YouTube but lost its final response is never retried blindly.
    if job["status"] == "needs_review" and job["upload_session_enc"]:
        return RedirectResponse("/queue?error=This%20job%20has%20an%20uncertain%20YouTube%20upload.%20Verify%20the%20destination%20channel%20before%20clearing%20the%20saved%20session.", status_code=303)
    DB.execute("UPDATE jobs SET status='queued',stage='queued',approved=1,error=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?",
               (int(time.time()), job_id))
    DB.event("Job manually retried", job_id=job_id)
    return RedirectResponse("/queue?message=Job%20queued%20for%20retry", status_code=303)


@app.post("/jobs/{job_id}/reconcile")
def reconcile_job(job_id: int, request: Request, decision: str = Form(...), destination_video_id: str = Form(""), verified_no_upload: str | None = Form(None), csrf: str = Form(...), username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    job = DB.one("SELECT * FROM jobs WHERE id=? AND status='needs_review'", (job_id,))
    if not job:
        return RedirectResponse("/queue?error=Job%20does%20not%20need%20reconciliation", status_code=303)
    if decision == "completed":
        if not destination_video_id.strip() or len(destination_video_id.strip()) > 128:
            return RedirectResponse("/queue?error=Enter%20the%20verified%20destination%20video%20ID", status_code=303)
        DB.execute("UPDATE jobs SET status='completed',stage='completed',progress=100,youtube_id=?,error=NULL,upload_session_enc=NULL,updated_at=? WHERE id=?",
                   (destination_video_id.strip(), int(time.time()), job_id))
        DB.event("Administrator recorded a verified destination upload", job_id=job_id)
    elif decision == "resume":
        if not job["upload_session_enc"]:
            return RedirectResponse("/queue?error=No%20saved%20resumable%20session%20is%20available", status_code=303)
        DB.execute("UPDATE jobs SET status='queued',stage='queued',approved=1,error=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?",
                   (int(time.time()), job_id))
        DB.event("Administrator resumed the existing resumable upload session", job_id=job_id)
    elif decision == "retry":
        if verified_no_upload != "yes":
            return RedirectResponse("/queue?error=Confirm%20that%20you%20verified%20no%20destination%20video%20was%20created", status_code=303)
        DB.execute("UPDATE jobs SET status='queued',stage='queued',approved=1,error=NULL,upload_session_enc=NULL,upload_started_at=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?",
                   (int(time.time()), job_id))
        DB.event("Administrator cleared uncertain upload after confirming no destination video was created", job_id=job_id)
    elif decision == "failed":
        DB.execute("UPDATE jobs SET status='failed',stage='failed',approved=0,upload_session_enc=NULL,updated_at=? WHERE id=?",
                   (int(time.time()), job_id))
    else:
        return RedirectResponse("/queue?error=Invalid%20reconciliation%20choice", status_code=303)
    return RedirectResponse("/queue", status_code=303)


@app.post("/settings")
def save_settings(request: Request, csrf: str = Form(...), privacy_status: str = Form("private"), category_id: str = Form("22"),
                  description_prefix: str = Form(""), source_attribution: str = Form(""), upload_tags: str = Form(""),
                  retry_limit: int = Form(5), poll_interval_seconds: int = Form(300), max_download_mb: int = Form(4096),
                  manual_approval: str | None = Form(None), download_api_key: str = Form(""), google_client_id: str = Form(""),
                  google_client_secret: str = Form(""), username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    if privacy_status not in {"private", "unlisted", "public"}:
        raise HTTPException(422, "Invalid visibility")
    if not (1 <= retry_limit <= 20 and 60 <= poll_interval_seconds <= 86400 and 100 <= max_download_mb <= 200000):
        raise HTTPException(422, "Retry, polling, or file size value is outside the allowed range")
    try:
        tags = [tag.strip()[:100] for tag in upload_tags.split(",") if tag.strip()][:30]
    except Exception:
        tags = []
    safe_values = {"privacy_status": privacy_status, "category_id": category_id[:8], "description_prefix": description_prefix[:1000],
                   "source_attribution": source_attribution[:300], "upload_tags": json.dumps(tags), "retry_limit": str(retry_limit),
                   "poll_interval_seconds": str(poll_interval_seconds), "max_download_bytes": str(max_download_mb * 1024 * 1024),
                   "manual_approval": "true" if manual_approval else "false"}
    for key, value in safe_values.items():
        DB.set_setting(key, value)
    for key, value in (("download_api_key", download_api_key.strip()), ("google_client_secret", google_client_secret.strip())):
        if value:
            DB.set_setting(key, encrypt_secret(value), is_secret=True)
    if google_client_id.strip():
        DB.set_setting("google_client_id", google_client_id.strip()[:500])
    DB.event("Application settings updated")
    return RedirectResponse("/settings?message=Settings%20saved.%20Secrets%20are%20stored%20encrypted%20and%20are%20not%20shown%20again", status_code=303)


@app.post("/oauth/start")
def oauth_start(request: Request, csrf: str = Form(...), username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    client_id = stored_setting("google_client_id")
    client_secret = stored_setting("google_client_secret")
    if not client_id or not client_secret:
        return RedirectResponse("/settings?error=Save%20your%20Google%20OAuth%20client%20ID%20and%20secret%20first", status_code=303)
    redirect_uri = settings.oauth_redirect_uri
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    try:
        url, code_verifier = oauth_authorization_url(client_id, client_secret, redirect_uri, state)
        request.session["oauth_code_verifier"] = code_verifier
    except Exception as exc:
        request.session.pop("oauth_state", None)
        request.session.pop("oauth_code_verifier", None)
        return RedirectResponse("/settings?error=" + quote(f"Could not start OAuth: {exc}"), status_code=303)
    return RedirectResponse(url, status_code=302)


@app.get("/oauth/callback")
def oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    expected = request.session.pop("oauth_state", None)
    code_verifier = request.session.pop("oauth_code_verifier", None)
    if error:
        raise HTTPException(400, "Google authorization was not completed")
    if not expected or not state or not secrets.compare_digest(str(expected), str(state)) or not code:
        raise HTTPException(400, "OAuth state check failed. Start the connection flow again.")
    if not code_verifier:
        raise HTTPException(400, "OAuth PKCE verifier is missing. Start Google sign-in again.")
    try:
        credentials_json = oauth_exchange(stored_setting("google_client_id") or "", stored_setting("google_client_secret") or "",
                                          settings.oauth_redirect_uri, state, str(request.url), code_verifier)
        encrypted = encrypt_secret(credentials_json)
        now = int(time.time())
        DB.execute("INSERT INTO oauth_tokens(id,credentials_enc,connected_at,updated_at) VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET credentials_enc=excluded.credentials_enc,updated_at=excluded.updated_at",
                   (encrypted, now, now))
        DB.event("Google destination account connected")
    except Exception as exc:
        raise HTTPException(400, "Google OAuth connection failed: " + redact_sensitive(exc, 500))
    return RedirectResponse("/settings?message=YouTube%20destination%20connected", status_code=303)


@app.post("/oauth/import-pickle")
async def oauth_import_pickle(request: Request, csrf: str = Form(...), token_file: UploadFile = File(...),
                              username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    filename = (token_file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if Path(filename).suffix.lower() not in {".pickle", ".pkl"}:
        raise HTTPException(422, "Choose a .pickle or .pkl Google credential file")
    payload = await token_file.read(1_000_001)
    await token_file.close()
    try:
        credentials_json = import_google_credentials_pickle(payload)
    except IntegrationError as exc:
        return RedirectResponse("/settings?error=" + quote(str(exc)), status_code=303)
    now = int(time.time())
    DB.execute("INSERT INTO oauth_tokens(id,credentials_enc,connected_at,updated_at) VALUES(1,?,?,?) "
               "ON CONFLICT(id) DO UPDATE SET credentials_enc=excluded.credentials_enc,connected_at=excluded.connected_at,updated_at=excluded.updated_at",
               (encrypt_secret(credentials_json), now, now))
    DB.event("Google token.pickle credentials imported by administrator")
    return RedirectResponse("/settings?message=Google%20credentials%20imported", status_code=303)


@app.post("/oauth/disconnect")
def oauth_disconnect(request: Request, csrf: str = Form(...), username: str = Depends(require_login)):
    assert_csrf(request, csrf)
    DB.execute("DELETE FROM oauth_tokens WHERE id=1")
    DB.event("Google destination account disconnected")
    return RedirectResponse("/settings", status_code=303)


@app.get("/api/overview")
def api_overview(request: Request, username: str = Depends(require_login)):
    return {"worker_enabled": WORKER.enabled(), "worker_heartbeat": WORKER.last_heartbeat,
            "sources": DB.one("SELECT COUNT(*) n FROM sources")["n"],
            "jobs": {row["status"]: row["n"] for row in DB.all("SELECT status,COUNT(*) n FROM jobs GROUP BY status")}}
