import base64
import os
import secrets
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "Test-Only-Password-8Dq!x9")
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("DATABASE_PATH", "/tmp/yt-uploader-tests-default.sqlite3")
os.environ.setdefault("DOWNLOADS_PATH", "/tmp/yt-uploader-tests-downloads")
os.environ.setdefault("LOG_PATH", "/tmp/yt-uploader-tests.log")
os.environ.setdefault("BASE_URL", "http://testserver")

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import Database
from app.worker import Worker


@pytest.fixture
def application(tmp_path, monkeypatch):
    db = Database(tmp_path / "app.sqlite3")
    monkeypatch.setattr(main, "DB", db)
    worker = Worker(db)
    monkeypatch.setattr(main, "WORKER", worker)
    with TestClient(main.app) as client:
        yield client, db, worker
