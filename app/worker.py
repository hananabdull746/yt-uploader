from __future__ import annotations

import random
import threading
import time
from pathlib import Path
from typing import Any

from .config import settings
from .db import Database
from .integrations import DownloadProvider, IntegrationError, YouTubeUploader, fetch_channel_feed, download_best_thumbnail
from .security import decrypt_secret, encrypt_secret, redact_sensitive
from .ai_shorts import DeepgramClient, KineticSubtitleGenerator, ViralHookDetector
from .video_engine import VideoEngine


class Worker:
    def __init__(self, db: Database):
        self.db = db
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_poll = 0.0
        self._cycle_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self.last_heartbeat = int(time.time())

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.recover_after_restart()
        self.cleanup_orphaned_media()
        self._thread = threading.Thread(target=self._run, name="yt-uploader-worker", daemon=True)
        self._thread.start()
        self.db.event("Background worker started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        self.db.event("Background worker stopped")

    def enabled(self) -> bool:
        row = self.db.one("SELECT value FROM settings WHERE key='worker_enabled'")
        return bool(row and row["value"] == "true")

    def set_enabled(self, enabled: bool) -> None:
        self.db.set_setting("worker_enabled", "true" if enabled else "false")
        self.db.event("Automation enabled" if enabled else "Automation paused")

    def _run(self) -> None:
        while not self._stop.is_set():
            self.last_heartbeat = int(time.time())
            if self.enabled():
                now = time.monotonic()
                if now - self._last_poll >= int(self.db.get_setting("poll_interval_seconds")[0] or settings.poll_interval):
                    try:
                        self.poll_sources()
                    except Exception as exc:
                        self.db.event(f"Source polling cycle failed: {type(exc).__name__}", "error")
                    self._last_poll = time.monotonic()
                try:
                    self.process_one_job()
                except Exception as exc:
                    self.db.event(f"Worker job cycle failed: {type(exc).__name__}", "error")
            self._stop.wait(2)

    def recover_after_restart(self) -> None:
        now = int(time.time())
        with self.db.transaction(immediate=True) as conn:
            # Downloads can be retried through the provider job ID; uploads are uncertain and must never be blindly repeated.
            conn.execute("UPDATE jobs SET status='retry',stage='recovery',error='Worker restarted during download; safe retry scheduled',updated_at=? WHERE status='downloading'", (now,))
            conn.execute("UPDATE jobs SET status='needs_review',stage='recovery',error='Worker restarted during upload; verify the destination channel before retrying',updated_at=? WHERE status='uploading'", (now,))
        self.db.event("Unfinished jobs reconciled after startup")

    def cleanup_orphaned_media(self, partial_age_seconds: int = 24 * 3600, orphan_age_seconds: int = 7 * 24 * 3600) -> int:
        root = Path(settings.downloads_path)
        if not root.exists():
            return 0
        now = time.time()
        referenced = {str(Path(row["media_path"]).resolve()) for row in self.db.all("SELECT media_path FROM jobs WHERE media_path IS NOT NULL")}
        referenced.update(str(Path(row["thumbnail_path"]).resolve()) for row in self.db.all("SELECT thumbnail_path FROM jobs WHERE thumbnail_path IS NOT NULL"))
        removed = 0
        for path in root.iterdir():
            if not path.is_file():
                continue
            age = now - path.stat().st_mtime
            stale_partial = path.name.endswith(".part") and age > partial_age_seconds
            stale_orphan = path.suffix in {".mp4", ".jpg", ".jpeg"} and age > orphan_age_seconds and str(path.resolve()) not in referenced
            if stale_partial or stale_orphan:
                path.unlink(missing_ok=True)
                removed += 1
        if removed:
            self.db.event(f"Removed {removed} stale temporary media file(s)")
        return removed

    def poll_sources(self) -> int:
        if not self._poll_lock.acquire(blocking=False):
            return 0
        try:
            return self._poll_sources_unlocked()
        finally:
            self._poll_lock.release()

    def _poll_sources_unlocked(self) -> int:
        sources = self.db.all("SELECT * FROM sources WHERE enabled=1 ORDER BY id")
        discovered = 0
        for source in sources:
            if self._stop.is_set():
                break
            try:
                channel_title, videos = fetch_channel_feed(source["channel_id"], timeout=settings.request_timeout)
                now = int(time.time())
                manual_approval = self.db.get_setting("manual_approval")[0] != "false"
                approved = 0 if manual_approval else 1
                with self.db.transaction(immediate=True) as conn:
                    current = conn.execute("SELECT initialized FROM sources WHERE id=?", (source["id"],)).fetchone()
                    if not current:
                        continue
                    if not current["initialized"]:
                        # Baseline current feed contents so old videos are never unexpectedly queued.
                        for video in videos:
                            conn.execute("INSERT OR IGNORE INTO seen_videos(video_id,source_id,seen_at,is_baseline) VALUES(?,?,?,1)",
                                         (video.video_id, source["id"], now))
                        conn.execute("UPDATE sources SET title=?,initialized=1,last_polled_at=?,last_video_id=? WHERE id=?",
                                     (channel_title, now, videos[0].video_id if videos else None, source["id"]))
                        conn.execute("INSERT INTO events(severity,message,source_id,created_at) VALUES('info',?,?,?)",
                                     (f"Source baseline established for {channel_title} ({len(videos)} feed entries)", source["id"], now))
                        continue
                    for video in reversed(videos):
                        cursor = conn.execute("INSERT OR IGNORE INTO seen_videos(video_id,source_id,seen_at,is_baseline) VALUES(?,?,?,0)",
                                              (video.video_id, source["id"], now))
                        if cursor.rowcount:
                            conn.execute("INSERT OR IGNORE INTO jobs(source_id,source_channel_id,source_title,video_id,source_url,title,description,status,approved,created_at,updated_at) "
                                         "VALUES(?,?,?,?,?,?,?,'queued',?,?,?)",
                                         (source["id"], source["channel_id"], channel_title, video.video_id,
                                          f"https://www.youtube.com/watch?v={video.video_id}", video.title,
                                          video.description, approved, now, now))
                            discovered += 1
                            conn.execute("INSERT INTO events(severity,message,source_id,created_at) VALUES('info',?,?,?)",
                                         (f"New video detected: {video.title[:160]}", source["id"], now))
                    conn.execute("UPDATE sources SET title=?,last_polled_at=?,last_video_id=? WHERE id=?",
                                 (channel_title, now, videos[0].video_id if videos else source.get("last_video_id"), source["id"]))
            except IntegrationError as exc:
                self.db.event(f"Source poll failed: {exc}", "warning", source_id=source["id"])
            except Exception as exc:
                self.db.event(f"Source poll failed: {type(exc).__name__}", "warning", source_id=source["id"])
        return discovered

    def process_one_job(self) -> bool:
        if not self._cycle_lock.acquire(blocking=False):
            return False
        try:
            job = self.db.claim_next_job()
            if not job:
                return False
            self._process(job)
            return True
        finally:
            self._cycle_lock.release()

    def _secret_setting(self, key: str) -> str | None:
        value, is_secret = self.db.get_setting(key)
        return decrypt_secret(value) if value and is_secret else value

    def _process(self, job: dict[str, Any]) -> None:
        if job.get("job_type") == "short":
            self._process_short(job)
            return

        job_id = job["id"]
        try:
            media_path = None
            saved_path = job.get("media_path")
            if saved_path:
                candidate = Path(saved_path).resolve()
                download_root = Path(settings.downloads_path).resolve()
                if candidate.is_relative_to(download_root) and candidate.is_file() and candidate.stat().st_size > 0:
                    media_path = candidate
            if media_path is None:
                api_key = self._secret_setting("download_api_key")
                if not api_key:
                    raise IntegrationError("Configure the video-download API key before processing jobs")
                provider = DownloadProvider(api_key, settings.request_timeout, int(self.db.get_setting("max_download_bytes")[0] or settings.max_download_bytes), settings.downloads_path)
                provider_job_id = job.get("provider_job_id")
                if not provider_job_id:
                    self.db.event("Download started", job_id=job_id)
                    provider_job_id, provider_title = provider.create_job(job["source_url"])
                    self.db.execute("UPDATE jobs SET provider_job_id=?,updated_at=? WHERE id=?", (provider_job_id, int(time.time()), job_id))
                    if provider_title:
                        self.db.execute("UPDATE jobs SET title=? WHERE id=? AND (title='' OR title='Untitled video')", (provider_title, job_id))
                media_path = provider.wait_and_download(provider_job_id, job_id,
                           progress=lambda p: self._update_progress(job_id, p, "downloading"))
                self.db.execute("UPDATE jobs SET media_path=?,updated_at=? WHERE id=?",
                                (str(media_path), int(time.time()), job_id))
            thumbnail_path = None
            saved_thumb = job.get("thumbnail_path")
            if saved_thumb:
                cand_thumb = Path(saved_thumb).resolve()
                if cand_thumb.is_relative_to(download_root) and cand_thumb.is_file() and cand_thumb.stat().st_size > 0:
                    thumbnail_path = cand_thumb
            if thumbnail_path is None and job.get("video_id"):
                thumbnail_path = download_best_thumbnail(job["video_id"], settings.downloads_path, timeout=settings.request_timeout)
                if thumbnail_path:
                    self.db.execute("UPDATE jobs SET thumbnail_path=?,updated_at=? WHERE id=?",
                                    (str(thumbnail_path), int(time.time()), job_id))
            uploader = self._get_uploader_for_job(job)

            def save_session(uri: str | None) -> None:
                encrypted = encrypt_secret(uri) if uri else None
                now = int(time.time())
                self.db.execute("UPDATE jobs SET upload_session_enc=?,upload_started_at=?,updated_at=? WHERE id=?",
                                (encrypted, now if uri else None, now, job_id))

            stored_session = self.db.one("SELECT upload_session_enc FROM jobs WHERE id=?", (job_id,))
            session_uri = decrypt_secret(stored_session["upload_session_enc"]) if stored_session and stored_session["upload_session_enc"] else None
            settings_row = self._metadata_settings()
            self.db.execute("UPDATE jobs SET status='uploading',stage='uploading',progress=0,updated_at=? WHERE id=?",
                            (int(time.time()), job_id))
            self.db.event("Upload started", job_id=job_id)

            def mark_upload_started() -> None:
                self.db.execute("UPDATE jobs SET upload_started_at=?,updated_at=? WHERE id=?",
                                (int(time.time()), int(time.time()), job_id))

            video_id = uploader.upload(media_path, {
                "title": job["title"],
                "description": self._format_description(job.get("description", ""), settings_row),
                "tags": settings_row.get("upload_tags", []),
                "category_id": settings_row.get("category_id", "22")
            }, settings_row.get("privacy_status", "private"), saved_session=session_uri,
               save_session=save_session, on_progress=lambda p: self._update_progress(job_id, p, "uploading"),
               on_upload_start=mark_upload_started)
            if thumbnail_path and thumbnail_path.is_file():
                try:
                    uploader.set_thumbnail(video_id, thumbnail_path)
                    self.db.event(f"Custom thumbnail uploaded for video {video_id}", job_id=job_id)
                except Exception as thumb_err:
                    self.db.event(f"Thumbnail upload notice: {thumb_err}", severity="warning", job_id=job_id)
                finally:
                    thumbnail_path.unlink(missing_ok=True)
            now = int(time.time())
            self.db.execute("UPDATE jobs SET status='completed',stage='completed',progress=100,youtube_id=?,error=NULL,upload_session_enc=NULL,upload_started_at=NULL,media_path=NULL,thumbnail_path=NULL,updated_at=? WHERE id=?",
                            (video_id, now, job_id))
            media_path.unlink(missing_ok=True)
            self.db.event(f"Upload completed; destination video ID {video_id}", job_id=job_id)

            # Auto-repurpose feature: enqueue a Short job if enabled
            try:
                if self.db.get_setting("shorts_auto_repurpose")[0] == "true":
                    existing = self.db.one("SELECT id FROM jobs WHERE parent_video_id=? OR video_id=?",
                                          (job["video_id"], f"{job['video_id']}_short"))
                    if not existing:
                        now_ts = int(time.time())
                        self.db.execute(
                            "INSERT INTO jobs(source_id, source_channel_id, source_title, video_id, source_url, "
                            "title, status, approved, job_type, parent_video_id, created_at, updated_at) "
                            "VALUES(?,?,?,?,?,?,'queued',0,'short',?,?,?)",
                            (job.get("source_id"), job.get("source_channel_id"), job.get("source_title", ""),
                             f"{job['video_id']}_short", job["source_url"], f"[Short] {job['title']}",
                             job["video_id"], now_ts, now_ts)
                        )
                        self.db.event(f"Auto-generated Short job queued for video {job['video_id']}", job_id=job_id)
            except Exception as auto_err:
                self.db.event(f"Auto-repurpose notice: {auto_err}", severity="warning", job_id=job_id)

        except IntegrationError as exc:
            self._fail(job, str(exc), retryable=exc.retryable)
        except Exception as exc:
            self._fail(job, f"Unexpected {type(exc).__name__}: {str(exc)[:350]}", retryable=True)

    def _process_short(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        try:
            clip_start = job.get("clip_start")
            clip_end = job.get("clip_end")
            words: list[dict[str, Any]] = []

            # 1. AI Hook Detection Phase (Deepgram + Groq / Gemini 3-part split)
            if clip_start is None or clip_end is None:
                self.db.execute("UPDATE jobs SET stage='analyzing_audio',progress=10,updated_at=? WHERE id=?",
                                (int(time.time()), job_id))
                self.db.event("Downloading audio and transcribing via Deepgram Nova-2", job_id=job_id)

                deepgram_key = self._secret_setting("deepgram_api_key")
                if not deepgram_key:
                    raise IntegrationError("Configure Deepgram API key in Settings before generating Shorts")
                groq_key = self._secret_setting("groq_api_key") or ""
                gemini_key = self._secret_setting("gemini_api_key") or ""
                if not groq_key and not gemini_key:
                    raise IntegrationError("Configure Groq or Gemini API key in Settings for AI hook detection")

                api_key = self._secret_setting("download_api_key")
                if not api_key:
                    raise IntegrationError("Configure the video-download API key before processing jobs")
                provider = DownloadProvider(api_key, settings.request_timeout, int(self.db.get_setting("max_download_bytes")[0] or settings.max_download_bytes), settings.downloads_path)

                audio_job_id, _ = provider.create_job(job["source_url"], format_id="mp3")
                audio_path = provider.wait_and_download(audio_job_id, job_id,
                                                       progress=lambda p: self._update_progress(job_id, int(p * 0.3), "analyzing_audio"))

                try:
                    dg = DeepgramClient(deepgram_key, timeout=settings.request_timeout * 2)
                    transcript_data = dg.transcribe_audio_file(audio_path)
                    words = transcript_data.get("words") or []
                    total_dur = transcript_data.get("duration") or 0.0

                    self.db.execute("UPDATE jobs SET stage='detecting_hooks',progress=35,updated_at=? WHERE id=?",
                                    (int(time.time()), job_id))
                    self.db.event(f"Transcribed {len(words)} words. Analyzing 3-part segments for viral hooks", job_id=job_id)

                    detector = ViralHookDetector(groq_key=groq_key, gemini_key=gemini_key, timeout=settings.request_timeout)
                    best_clip = detector.detect_best_short(words, total_dur, job.get("title", ""))

                    clip_start = best_clip["start"]
                    clip_end = best_clip["end"]
                    new_title = best_clip["title"]
                    new_desc = best_clip["description"]
                    hook_summary = f"{best_clip['hook_reason']} (Score: {best_clip['virality_score']}/100)"

                    self.db.execute(
                        "UPDATE jobs SET clip_start=?,clip_end=?,title=?,description=?,hook_summary=?,updated_at=? WHERE id=?",
                        (clip_start, clip_end, new_title, new_desc, hook_summary, int(time.time()), job_id)
                    )
                    self.db.event(f"Selected viral Short hook: {clip_start:.1f}s -> {clip_end:.1f}s: {new_title}", job_id=job_id)
                finally:
                    audio_path.unlink(missing_ok=True)

            # 2. 9:16 Video Rendering Phase with Kinetic Subtitles
            self.db.execute("UPDATE jobs SET stage='rendering_short',progress=50,updated_at=? WHERE id=?",
                            (int(time.time()), job_id))
            self.db.event(f"Downloading source video and rendering 9:16 vertical Short ({clip_start:.1f}s -> {clip_end:.1f}s)", job_id=job_id)

            api_key = self._secret_setting("download_api_key")
            provider = DownloadProvider(api_key, settings.request_timeout, int(self.db.get_setting("max_download_bytes")[0] or settings.max_download_bytes), settings.downloads_path)

            video_job_id, _ = provider.create_job(job["source_url"], format_id="1080")
            raw_video = provider.wait_and_download(video_job_id, job_id,
                                                  progress=lambda p: self._update_progress(job_id, 50 + int(p * 0.2), "rendering_short"))

            short_output_path = Path(settings.downloads_path) / f"short_{job_id}_{int(time.time())}.mp4"
            ass_path = Path(settings.downloads_path) / f"sub_{job_id}_{int(time.time())}.ass"

            try:
                layout = self.db.get_setting("shorts_layout")[0] or "blur"
                font = self.db.get_setting("shorts_font")[0] or "Montserrat"
                caption_style = self.db.get_setting("shorts_caption_style")[0] or "hormozi_yellow"

                subtitles_file = None
                if words:
                    subtitles_file = KineticSubtitleGenerator.generate_ass(
                        words, clip_start, clip_end, ass_path, font_name=font, style_name=caption_style
                    )

                VideoEngine.render_short_clip(
                    input_video=raw_video,
                    output_path=short_output_path,
                    start_seconds=clip_start,
                    end_seconds=clip_end,
                    layout=layout,
                    subtitles_ass_path=subtitles_file
                )
            finally:
                raw_video.unlink(missing_ok=True)
                ass_path.unlink(missing_ok=True)

            self.db.execute("UPDATE jobs SET media_path=?,updated_at=? WHERE id=?",
                            (str(short_output_path), int(time.time()), job_id))

            # 3. YouTube Shorts Upload Phase
            uploader = self._get_uploader_for_job(job)

            def save_session(uri: str | None) -> None:
                encrypted = encrypt_secret(uri) if uri else None
                now = int(time.time())
                self.db.execute("UPDATE jobs SET upload_session_enc=?,upload_started_at=?,updated_at=? WHERE id=?",
                                (encrypted, now if uri else None, now, job_id))

            settings_row = self._metadata_settings()
            self.db.execute("UPDATE jobs SET status='uploading',stage='uploading',progress=75,updated_at=? WHERE id=?",
                            (int(time.time()), job_id))
            self.db.event("Shorts upload started to YouTube", job_id=job_id)

            tags = list(settings_row.get("upload_tags", []))
            if "Shorts" not in tags:
                tags.append("Shorts")

            current_job = self.db.one("SELECT title,description FROM jobs WHERE id=?", (job_id,))
            title = current_job["title"] if current_job else job["title"]
            if "#Shorts" not in title and "#shorts" not in title.lower():
                title = f"{title} #Shorts"

            video_id = uploader.upload(short_output_path, {
                "title": title,
                "description": (current_job["description"] if current_job else "") + "\n\n#Shorts #Viral",
                "tags": tags,
                "category_id": settings_row.get("category_id", "22")
            }, settings_row.get("privacy_status", "private"),
               save_session=save_session, on_progress=lambda p: self._update_progress(job_id, 75 + int(p * 0.25), "uploading"))

            now = int(time.time())
            self.db.execute("UPDATE jobs SET status='completed',stage='completed',progress=100,youtube_id=?,error=NULL,upload_session_enc=NULL,upload_started_at=NULL,media_path=NULL,thumbnail_path=NULL,updated_at=? WHERE id=?",
                            (video_id, now, job_id))
            short_output_path.unlink(missing_ok=True)
            self.db.event(f"Shorts upload completed; destination video ID {video_id}", job_id=job_id)
        except IntegrationError as exc:
            self._fail(job, str(exc), retryable=exc.retryable)
        except Exception as exc:
            self._fail(job, f"Unexpected {type(exc).__name__}: {str(exc)[:350]}", retryable=True)

    def _get_uploader_for_job(self, job: dict[str, Any]) -> YouTubeUploader:
        user_id = job.get("user_id")
        token_row = None
        is_user_token = False

        if user_id:
            token_row = self.db.one("SELECT credentials_enc FROM user_oauth_tokens WHERE user_id=?", (user_id,))
            if token_row:
                is_user_token = True

        if not token_row:
            token_row = self.db.one("SELECT credentials_enc FROM oauth_tokens WHERE id=1")

        if not token_row:
            raise IntegrationError("Connect a destination YouTube account before processing jobs")

        token_json = decrypt_secret(token_row["credentials_enc"])

        def save_credentials(value: str) -> None:
            now = int(time.time())
            if is_user_token and user_id:
                self.db.execute("UPDATE user_oauth_tokens SET credentials_enc=?,updated_at=? WHERE user_id=?",
                                (encrypt_secret(value), now, user_id))
            else:
                self.db.execute("UPDATE oauth_tokens SET credentials_enc=?,updated_at=? WHERE id=1",
                                (encrypt_secret(value), now))

        return YouTubeUploader(token_json, on_credentials=save_credentials, timeout=settings.request_timeout)

    def _metadata_settings(self) -> dict[str, Any]:
        import json
        row = self.db.one("SELECT value FROM settings WHERE key='upload_tags'")
        try:
            tags = json.loads(row["value"]) if row else []
        except (ValueError, TypeError):
            tags = []
        return {"privacy_status": self.db.get_setting("privacy_status")[0] or "private",
                "category_id": self.db.get_setting("category_id")[0] or "22",
                "description_prefix": self.db.get_setting("description_prefix")[0] or "",
                "source_attribution": self.db.get_setting("source_attribution")[0] or "Authorized republication",
                "upload_tags": tags}

    @staticmethod
    def _format_description(description: str, rules: dict[str, Any]) -> str:
        prefix = rules.get("description_prefix", "")
        attribution = f"\n\nSource: {rules.get('source_attribution', 'Original channel') }"
        return (prefix + "\n\n" if prefix else "") + (description or "") + attribution

    def _update_progress(self, job_id: int, value: int, stage: str) -> None:
        self.db.execute("UPDATE jobs SET progress=?,stage=?,updated_at=? WHERE id=?",
                        (max(0, min(100, int(value))), stage, int(time.time()), job_id))

    def _fail(self, job: dict[str, Any], message: str, retryable: bool) -> None:
        message = redact_sensitive(message)
        attempts = int(job.get("attempts", 1))
        limit = int(self.db.get_setting("retry_limit")[0] or 5)
        now = int(time.time())
        current = self.db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))
        # Upload errors with an existing resumable session remain retryable only through that exact session.
        can_retry = retryable and attempts < limit
        saved_session = self.db.one("SELECT upload_session_enc,upload_started_at FROM jobs WHERE id=?", (job["id"],))
        uncertain_upload = bool(current and current["status"] == "uploading" and saved_session and saved_session["upload_started_at"])
        status = "needs_review" if uncertain_upload and (not can_retry or not saved_session["upload_session_enc"]) else ("retry" if can_retry else "failed")
        delay = min(3600, 2 ** min(attempts, 10)) + random.randint(0, 20) if status == "retry" else None
        next_attempt = now + delay if delay else None
        self.db.execute("UPDATE jobs SET status=?,stage=?,error=?,next_attempt_at=?,updated_at=? WHERE id=?",
                        (status, "retry" if status == "retry" else status, message[:1500], next_attempt, now, job["id"]))
        self.db.event(("Retry scheduled: " if status == "retry" else "Job failed: ") + message[:350],
                      "warning" if status == "retry" else "error", job_id=job["id"])
