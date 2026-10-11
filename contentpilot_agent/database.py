"""Shared SQLAlchemy schema. All ownership checks stay at the API boundary.

Epoch timestamps avoid differences between SQLite and PostgreSQL timezone handling.
Queue updates always compare the current lease token (fencing stale workers).
"""

import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    event,
    select,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def uid():
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password: Mapped[str] = mapped_column(Text)
    admin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class LoginSession(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    key: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    facts: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    request: Mapped[dict] = mapped_column(JSON)
    source: Mapped[dict] = mapped_column(JSON)
    provider: Mapped[str] = mapped_column(String(32), default="mock")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    contents: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    available_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(64))
    elapsed_ms: Mapped[float] = mapped_column(Float, default=0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ReviewAudit(Base):
    __tablename__ = "review_audit"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text)
    contents: Mapped[list] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    updated_at: Mapped[float] = mapped_column(Float, index=True)


class ReferencePost(Base):
    __tablename__ = "reference_posts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(120), default="")
    keyword: Mapped[str] = mapped_column(String(100), default="")
    source_type: Mapped[str] = mapped_column(String(32))
    captured_at: Mapped[float] = mapped_column(Float, default=time.time)


class ReferenceSearch(Base):
    __tablename__ = "reference_searches"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    keyword: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), default="running")
    created_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)


class Publication(Base):
    __tablename__ = "publications"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    content_index: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    platform: Mapped[str] = mapped_column(String(32))
    content: Mapped[dict] = mapped_column(JSON)
    source: Mapped[dict] = mapped_column(JSON)
    experiment: Mapped[str] = mapped_column(String(120), default="")
    variant: Mapped[str] = mapped_column(String(32), default="A")
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class PerformanceSnapshot(Base):
    __tablename__ = "performance_snapshots"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), index=True)
    impressions: Mapped[int] = mapped_column(BigInteger)
    clicks: Mapped[int] = mapped_column(BigInteger)
    conversions: Mapped[int] = mapped_column(BigInteger)
    revenue: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float)
    evidence: Mapped[str] = mapped_column(String(500))
    captured_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class HumanEvaluation(Base):
    __tablename__ = "human_evaluations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    content_index: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)
    facts_score: Mapped[int] = mapped_column(Integer)
    platform_score: Mapped[int] = mapped_column(Integer)
    usefulness_score: Mapped[int] = mapped_column(Integer)
    factual_errors: Mapped[list] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class MediaAsset(Base):
    __tablename__ = "media_assets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)
    source: Mapped[dict] = mapped_column(JSON)
    prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    lease_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    remote_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    blob: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ServiceConfig(Base):
    __tablename__ = "service_configs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    encrypted: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    tested_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    test_status: Mapped[str] = mapped_column(String(32), default="untested")


class ServiceAudit(Base):
    __tablename__ = "service_audits"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    service_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ReviewerRole(Base):
    __tablename__ = "reviewer_roles"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class HotspotBatch(Base):
    __tablename__ = "hotspot_batches"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    topics: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ReviewAssignment(Base):
    __tablename__ = "review_assignments"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_at: Mapped[float] = mapped_column(Float, default=time.time)


class WorkflowCheckpoint(Base):
    __tablename__ = "workflow_checkpoints"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    state: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class DataSubscription(Base):
    __tablename__ = "data_subscriptions"
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=360)
    next_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)


class DataCollection(Base):
    __tablename__ = "data_collections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    lease_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    observation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Database:
    def __init__(self, url: str):
        self.url = url
        options = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            path = make_url(url).database
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            options["connect_args"] = {"check_same_thread": False, "timeout": 20}
        self.engine = create_engine(url, **options)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def sqlite_settings(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")

        self.factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        with self.factory.begin() as session:
            yield session

    def initialize(self):
        # v5 is additive; existing account/content columns stay intact.
        from sqlalchemy import inspect

        if inspect(self.engine).has_table("schema_version"):
            with self.session() as s:
                version = s.get(SchemaVersion, 1)
                if version and version.version not in {1, 2, 3, 4, 5}:
                    raise RuntimeError("数据库版本不兼容，请按部署文档迁移")
        Base.metadata.create_all(self.engine)
        with self.session() as s:
            version = s.get(SchemaVersion, 1)
            if version is None:
                s.add(SchemaVersion(id=1, version=5))
            elif version.version < 5:
                version.version = 5

    def save_checkpoint(self, job_id, token, state):
        with self.session() as s:
            if not s.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "running", Job.lease_token == token)
                .values(updated_at=time.time())
            ).rowcount:
                raise RuntimeError("Task lease lost")
            row = s.get(WorkflowCheckpoint, job_id)
            if row:
                row.state, row.updated_at = state, time.time()
            else:
                s.add(WorkflowCheckpoint(job_id=job_id, state=state))

    def worker_heartbeat(self, worker_id):
        now = time.time()
        with self.session() as s:
            s.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.updated_at < now - 86400))
            row = s.get(WorkerHeartbeat, worker_id)
            if row:
                row.updated_at = now
            else:
                s.add(WorkerHeartbeat(id=worker_id, updated_at=now))

    def worker_alive(self):
        with self.session() as s:
            return (
                s.scalar(
                    select(WorkerHeartbeat.id)
                    .where(WorkerHeartbeat.updated_at >= time.time() - 90)
                    .limit(1)
                )
                is not None
            )

    def remove_worker(self, worker_id):
        with self.session() as s:
            s.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.id == worker_id))

    def claim(self, lease_seconds=30):
        now = time.time()
        with self.session() as s:
            # Expired leases are abandoned attempts, never marked as success.
            expired = s.scalars(
                select(Job).where(Job.status == "running", Job.lease_until < now)
            ).all()
            for job in expired:
                status = "failed" if job.attempts >= job.max_attempts else "queued"
                changed = s.execute(
                    update(Job)
                    .where(
                        Job.id == job.id,
                        Job.status == "running",
                        Job.lease_token == job.lease_token,
                        Job.lease_until < now,
                    )
                    .values(
                        status=status,
                        stage="worker_lost",
                        lease_token=None,
                        lease_until=None,
                        error="执行进程中断或租约过期",
                        available_at=now,
                        updated_at=now,
                    )
                ).rowcount
                if changed:
                    s.add(
                        JobEvent(
                            job_id=job.id,
                            attempt=job.attempts,
                            stage="worker_lost",
                            payload={"message": "执行进程中断或租约过期"},
                        )
                    )
            ids = s.scalars(
                select(Job.id)
                .where(Job.status == "queued", Job.available_at <= now)
                .order_by(Job.created_at)
                .limit(10)
            ).all()
            for job_id in ids:
                token = uid()
                changed = s.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == "queued", Job.available_at <= now)
                    .values(
                        status="running",
                        stage="starting",
                        lease_token=token,
                        lease_until=now + lease_seconds,
                        attempts=Job.attempts + 1,
                        updated_at=now,
                        error=None,
                    )
                ).rowcount
                if changed:
                    s.expire_all()
                    job = s.get(Job, job_id)
                    return job.id, token
        return None

    def heartbeat(self, job_id, token, lease_seconds=30):
        with self.session() as s:
            return bool(
                s.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == "running", Job.lease_token == token)
                    .values(lease_until=time.time() + lease_seconds)
                ).rowcount
            )

    def record_event(self, job_id, token, stage, elapsed_ms, payload):
        with self.session() as s:
            if not s.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "running", Job.lease_token == token)
                .values(stage=stage, updated_at=time.time())
            ).rowcount:
                raise RuntimeError("Task lease lost")
            job = s.get(Job, job_id)
            s.add(
                JobEvent(
                    job_id=job_id,
                    attempt=job.attempts,
                    stage=stage,
                    elapsed_ms=elapsed_ms,
                    payload=payload,
                )
            )

    def complete(self, job_id, token, result):
        with self.session() as s:
            if "contents" not in result:
                return False
            return bool(
                s.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == "running", Job.lease_token == token)
                    .values(
                        status="pending_review",
                        stage="finished",
                        result=result,
                        contents=result["contents"],
                        revision=Job.revision + 1,
                        lease_token=None,
                        lease_until=None,
                        updated_at=time.time(),
                    )
                ).rowcount
            )

    def fail(self, job_id, token, message, retryable=False):
        with self.session() as s:
            # Atomic update fences stale processes and serializes failure with heartbeat.
            if not s.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "running", Job.lease_token == token)
                .values(updated_at=time.time())
            ).rowcount:
                return
            job = s.get(Job, job_id)
            job.status = "queued" if retryable and job.attempts < job.max_attempts else "failed"
            job.stage = "retry_wait" if job.status == "queued" else "failed"
            job.error = message
            job.lease_token = None
            job.lease_until = None
            job.available_at = time.time() + min(60, 2**job.attempts)
            s.add(
                JobEvent(
                    job_id=job_id,
                    attempt=job.attempts,
                    stage=job.stage,
                    payload={"message": message},
                )
            )

    def prune_auth(self):
        with self.session() as s:
            s.execute(delete(LoginSession).where(LoginSession.expires_at < time.time()))
            s.execute(delete(LoginAttempt).where(LoginAttempt.created_at < time.time() - 900))
