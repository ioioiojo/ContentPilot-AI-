"""DB-backed queue worker with lease fencing and subprocess hard timeouts."""

import argparse
import logging
import multiprocessing
import signal
import tempfile
import time
from pathlib import Path

from sqlalchemy import text

from .collection import run_collection_once, schedule_due
from .config import Settings
from .database import Database, Job, WorkflowCheckpoint, uid
from .media import run_media_once
from .models import ContentRequest
from .providers import provider_from_env
from .services import SERVICE_CACHE, SERVICE_DB
from .tools import ContentTools
from .workflow import run_workflow

log = logging.getLogger("contentpilot.worker")
HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "contentpilot-worker-heartbeat"


def execute_job(database_url: str, job_id: str, token: str):
    db = Database(database_url)
    service_token = SERVICE_DB.set(db)
    cache_token = SERVICE_CACHE.set({})
    try:
        with db.session() as s:
            job = s.get(Job, job_id)
            if not job or job.status != "running" or job.lease_token != token:
                return
            request = ContentRequest.model_validate(job.request)
            mode = job.provider
            saved = s.get(WorkflowCheckpoint, job_id)
            saved_state = saved.state if saved else None
        provider = provider_from_env(mode)

        def observe(stage, elapsed, payload):
            db.record_event(job_id, token, stage, elapsed, payload)

        result = run_workflow(
            request,
            provider=provider,
            observer=observe,
            tools=ContentTools(mode=request.tools_mode),
            checkpoint=lambda state: db.save_checkpoint(job_id, token, state),
            initial_checkpoint=saved_state,
        )
        result.run_id = job_id
        from sqlalchemy import select

        from .database import JobEvent

        with db.session() as s:
            result.versions = [
                {"stage": e.stage, "attempt": e.attempt, "elapsed_ms": e.elapsed_ms, **e.payload}
                for e in s.scalars(
                    select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.created_at)
                )
                if "contents" in e.payload
            ]
        db.complete(job_id, token, result.model_dump())
    except Exception as exc:  # noqa: BLE001 - persist a sanitized failure for every task exception
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        transient = isinstance(exc, (APIConnectionError, APITimeoutError)) or (
            isinstance(exc, APIStatusError) and (exc.status_code == 429 or exc.status_code >= 500)
        )
        if transient:
            message = "模型连接超时、限流或服务暂不可用"
        elif isinstance(exc, ValueError):
            message = "模型配置或输出校验失败，请检查模型配置、内容数量及格式"
        else:
            message = f"执行失败（{type(exc).__name__}），请查看对应步骤"
        # Never expose provider exception text (may include keys or raw prompts).
        db.fail(job_id, token, message, retryable=transient)
        log.error("job=%s error_type=%s retryable=%s", job_id, type(exc).__name__, transient)
    finally:
        SERVICE_DB.reset(service_token)
        SERVICE_CACHE.reset(cache_token)
        db.engine.dispose()


def run_once(db: Database, settings: Settings, target=execute_job, pulse=None):
    claim = db.claim(settings.lease_seconds)
    if not claim:
        return False
    job_id, token = claim
    process = multiprocessing.get_context("spawn").Process(
        target=target, args=(settings.database_url, job_id, token)
    )
    try:
        try:
            process.start()
        except (OSError, RuntimeError):
            db.fail(job_id, token, "无法启动工作进程", retryable=True)
            return True
        started = time.monotonic()
        while process.is_alive():
            process.join(timeout=min(1, settings.lease_seconds / 3))
            if not process.is_alive():
                break
            if time.monotonic() - started >= settings.job_timeout:
                process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join()
                db.fail(job_id, token, "任务执行超时，已终止本次执行", retryable=True)
                return True
            if not db.heartbeat(job_id, token, settings.lease_seconds):
                process.terminate()
                process.join(5)
                return True
            if pulse:
                pulse()
        db.fail(job_id, token, "工作进程意外退出", retryable=True)
        return True
    finally:
        if process.pid and process.is_alive():
            process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="处理一个任务后退出")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    db = Database(settings.database_url)
    service_token = SERVICE_DB.set(db)
    cache_token = SERVICE_CACHE.set({})
    if args.healthcheck:
        try:
            with db.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            age = time.time() - HEARTBEAT_FILE.stat().st_mtime
            if age > 90:
                raise SystemExit(1)
            return
        finally:
            SERVICE_DB.reset(service_token)
            SERVICE_CACHE.reset(cache_token)
            db.engine.dispose()
    worker_id = uid()
    last_pulse = 0.0

    def pulse():
        nonlocal last_pulse
        if time.monotonic() - last_pulse >= 10:
            db.worker_heartbeat(worker_id)
            HEARTBEAT_FILE.touch()
            last_pulse = time.monotonic()

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            SERVICE_CACHE.set({})
            pulse()
            schedule_due(db, settings.submissions_enabled)
            worked = run_once(db, settings, pulse=pulse)
            if not worked:
                worked = run_media_once(db, settings, pulse=pulse)
            if not worked:
                worked = run_collection_once(db, settings, pulse=pulse)
            if args.once:
                break
            if not worked:
                time.sleep(1)
    except KeyboardInterrupt:
        log.info("Worker stopped; unfinished leases will be recovered")
    finally:
        try:
            db.remove_worker(worker_id)
            HEARTBEAT_FILE.unlink(missing_ok=True)
        finally:
            SERVICE_DB.reset(service_token)
            SERVICE_CACHE.reset(cache_token)
            db.engine.dispose()


if __name__ == "__main__":
    main()
