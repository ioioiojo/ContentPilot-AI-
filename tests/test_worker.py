import time
from dataclasses import replace

from contentpilot_agent.config import Settings
from contentpilot_agent.database import Database, Job, User
from contentpilot_agent.models import ContentRequest
from contentpilot_agent.security import password_hash
from contentpilot_agent.worker import run_once


def hang(_url, _job_id, _token):
    time.sleep(30)


def setup_job(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'worker.db'}", provider="mock")
    db = Database(settings.database_url)
    db.initialize()
    with db.session() as s:
        user = User(username="test", password=password_hash("worker-test-password"))
        s.add(user)
        s.flush()
        job = Job(
            owner_id=user.id,
            request=ContentRequest(
                product_name="咖啡", product_description="便携", audience="通勤用户"
            ).model_dump(),
            source={"source": "说明书"},
            max_attempts=2,
        )
        s.add(job)
        s.flush()
        job_id = job.id
    return settings, db, job_id


def test_real_subprocess_persists_steps_and_result(tmp_path):
    settings, db, job_id = setup_job(tmp_path)
    assert run_once(db, settings)
    with db.session() as s:
        job = s.get(Job, job_id)
        assert job.status == "pending_review"
        assert job.result["versions"][0]["stage"] == "generate"
        assert job.result["metrics"]["elapsed_ms"] > 0
    db.engine.dispose()


def test_hard_timeout_and_retry_budget(tmp_path):
    settings, db, job_id = setup_job(tmp_path)
    settings = replace(settings, job_timeout=1)
    assert run_once(db, settings, target=hang)
    with db.session() as s:
        job = s.get(Job, job_id)
        assert job.status == "queued"
        assert "超时" in job.error
        job.available_at = time.time() - 1
    assert run_once(db, settings, target=hang)
    with db.session() as s:
        job = s.get(Job, job_id)
        assert job.status == "failed"
        assert job.attempts == 2
        assert job.lease_token is None
    db.engine.dispose()
