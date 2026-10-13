from __future__ import annotations

import uvicorn

from .config import settings

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, log_level="info", proxy_headers=False)
