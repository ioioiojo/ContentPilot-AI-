"""Dedicated disposable databases; requires TEST_DATABASE_URL to a CI PostgreSQL superuser."""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from test_production import assert_additive_migration, assert_concurrent_admission

from contentpilot_agent.database import Database, Job

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL CI only")


@pytest.fixture
def postgres_db():
    url = make_url(os.environ["TEST_DATABASE_URL"])
    name = "cp_test_" + uuid.uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    db = Database(url.set(database=name).render_as_string(hide_password=False))
    try:
        yield db
    finally:
        db.engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def test_postgres_migration(postgres_db):
    assert_additive_migration(postgres_db)


def test_postgres_concurrent_admission_and_queue_fencing(postgres_db):
    db = postgres_db
    db.initialize()
    assert_concurrent_admission(db)
    job_id, token = db.claim()
    assert db.claim() is None
    assert not db.heartbeat(job_id, "stale-token")
    assert db.heartbeat(job_id, token)
    db.fail(job_id, "stale-token", "should not alter")
    with db.session() as s:
        assert s.get(Job, job_id).status == "running"
    db.worker_heartbeat("test-worker")
    assert db.worker_alive()
