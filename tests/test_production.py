import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_api import MEMBER_PASSWORD, add_product, login, make_client

from contentpilot_agent.api import create_app
from contentpilot_agent.config import Settings
from contentpilot_agent.database import (
    Base,
    Database,
    Job,
    SchemaVersion,
    User,
    WorkerHeartbeat,
)
from contentpilot_agent.limits import enforce_limits, lock_owner


def test_production_refuses_unsafe_settings():
    valid = Settings(
        app_env="production",
        database_url="postgresql+psycopg://user:password@db/app",
        cookie_secure=True,
        provider="compatible",
        allowed_hosts=("localhost", "127.0.0.1", "content.example.org"),
    )
    for changes in (
        {"database_url": "sqlite:///data.db"},
        {"cookie_secure": False},
        {"provider": "mock"},
        {"allowed_hosts": ("*",)},
        {"allowed_hosts": ("localhost", "127.0.0.1", "contentpilot.example.com")},
        {"allowed_hosts": ("content.example.org",)},
        {"daily_jobs": 0},
    ):
        with pytest.raises(ValueError):
            replace(valid, **changes)
    assert replace(valid, app_env="staging", provider="mock")


def test_production_env_credentials_and_https(monkeypatch):
    monkeypatch.setattr("contentpilot_agent.config.load_dotenv", lambda: None)
    values = {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+psycopg://user:secret@db/app",
        "COOKIE_SECURE": "true",
        "CONTENTPILOT_PROVIDER": "compatible",
        "ALLOWED_HOSTS": "localhost,127.0.0.1,content.example.org",
        "OPENAI_MODEL": "test",
        "OPENAI_API_KEY": "test-not-a-key",
        "OPENAI_BASE_URL": "https://model.example.org/v1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert Settings.from_env().max_attempts == 1
    for endpoint in ("http://model.example.org", "https://user:pass@model.example.org"):
        monkeypatch.setenv("OPENAI_BASE_URL", endpoint)
        with pytest.raises(ValueError):
            Settings.from_env()
    monkeypatch.setenv("OPENAI_BASE_URL", values["OPENAI_BASE_URL"])
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_readiness_and_admin_diagnostics(tmp_path):
    client, db = make_client(tmp_path)
    with client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        db.worker_heartbeat("worker-test")
        assert client.get("/ready").json() == {"status": "ready"}
        with db.session() as s:
            s.get(WorkerHeartbeat, "worker-test").updated_at = time.time() - 91
        assert client.get("/ready").status_code == 503
        db.worker_heartbeat("worker-test")
        db.remove_worker("worker-test")
        assert client.get("/ready").status_code == 503
        assert client.get("/api/admin/runtime").status_code == 401
        login(client, "member", MEMBER_PASSWORD)
        assert client.get("/api/admin/runtime").status_code == 403
        login(client)
        response = client.get("/api/admin/runtime")
        assert response.status_code == 200
        assert "password" not in response.text and "API_KEY" not in response.text


def test_daily_quota_retry_active_quota_and_pause(tmp_path):
    original, db = make_client(tmp_path)
    settings = replace(original.app.state.settings, max_active_jobs=1, daily_jobs=1)
    with TestClient(create_app(settings, db)) as client:
        csrf = login(client)
        headers = {"X-CSRF-Token": csrf}
        product = add_product(client, csrf)
        data = {"product_id": product["id"], "audience": "用户"}
        job = client.post("/api/jobs", headers=headers, json=data).json()
        assert client.post("/api/jobs", headers=headers, json=data).status_code == 429
        with db.session() as s:
            s.get(Job, job["id"]).status = "failed"
        # Daily limits include failed tasks; retries retain the original attempt budget.
        assert client.post("/api/jobs", headers=headers, json=data).status_code == 429
        assert client.post(f"/api/jobs/{job['id']}/retry", headers=headers).status_code == 202
        with db.session() as s:
            row = s.get(Job, job["id"])
            row.status = "failed"
            row.created_at = time.time() - 86401
        assert client.post("/api/jobs", headers=headers, json=data).status_code == 202
        assert client.post(f"/api/jobs/{job['id']}/retry", headers=headers).status_code == 429
    with TestClient(create_app(replace(settings, submissions_enabled=False), db)) as client:
        csrf = login(client)
        assert (
            client.post("/api/jobs", headers={"X-CSRF-Token": csrf}, json=data).status_code == 503
        )


def assert_concurrent_admission(db):
    with db.session() as s:
        user = User(username="race", password="not-used")
        s.add(user)
        s.flush()
        owner_id = user.id

    def submit(_):
        try:
            with db.session() as s:
                lock_owner(s, owner_id)
                enforce_limits(s, Job, owner_id, 1, 10, True)
                s.add(Job(owner_id=owner_id, request={}, source={}))
            return 202
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(submit, range(8)))
    assert statuses.count(202) == 1
    assert statuses.count(429) == 7
    with db.session() as s:
        assert len(s.scalars(select(Job).where(Job.owner_id == owner_id)).all()) == 1


def test_concurrent_sqlite_admission(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'race.db'}")
    db.initialize()
    try:
        assert_concurrent_admission(db)
    finally:
        db.engine.dispose()


def assert_additive_migration(db):
    Base.metadata.create_all(
        db.engine,
        tables=[
            table
            for table in Base.metadata.sorted_tables
            if table.name not in {"worker_heartbeats", "reference_posts", "reference_searches"}
        ],
    )
    with db.session() as s:
        s.add(SchemaVersion(id=1, version=2))
        user = User(username="preserve", password="password-hash", admin=True)
        s.add(user)
        s.flush()
        s.add(Job(owner_id=user.id, request={"snapshot": "old"}, source={}, status="approved"))
    db.initialize()
    with db.session() as s:
        assert s.get(SchemaVersion, 1).version == 5
        assert s.scalar(select(User).where(User.username == "preserve")).password == "password-hash"
        assert s.scalar(select(Job)).request == {"snapshot": "old"}
        s.get(SchemaVersion, 1).version = 999
    with pytest.raises(RuntimeError):
        db.initialize()


def test_v2_to_v4_preserves_records_and_refuses_unknown_schema(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        assert_additive_migration(db)
    finally:
        db.engine.dispose()
