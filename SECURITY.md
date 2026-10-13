# Security policy

YT Uploader handles Google OAuth refresh tokens, a paid-provider API key, media files, and publishing controls. OAuth callback URLs using either HTTP or HTTPS are accepted on any hostname. Plain HTTP can expose authorization codes and account access in transit; use HTTPS/TLS for any public deployment. Keep the dashboard on loopback or place it behind a correctly configured reverse proxy and firewall.

## Reporting a vulnerability

Please do not publish access tokens, credentials, signed media URLs, or live database files in a public issue. Contact the repository owner through GitHub with a concise reproduction and impact description. Rotate affected credentials immediately if exposure is suspected.

## Deployment expectations

- Keep `/etc/yt-uploader/yt-uploader.env` readable only by root and the service group.
- Back up `ENCRYPTION_KEY` separately from the database; both are needed to recover encrypted credentials.
- Do not expose port 8000 publicly without a reverse proxy, firewall controls, and appropriate hardening. Use HTTPS; plain HTTP OAuth callbacks are enabled but vulnerable to interception.
- Use a unique administrator password and protect GitHub deploy credentials.
- Token pickle import is limited to allowlisted Google-auth classes, but only import credential files you trust. Exported token files contain refresh tokens; keep them private and delete them when no longer needed.
- Review source rights before approval and test with private uploads first.
