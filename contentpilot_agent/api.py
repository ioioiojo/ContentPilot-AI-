import hmac
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .database import (
    Database,
    Job,
    JobEvent,
    LoginAttempt,
    LoginSession,
    Product,
    ReviewAssignment,
    ReviewAudit,
    User,
    WorkflowCheckpoint,
)
from .growth import performance_context
from .limits import enforce_limits, lock_owner
from .models import ContentItem, ContentRequest, Platform
from .quality import check_content_rules, check_reference_rules
from .references import selected_snapshots
from .security import DUMMY_PASSWORD_HASH, password_hash, password_matches, token_hash
from .skills import SKILLS, skill_snapshot

COOKIE = "contentpilot_session"
ROOT = Path(__file__).resolve().parent


class RequestGuards:
    """Bound unsafe request bodies and add same-origin/security headers."""

    def __init__(self, app, body_limit=256_000):
        self.app = app
        self.body_limit = body_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        if scope.get("method") in {"POST", "PUT", "PATCH", "DELETE"}:
            host = headers.get("host", "")
            origin = headers.get("origin")
            if headers.get("sec-fetch-site") == "cross-site" or (
                origin and urlsplit(origin).netloc != host
            ):
                await self._simple_response(send, 403, b"Cross-origin request rejected")
                return
            try:
                declared = int(headers.get("content-length", "0"))
            except ValueError:
                await self._simple_response(send, 400, b"Invalid content length")
                return
            if declared > self.body_limit:
                await self._simple_response(send, 413, b"Request too large")
                return
            chunks = []
            size = 0
            more = True
            while more:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.body_limit:
                    await self._simple_response(send, 413, b"Request too large")
                    return
                chunks.append(chunk)
                more = message.get("more_body", False)
            body = b"".join(chunks)
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}

            receive = replay

        async def secure_send(message):
            if message["type"] == "http.response.start":
                current = list(message.get("headers", []))
                values = {key.lower() for key, _ in current}
                for key, value in (
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"same-origin"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
                    ),
                ):
                    if key not in values:
                        current.append((key, value))
                if not scope.get("path", "").startswith("/static/"):
                    current.append((b"cache-control", b"no-store"))
                message["headers"] = current
            await send(message)

        await self.app(scope, receive, secure_send)

    @staticmethod
    async def _simple_response(send, status, body):
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Input):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=128)


class NewUser(Login):
    password: str = Field(min_length=12, max_length=128)


class PasswordChange(Input):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class ProductInput(Input):
    name: str = Field(min_length=1, max_length=120)
    facts: str = Field(min_length=1, max_length=12000)
    source: str = Field(min_length=1, max_length=500)


class NewJob(Input):
    product_id: str
    audience: str = Field(min_length=1, max_length=300)
    goal: str = Field(default="提升点击和转化", min_length=1, max_length=300)
    tone: str = Field(default="真实、简洁、有说服力", min_length=1, max_length=200)
    platforms: list[Platform] = Field(
        default_factory=lambda: ["xiaohongshu", "douyin"], min_length=1, max_length=3
    )
    content_count: int = Field(default=3, ge=1, le=10)
    skill_id: str = Field(default="scene", max_length=32)
    use_feedback: bool = True
    reference_ids: list[str] = Field(default_factory=list, max_length=5)
    rewrite_guidance: str = Field(default="", max_length=1000)
    use_knowledge: bool = True
    hotspot_batch_id: str | None = None


class ReviewInput(Input):
    revision: int = Field(ge=0)
    action: str = Field(pattern="^(save|approve|reject)$")
    note: str = Field(min_length=1, max_length=2000)
    contents: list[ContentItem] = Field(min_length=1, max_length=10)


class ActiveInput(Input):
    active: bool


def public_user(user):
    return {"id": user.id, "username": user.username, "admin": user.admin, "active": user.active}


def product_data(product):
    return {k: getattr(product, k) for k in ("id", "name", "facts", "source", "created_at")}


def job_data(job, detail=True):
    keys = (
        "id",
        "provider",
        "status",
        "stage",
        "attempts",
        "max_attempts",
        "error",
        "revision",
        "created_at",
        "updated_at",
    )
    result = {k: getattr(job, k) for k in keys}
    result["product_name"] = job.request["product_name"]
    if detail:
        result.update(
            request=job.request, source=job.source, result=job.result, contents=job.contents
        )
    return result


def create_app(settings: Settings | None = None, database: Database | None = None):
    settings = settings or Settings.from_env()
    db = database or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app):
        db.initialize()
        yield
        db.engine.dispose()

    app = FastAPI(
        title="ContentPilot",
        version="0.7.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.db = db
    app.state.settings = settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.add_middleware(RequestGuards)
    from .services import ServiceContext

    app.add_middleware(ServiceContext, db=db)

    @app.exception_handler(RequestValidationError)
    async def safe_validation(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()
                ]
            },
        )

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Environment(
        loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape()
    )

    def current_user(request: Request):
        token = request.cookies.get(COOKIE, "")
        with db.session() as s:
            session = s.get(LoginSession, token_hash(token)) if token else None
            user = (
                s.get(User, session.user_id)
                if session and session.expires_at > time.time()
                else None
            )
            if not user or not user.active:
                raise HTTPException(401, "请先登录")
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not hmac.compare_digest(
                request.headers.get("x-csrf-token", ""), session.csrf
            ):
                raise HTTPException(403, "会话校验失败，请刷新后重试")
            request.state.csrf = session.csrf
            return user

    def admin_user(user=Depends(current_user)):
        if not user.admin:
            raise HTTPException(403, "需要管理员权限")
        return user

    def owned(s, model, object_id, user):
        obj = s.scalar(select(model).where(model.id == object_id, model.owner_id == user.id))
        if obj is None:
            raise HTTPException(404, "记录不存在")
        return obj

    @app.get("/", response_class=HTMLResponse)
    def index():
        return templates.get_template("index.html").render()

    @app.get("/health")
    def health():
        try:
            with db.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except SQLAlchemyError:
            raise HTTPException(503, "database unavailable")
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        health()
        try:
            alive = db.worker_alive()
        except SQLAlchemyError:
            raise HTTPException(503, "worker status unavailable")
        if not alive:
            raise HTTPException(503, "worker unavailable")
        return {"status": "ready"}

    @app.get("/api/admin/runtime")
    def runtime(user=Depends(admin_user)):
        # Aggregate configuration only: no keys, prompts, or member content.
        with db.session() as s:
            queued = s.scalar(select(func.count()).select_from(Job).where(Job.status == "queued"))
            running = s.scalar(select(func.count()).select_from(Job).where(Job.status == "running"))
        return {
            "version": "0.7.0",
            "environment": settings.app_env,
            "worker_alive": db.worker_alive(),
            "queued": queued,
            "running": running,
            "submissions_enabled": settings.submissions_enabled,
            "limits": {
                "active_text": settings.max_active_jobs,
                "text_24h": settings.daily_jobs,
                "active_media": settings.max_active_media,
                "media_24h": settings.daily_media,
                "max_attempts": settings.max_attempts,
            },
        }

    @app.post("/api/login")
    def login(data: Login, request: Request, response: Response):
        name = data.username.lower()
        key = token_hash("user:" + name)
        peer = token_hash("peer:" + (request.client.host if request.client else "unknown"))
        db.prune_auth()
        with db.session() as s:
            count = s.scalar(
                select(func.count()).select_from(LoginAttempt).where(LoginAttempt.key == key)
            )
            peer_count = s.scalar(
                select(func.count()).select_from(LoginAttempt).where(LoginAttempt.key == peer)
            )
            if count >= 5 or peer_count >= 100:
                raise HTTPException(429, "尝试次数过多，请 15 分钟后再试")
            user = s.scalar(select(User).where(User.username == name))
            valid = password_matches(data.password, user.password if user else DUMMY_PASSWORD_HASH)
            if not valid or not user or not user.active:
                s.add_all([LoginAttempt(key=key), LoginAttempt(key=peer)])
                failure = True
            else:
                failure = False
                s.execute(delete(LoginAttempt).where(LoginAttempt.key == key))
                old_token = request.cookies.get(COOKIE)
                if old_token:
                    s.execute(delete(LoginSession).where(LoginSession.id == token_hash(old_token)))
                token = secrets.token_urlsafe(32)
                csrf = secrets.token_hex(32)
                s.add(
                    LoginSession(
                        id=token_hash(token),
                        user_id=user.id,
                        csrf=csrf,
                        expires_at=time.time() + settings.session_hours * 3600,
                    )
                )
                result = {"user": public_user(user), "csrf": csrf, "provider": settings.provider}
        if failure:
            raise HTTPException(401, "用户名或密码错误")
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=settings.session_hours * 3600,
        )
        return result

    @app.get("/api/me")
    def me(request: Request, user=Depends(current_user)):
        return {
            "user": public_user(user),
            "csrf": request.state.csrf,
            "provider": settings.provider,
        }

    @app.post("/api/logout")
    def logout(request: Request, response: Response, user=Depends(current_user)):
        with db.session() as s:
            s.execute(
                delete(LoginSession).where(LoginSession.id == token_hash(request.cookies[COOKIE]))
            )
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.post("/api/password")
    def change_password(data: PasswordChange, response: Response, user=Depends(current_user)):
        if not password_matches(data.current_password, user.password):
            raise HTTPException(400, "原密码不正确")
        with db.session() as s:
            s.execute(
                update(User)
                .where(User.id == user.id)
                .values(password=password_hash(data.new_password))
            )
            s.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/users")
    def users(user=Depends(admin_user)):
        with db.session() as s:
            return [public_user(u) for u in s.scalars(select(User).order_by(User.created_at))]

    @app.post("/api/users", status_code=201)
    def add_user(data: NewUser, user=Depends(admin_user)):
        try:
            with db.session() as s:
                member = User(username=data.username.lower(), password=password_hash(data.password))
                s.add(member)
                s.flush()
                return public_user(member)
        except IntegrityError:
            raise HTTPException(409, "用户名已存在")

    @app.patch("/api/users/{user_id}")
    def set_active(user_id: str, data: ActiveInput, user=Depends(admin_user)):
        with db.session() as s:
            member = s.get(User, user_id)
            if member is None:
                raise HTTPException(404, "用户不存在")
            if member.admin:
                raise HTTPException(409, "管理员账号不能在此停用")
            member.active = data.active
            if not data.active:
                s.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
            return public_user(member)

    @app.get("/api/products")
    def products(user=Depends(current_user)):
        with db.session() as s:
            return [
                product_data(p)
                for p in s.scalars(
                    select(Product)
                    .where(Product.owner_id == user.id)
                    .order_by(Product.created_at.desc())
                    .limit(500)
                )
            ]

    @app.post("/api/products", status_code=201)
    def add_product(data: ProductInput, user=Depends(current_user)):
        with db.session() as s:
            product = Product(owner_id=user.id, **data.model_dump())
            s.add(product)
            s.flush()
            return product_data(product)

    @app.put("/api/products/{product_id}")
    def edit_product(product_id: str, data: ProductInput, user=Depends(current_user)):
        with db.session() as s:
            product = owned(s, Product, product_id, user)
            for key, value in data.model_dump().items():
                setattr(product, key, value)
            return product_data(product)

    @app.delete("/api/products/{product_id}")
    def delete_product(product_id: str, user=Depends(current_user)):
        with db.session() as s:
            s.delete(owned(s, Product, product_id, user))
        return {"ok": True}

    @app.post("/api/jobs", status_code=202)
    def create_job(data: NewJob, user=Depends(current_user)):
        with db.session() as s:
            lock_owner(s, user.id)
            product = owned(s, Product, data.product_id, user)
            enforce_limits(
                s,
                Job,
                user.id,
                settings.max_active_jobs,
                settings.daily_jobs,
                settings.submissions_enabled,
            )
            if len(data.platforms) != len(set(data.platforms)):
                raise HTTPException(422, "平台不能重复")
            references = selected_snapshots(s, user.id, data.reference_ids)
            from .database import HotspotBatch
            from .knowledge import retrieve

            knowledge = (
                retrieve(s, user.id, product.name + " " + data.audience)
                if data.use_knowledge
                else []
            )
            live_topics = []
            if data.hotspot_batch_id:
                batch = owned(s, HotspotBatch, data.hotspot_batch_id, user)
                if batch.created_at < time.time() - 86400:
                    raise HTTPException(422, "热点已超过 24 小时，请重新获取")
                live_topics = [t for t in batch.topics if t["platform"] in data.platforms]
            brief = ContentRequest(
                product_name=product.name,
                product_description=product.facts,
                **data.model_dump(
                    exclude={
                        "product_id",
                        "skill_id",
                        "use_feedback",
                        "reference_ids",
                        "use_knowledge",
                        "hotspot_batch_id",
                    }
                ),
                references=references,
                knowledge_context=knowledge,
                live_topics=live_topics,
                skill=skill_snapshot(data.skill_id) if data.skill_id in SKILLS else {},
                performance_context=performance_context(s, user.id, product.id)
                if data.use_feedback
                else [],
                tools_mode=settings.tools_mode,
            )
            if data.skill_id not in SKILLS:
                raise HTTPException(422, "未知内容技能")
            job = Job(
                owner_id=user.id,
                request=brief.model_dump(),
                source={
                    **product_data(product),
                    **({"references": references} if references else {}),
                },
                provider=settings.provider,
                max_attempts=settings.max_attempts,
            )
            s.add(job)
            s.flush()
            return job_data(job)

    @app.get("/api/jobs")
    def jobs(offset: int = 0, user=Depends(current_user)):
        if not 0 <= offset <= 100000:
            raise HTTPException(422, "分页参数无效")
        with db.session() as s:
            return [
                job_data(j, False)
                for j in s.scalars(
                    select(Job)
                    .where(Job.owner_id == user.id)
                    .order_by(Job.created_at.desc())
                    .offset(offset)
                    .limit(50)
                )
            ]

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str, user=Depends(current_user)):
        with db.session() as s:
            from .editorial import reviewer_access

            job = s.get(Job, job_id)
            if not job or not reviewer_access(s, job, user):
                raise HTTPException(404, "任务不存在")
            data = job_data(job)
            assignment = s.get(ReviewAssignment, job_id)
            data["is_owner"] = job.owner_id == user.id
            data["assigned_reviewer"] = assignment.reviewer_id if assignment else None
            data["checkpoint"] = (
                s.get(WorkflowCheckpoint, job_id).state.get("next_node")
                if s.get(WorkflowCheckpoint, job_id)
                else None
            )
            data["events"] = [
                {
                    "stage": e.stage,
                    "attempt": e.attempt,
                    "elapsed_ms": e.elapsed_ms,
                    "payload": e.payload,
                    "created_at": e.created_at,
                }
                for e in s.scalars(
                    select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.created_at)
                )
            ]
            data["audit"] = [
                {
                    "action": a.action,
                    "note": a.note,
                    "contents": a.contents,
                    "revision": a.revision,
                    "reviewer": s.get(User, a.user_id).username,
                    "created_at": a.created_at,
                }
                for a in s.scalars(
                    select(ReviewAudit)
                    .where(ReviewAudit.job_id == job_id)
                    .order_by(ReviewAudit.created_at)
                )
            ]
            return data

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry_job(job_id: str, user=Depends(current_user)):
        with db.session() as s:
            lock_owner(s, user.id)
            job = owned(s, Job, job_id, user)
            if job.status != "failed" or job.attempts >= job.max_attempts:
                raise HTTPException(409, "仅可重试未达上限的失败任务；请新建任务以重新开始")
            enforce_limits(
                s,
                Job,
                user.id,
                settings.max_active_jobs,
                settings.daily_jobs,
                settings.submissions_enabled,
                new=False,
            )
            changed = s.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "failed", Job.revision == job.revision)
                .values(
                    status="queued",
                    stage="queued",
                    error=None,
                    available_at=time.time(),
                    updated_at=time.time(),
                    revision=Job.revision + 1,
                )
            ).rowcount
            if not changed:
                raise HTTPException(409, "任务已变更")
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/resume", status_code=202)
    def resume_job(job_id: str, user=Depends(current_user)):
        with db.session() as s:
            lock_owner(s, user.id)
            job = owned(s, Job, job_id, user)
            saved = s.get(WorkflowCheckpoint, job_id)
            if job.status != "failed" or not saved or job.attempts >= 3:
                raise HTTPException(409, "仅有断点且总执行次数少于 3 次的失败任务可继续")
            enforce_limits(
                s,
                Job,
                user.id,
                settings.max_active_jobs,
                settings.daily_jobs,
                settings.submissions_enabled,
                new=False,
            )
            if not s.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "failed", Job.revision == job.revision)
                .values(
                    status="queued",
                    stage="resume_queued",
                    max_attempts=max(job.max_attempts, job.attempts + 1),
                    error=None,
                    available_at=time.time(),
                    updated_at=time.time(),
                    revision=Job.revision + 1,
                )
            ).rowcount:
                raise HTTPException(409, "任务已变化")
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/review")
    def review_job(job_id: str, data: ReviewInput, user=Depends(current_user)):
        with db.session() as s:
            from .editorial import reviewer_access

            job = s.get(Job, job_id)
            if not job or not reviewer_access(s, job, user):
                raise HTTPException(404, "任务不存在")
            assignment = s.get(ReviewAssignment, job_id)
            if (
                assignment
                and assignment.reviewer_id != user.id
                and data.action in {"approve", "reject"}
            ):
                raise HTTPException(403, "已分配独立审稿人，请由审稿人完成通过或驳回")
            if job.status not in {"pending_review", "rejected"}:
                raise HTTPException(409, "当前任务不允许修改或审核")
            if len(data.contents) != job.request["content_count"] or any(
                c.platform not in job.request["platforms"] for c in data.contents
            ):
                raise HTTPException(422, "内容数量或平台与任务不一致")
            issues = check_content_rules(data.contents) + check_reference_rules(
                data.contents, ContentRequest.model_validate(job.request).references
            )
            if data.action == "approve" and issues:
                raise HTTPException(422, "请先修复规则问题：" + "；".join(issues))
            next_status = {"save": "pending_review", "approve": "approved", "reject": "rejected"}[
                data.action
            ]
            contents = [c.model_dump() for c in data.contents]
            changed = s.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.revision == data.revision,
                    Job.status.in_(["pending_review", "rejected"]),
                )
                .values(
                    contents=contents,
                    status=next_status,
                    revision=Job.revision + 1,
                    updated_at=time.time(),
                )
            ).rowcount
            if not changed:
                raise HTTPException(409, "内容已被其他页面更新，请重新打开任务")
            s.add(
                ReviewAudit(
                    job_id=job_id,
                    user_id=user.id,
                    action=data.action,
                    note=data.note,
                    contents=contents,
                    revision=data.revision + 1,
                )
            )
        return {"ok": True, "status": next_status, "revision": data.revision + 1}

    @app.get("/api/jobs/{job_id}/export")
    def export(job_id: str, user=Depends(current_user)):
        import json

        with db.session() as s:
            job = owned(s, Job, job_id, user)
            if job.status != "approved":
                raise HTTPException(409, "人工审核通过后才可导出")
            payload = {
                "id": job.id,
                "mode": job.provider,
                "status": job.status,
                "revision": job.revision,
                "source": job.source,
                "contents": job.contents,
                "references": job.request.get("references", []),
                "rewrite_guidance": job.request.get("rewrite_guidance", ""),
                "knowledge_context": job.request.get("knowledge_context", []),
                "live_topics": job.request.get("live_topics", []),
            }
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="contentpilot-{job_id}.json"'},
        )

    @app.get("/api/stats")
    def stats(user=Depends(current_user)):
        with db.session() as s:
            counts = dict(
                s.execute(
                    select(Job.status, func.count())
                    .where(Job.owner_id == user.id)
                    .group_by(Job.status)
                ).all()
            )
            recent = list(
                s.scalars(
                    select(Job)
                    .where(Job.owner_id == user.id, Job.result.is_not(None))
                    .order_by(Job.updated_at.desc())
                    .limit(100)
                )
            )
            elapsed = [j.result.get("metrics", {}).get("elapsed_ms", 0) for j in recent]
            return {
                "counts": counts,
                "sample_size": len(recent),
                "average_elapsed_ms": round(sum(elapsed) / len(elapsed), 2) if elapsed else None,
                "average_revisions": round(
                    sum(j.result["iterations"] for j in recent) / len(recent), 2
                )
                if recent
                else None,
                "token_usage": sum(
                    (j.result.get("metrics", {}).get("token_usage") or {}).get("total_tokens", 0)
                    for j in recent
                )
                if any(j.provider != "mock" for j in recent)
                else None,
            }

    from .operations import register_operations

    register_operations(app, db, current_user, owned, settings)
    from .references import register_references

    register_references(app, db, current_user, owned)
    from .collection import register_collection
    from .editorial import register_editorial
    from .knowledge import register_knowledge
    from .services import register_services

    register_services(app, db, admin_user)
    register_knowledge(app, db, current_user, owned)
    register_editorial(app, db, current_user, admin_user, owned, job_data)
    register_collection(app, db, current_user, owned, settings)
    return app


app = create_app()
