# YT Uploader

A self-hosted, single-administrator dashboard and background worker for monitoring YouTube channel feeds and processing videos the operator owns or has documented authorization to redistribute. It uses FastAPI, SQLite, Google OAuth 2.0, the YouTube Data API resumable-upload protocol, and the documented Video Download API endpoints.

> A publicly viewable YouTube URL is not proof of permission to download or redistribute content. Only add sources and approve videos when you have the necessary rights. The app does not bypass DRM, access controls, or YouTube restrictions.

## Included

- Dark, responsive dashboard with overview, source management, queue, settings, connection status, and event history.
- Administrator login with Argon2 password hashing, session cookies, CSRF tokens, and in-process login throttling.
- SQLite persistence with WAL mode, foreign keys, unique source video IDs, transactional job claims, and automatic schema initialization.
- YouTube channel-feed polling, a first-poll baseline to avoid enqueuing existing feed items, and duplicate prevention.
- Manual video approval enabled by default, and private visibility selected by default.
- Provider adapter for the API contract at [`video-download-api.com/api/docs`](https://video-download-api.com/api/docs): create a job with `/ajax/download.php`, poll `/ajax/progress.php`, and stream the completed file to disk. Download files are size-limited and the returned URL is restricted to HTTPS on `p.savenow.to`.
- Google OAuth using only `youtube.upload`, encrypted credential storage, token refresh, and chunked resumable uploads with persisted session URLs.
- Import-only upload for a compatible Google `token.pickle`; a local Desktop OAuth helper at `tools/token.py` opens Google sign-in and creates the file.
- Recovery safeguards: completed media downloads are reused across retries; interrupted/uncertain uploads are held for manual reconciliation, with an option to resume the saved YouTube session rather than start a duplicate upload.
- A one-command Ubuntu installer and systemd service running under an unprivileged account.

## Requirements

- Ubuntu 22.04 or 24.04 for the installer.
- Python 3.11+ (the project is also tested with Python 3.12).
- A Google Cloud project with YouTube Data API v3 enabled and an OAuth Web application.
- A Video Download API account/key. Its service documents metered download jobs; check its current pricing and terms before enabling it.
- The operator's authorization to redistribute each source video.

## Install on Ubuntu

Clone the repository to the server, then run:

```bash
git clone <your-private-repository-url> yt-uploader
cd yt-uploader
sudo ./install.sh
```

The installer installs dependencies, creates the `yt-uploader` system user, stores persistent state under `/var/lib/yt-uploader`, stores secrets in `/etc/yt-uploader/yt-uploader.env`, and starts `yt-uploader.service`. It generates a unique admin password on first install and prints it once. Save it securely.

The dashboard binds to `127.0.0.1:8000`; it is not exposed to the public network. From your workstation, use an SSH tunnel:

```bash
ssh -L 8000:127.0.0.1:8000 <linux-user>@<server-ip>
```

Then visit <http://localhost:8000> in your browser. The service automatically starts on reboot. The dashboard binds to loopback by default; if you expose it through a reverse proxy, use HTTPS. Useful service commands:

```bash
sudo systemctl status yt-uploader
sudo journalctl -u yt-uploader -f
sudo systemctl restart yt-uploader
```

To update, fetch the new code in the clone and rerun `sudo ./install.sh`. The installer preserves the environment file and app state. Review release notes before database-changing upgrades.

## Local development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `ADMIN_PASSWORD`, `SESSION_SECRET`, and `ENCRYPTION_KEY` in `.env` to unique values. Generate them with:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Run the app with `python -m app.run`. It binds to `127.0.0.1:8000` by default. Run tests with `pytest -q`.

## Google OAuth setup

1. In Google Cloud Console, create or select a project and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen and create an OAuth client of type **Web application**.
3. Set the authorized redirect URI to the URI shown in the dashboard. OAuth always uses `http://localhost:<APP_PORT>/oauth/callback` through the SSH tunnel and is independent of `BASE_URL`; register this exact URI in Google Cloud.
4. `BASE_URL` may be configured for other app URLs, but does not change the OAuth callback. OAuthlib transport validation is disabled for this app as requested; external OAuth providers may still enforce their own redirect URI rules.
5. In the dashboard, open **Connections & settings**, save the OAuth client ID and secret, and choose **Connect Google account**. If that flow still fails, use `tools/token.py` locally: create a Google OAuth client of type **Desktop app**, copy `tools/token.py` to `tools/token.local.py`, paste that client's ID and secret into the local copy, install `google-auth-oauthlib` (`python -m pip install google-auth-oauthlib`), and run `python tools/token.local.py`. Complete Google sign-in in the browser, then upload the generated `token.pickle` in the dashboard. The uploader accepts only Google-auth credentials with `youtube.upload`; unsupported pickle objects are rejected.

Google may require OAuth app verification or may restrict uploads for unverified projects. Review current Google/YouTube policy, OAuth verification, API quota, and channel eligibility in the relevant consoles before unattended or public publishing. The service does not promise that every API upload will become publicly available.

## Provider setup and first run

1. Review the provider's [current API docs](https://video-download-api.com/api/docs), pricing, duration rules, and account balance. A successful API job may incur a charge.
2. Save your provider API key in **Connections & settings**. It is encrypted before it is persisted and never returned to the page.
3. Add a source using a channel ID such as `UC…` or a channel URL in `/channel/UC…` form. Handle and vanity URLs are not accepted by this build.
4. Confirm ownership/redistribution permission and add an optional authorization note. The first feed poll establishes a baseline; existing feed videos are not enqueued.
5. Connect the destination YouTube account. Leave visibility set to **Private** while testing.
6. Enable automation only after checking the provider settings and OAuth connection. New jobs wait for manual approval by default.
7. Approve only media you are authorized to publish. Confirm the resulting destination video ID and privacy in YouTube Studio.

The API client follows the provider's currently published contract and only accepts media URLs over HTTPS from `p.savenow.to`. If the provider changes that contract, update and test `app/integrations.py` before using the worker.

## Settings and operational behavior

- Default polling interval: 300 seconds; configurable from 60 seconds to one day.
- One worker/process, one media job at a time. No GPU or transcoding.
- Default maximum download size: 4 GiB; configurable in the dashboard. The destination upload uses resumable 8 MiB chunks.
- Default retry limit: five. Transient errors use exponential backoff with jitter; permanent errors are surfaced.
- Public and unlisted privacy settings are available, but Private is the initial default. Manual approval remains enabled unless the administrator explicitly changes it.
- A session interrupted after a resumable upload begins is held as `needs review`. Check the destination channel before clearing the session for retry. This avoids automatic duplicate uploads when the remote outcome is uncertain.
- `GET /health` provides a basic local process/DB health response. All dashboard and overview API data require administrator authentication.

## Backups and secrets

Back up the SQLite database and the environment file. The database contains encrypted provider/OAuth secrets, while `ENCRYPTION_KEY` is required to decrypt them. Keep that key separately protected from database backups. Losing or changing it without first re-encrypting stored data makes those secrets unavailable. Restrict access to backups; the OAuth refresh token is sensitive.

To back up the database consistently, stop the service or use SQLite's online backup API. Do not commit `.env`, the database, media downloads, logs, or OAuth token data.

## Test suite

Tests mock remote services and cover channel parsing, feed baselining, duplicate prevention, job claims, secret encryption, provider API validation and streaming, OAuth state handling, authentication/CSRF, restart recovery, and resumable-upload helpers. They do not perform a real Google upload or a billable provider request. Complete a manual end-to-end test with authorized test media and a test channel before production use.

## Project layout

```text
app/
  config.py         environment configuration
  db.py             SQLite schema and transactions
  security.py       password hashing, CSRF, and Fernet encryption
  integrations.py   feed, download provider, Google OAuth/upload client
  worker.py         polling, queue, retries, and recovery
  main.py           FastAPI routes and dashboard
  templates/        HTML views
  static/           responsive CSS and small JS helper
deploy/
  yt-uploader.service
install.sh
requirements.txt
tests/
```

## Official references

- [YouTube Data API: Upload a video](https://developers.google.com/youtube/v3/guides/uploading_a_video)
- [YouTube Data API: videos.insert](https://developers.google.com/youtube/v3/docs/videos/insert)
- [Google OAuth 2.0 for web server applications](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Video Download API documentation](https://video-download-api.com/api/docs)
