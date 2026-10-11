"""Scheduled, lease-fenced data collection; missing metrics remain unknown."""

import multiprocessing
import re
import time
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update

from .database import (
    Database,
    DataCollection,
    DataSubscription,
    PerformanceSnapshot,
    Publication,
    ServiceConfig,
    User,
    uid,
)
from .limits import lock_owner
from .publishing import valid_https
from .services import SERVICE_DB, bounded_json, decrypt, service_env, service_scope


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)
    captured_at: float = Field(gt=0)
    evidence: str = Field(min_length=1, max_length=500)
    impressions: int | None = Field(default=None, ge=0, le=10**12)
    clicks: int | None = Field(default=None, ge=0, le=10**12)
    conversions: int | None = Field(default=None, ge=0, le=10**12)
    revenue: float | None = Field(default=None, ge=0, le=10**12)
    cost: float | None = Field(default=None, ge=0, le=10**12)
    likes: int | None = Field(default=None, ge=0, le=10**12)
    comments: int | None = Field(default=None, ge=0, le=10**12)
    collects: int | None = Field(default=None, ge=0, le=10**12)
    shares: int | None = Field(default=None, ge=0, le=10**12)

    @model_validator(mode="after")
    def valid_observation(self):
        if self.captured_at > time.time() + 60:
            raise ValueError("采集时间不能在未来")
        funnel = [v for v in (self.impressions, self.clicks, self.conversions) if v is not None]
        if funnel != sorted(funnel, reverse=True):
            raise ValueError("指标口径不一致")
        if all(getattr(self, k) is None for k in METRIC_FIELDS):
            raise ValueError("没有返回任何可用指标")
        return self


METRIC_FIELDS = (
    "impressions",
    "clicks",
    "conversions",
    "revenue",
    "cost",
    "likes",
    "comments",
    "collects",
    "shares",
)


class SubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    interval_minutes: int = Field(default=360, ge=60, le=10080)


def collection_config(platform):
    prefix = "CONTENTPILOT_METRICS_" + platform.upper()
    url, key = service_env(prefix + "_URL", ""), service_env(prefix + "_TOKEN", "")
    if valid_https(url) and key:
        return {"provider": "gateway", "configured": True}
    db = SERVICE_DB.get()
    if db:
        with db.session() as s:
            row = s.get(ServiceConfig, "metrics_" + platform)
            if row and not decrypt(row)["enabled"]:
                return {"provider": "gateway", "configured": False}
    if platform == "xiaohongshu" and service_env("TIKHUB_API_KEY", ""):
        return {"provider": "tikhub_public", "configured": True}
    return {"provider": "gateway", "configured": False}


def fetch_observation(pub, request_id):
    service = collection_config(pub.platform)
    if not service["configured"]:
        raise ValueError("采集服务未配置")
    if service["provider"] == "tikhub_public":
        from .tikhub import BASE_URL, DETAIL_PATH, _card, _get_json, _id, _items

        parsed = urlsplit(pub.url or "")
        note_id = parsed.path.rstrip("/").split("/")[-1]
        if parsed.hostname not in {
            "www.xiaohongshu.com",
            "xiaohongshu.com",
            "m.xiaohongshu.com",
        } or not re.fullmatch(r"[0-9a-fA-F]{24}", note_id):
            raise ValueError("TikHub 采集需要完整小红书笔记链接，不能使用短链接")
        with httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": "Bearer " + service_env("TIKHUB_API_KEY", "")},
            follow_redirects=False,
        ) as client:
            data = _get_json(client, DETAIL_PATH, {"note_id": note_id}, time.monotonic() + 20)
        cards = (
            [_card(i) for i in _items(data)]
            if isinstance(data, list) or any(k in data for k in ("notes", "items", "note_list"))
            else [_card(data)]
        )
        cards = [card for card in cards if _id(card) == note_id]
        if len(cards) != 1:
            raise ValueError("采集笔记编号不匹配")
        card = cards[0]
        stats = card.get("interact_info") or card
        mapping = {
            "likes": ("liked_count", "likes"),
            "comments": ("comment_count", "comments_count"),
            "collects": ("collected_count", "collect_count"),
            "shares": ("share_count",),
        }
        values = {}
        for name, aliases in mapping.items():
            for alias in aliases:
                value = stats.get(alias)
                if (
                    isinstance(value, (int, str))
                    and not isinstance(value, bool)
                    and re.fullmatch(r"\d+", str(value))
                ):
                    values[name] = int(value)
                    break
        return Observation(
            captured_at=time.time(),
            evidence="TikHub 小红书公开互动数据；不含后台曝光/成交",
            **values,
        )
    prefix = "CONTENTPILOT_METRICS_" + pub.platform.upper()
    result = bounded_json(
        "POST",
        service_env(prefix + "_URL", ""),
        service_env(prefix + "_TOKEN", ""),
        {"publication_id": pub.id, "platform": pub.platform, "url": pub.url},
        request_id=request_id,
    )
    if not isinstance(result, dict) or result.get("publication_id") != pub.id:
        raise ValueError("采集记录编号不匹配")
    return Observation.model_validate(result.get("metrics"))


def enqueue(s, pub, manual=False):
    lock_owner(s, pub.owner_id)
    recent = s.scalars(
        select(DataCollection)
        .join(Publication, Publication.id == DataCollection.publication_id)
        .where(
            Publication.owner_id == pub.owner_id, DataCollection.created_at >= time.time() - 86400
        )
    ).all()
    if len(recent) >= 30 or sum(r.status in {"queued", "running"} for r in recent) >= 5:
        raise HTTPException(429, "每账号每天最多 30 次采集，同时最多 5 个采集任务")
    if any(
        r.publication_id == pub.id
        and (r.status in {"queued", "running"} or manual and r.created_at > time.time() - 60)
        for r in recent
    ):
        raise HTTPException(429, "该内容正在采集或一分钟内已请求")
    row = DataCollection(publication_id=pub.id)
    s.add(row)
    s.flush()
    return row.id


def schedule_due(db, enabled=True):
    if not enabled:
        return
    now = time.time()
    with db.session() as s:
        ids = s.scalars(
            select(DataSubscription.publication_id)
            .where(DataSubscription.enabled.is_(True), DataSubscription.next_at <= now)
            .limit(100)
        ).all()
    for pid in ids:
        with db.session() as s:
            pub = s.get(Publication, pid)
            if not pub or pub.status != "published" or not s.get(User, pub.owner_id).active:
                continue
            lock_owner(s, pub.owner_id)
            row = s.get(DataSubscription, pid)
            if not s.execute(
                update(DataSubscription)
                .where(
                    DataSubscription.publication_id == pid,
                    DataSubscription.enabled.is_(True),
                    DataSubscription.next_at <= now,
                )
                .values(next_at=now + row.interval_minutes * 60)
            ).rowcount:
                continue
            if collection_config(pub.platform)["configured"]:
                try:
                    enqueue(s, pub)
                except HTTPException:
                    pass


def execute_collection(database_url, collection_id, token):
    db = Database(database_url)
    try:
        with service_scope(db):
            with db.session() as s:
                row = s.get(DataCollection, collection_id)
                if not row or row.status != "running" or row.lease_token != token:
                    return
                pub = s.get(Publication, row.publication_id)
                if not s.get(User, pub.owner_id).active:
                    raise ValueError("账号已停用")
            observation = fetch_observation(pub, collection_id)
            with db.session() as s:
                if not s.execute(
                    update(DataCollection)
                    .where(
                        DataCollection.id == collection_id,
                        DataCollection.status == "running",
                        DataCollection.lease_token == token,
                    )
                    .values(
                        status="completed",
                        observation=observation.model_dump(),
                        lease_token=None,
                        lease_until=None,
                    )
                ).rowcount:
                    return
                keys = ("impressions", "clicks", "conversions", "revenue", "cost")
                if all(getattr(observation, k) is not None for k in keys):
                    # Incomplete observations never become fabricated zero-filled business snapshots.
                    previous = s.scalar(
                        select(PerformanceSnapshot.id)
                        .where(
                            PerformanceSnapshot.publication_id == pub.id,
                            PerformanceSnapshot.captured_at == observation.captured_at,
                        )
                        .limit(1)
                    )
                    if not previous:
                        s.add(
                            PerformanceSnapshot(
                                publication_id=pub.id,
                                **observation.model_dump(
                                    include={*keys, "captured_at", "evidence"}
                                ),
                            )
                        )
    except Exception:  # noqa: BLE001 - no raw provider errors or credentials in logs/API
        with db.session() as s:
            s.execute(
                update(DataCollection)
                .where(
                    DataCollection.id == collection_id,
                    DataCollection.status == "running",
                    DataCollection.lease_token == token,
                )
                .values(
                    status="failed",
                    error="采集失败，请核对服务、权限、链接及额度；未自动重试",
                    lease_token=None,
                    lease_until=None,
                )
            )
    finally:
        db.engine.dispose()


def run_collection_once(db, settings, target=execute_collection, pulse=None):
    now = time.time()
    with db.session() as s:
        s.execute(
            update(DataCollection)
            .where(DataCollection.status == "running", DataCollection.lease_until < now)
            .values(
                status="failed",
                error="采集进程中断，未自动重发",
                lease_token=None,
                lease_until=None,
            )
        )
        ids = s.scalars(
            select(DataCollection.id)
            .where(DataCollection.status == "queued")
            .order_by(DataCollection.created_at)
            .limit(10)
        ).all()
        claim = None
        for cid in ids:
            token = uid()
            if s.execute(
                update(DataCollection)
                .where(DataCollection.id == cid, DataCollection.status == "queued")
                .values(
                    status="running", lease_token=token, lease_until=now + settings.lease_seconds
                )
            ).rowcount:
                claim = cid, token
                break
    if not claim:
        return False
    process = multiprocessing.get_context("spawn").Process(target=target, args=(db.url, *claim))
    try:
        try:
            process.start()
        except (OSError, RuntimeError):
            return True
        start = time.monotonic()
        while process.is_alive():
            process.join(min(1, settings.lease_seconds / 3))
            if pulse:
                pulse()
            if time.monotonic() - start > min(90, settings.job_timeout):
                break
            with db.session() as s:
                if not s.execute(
                    update(DataCollection)
                    .where(
                        DataCollection.id == claim[0],
                        DataCollection.status == "running",
                        DataCollection.lease_token == claim[1],
                    )
                    .values(lease_until=time.time() + settings.lease_seconds)
                ).rowcount:
                    break
    finally:
        if process.pid and process.is_alive():
            process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
        with db.session() as s:
            s.execute(
                update(DataCollection)
                .where(
                    DataCollection.id == claim[0],
                    DataCollection.status == "running",
                    DataCollection.lease_token == claim[1],
                )
                .values(
                    status="failed",
                    error="采集未完成或超时，未自动重发",
                    lease_token=None,
                    lease_until=None,
                )
            )
    return True


def register_collection(app, db, current_user, owned, settings):
    @app.get("/api/publications/{pid}/collection")
    def collection_state(pid: str, user=Depends(current_user)):
        with db.session() as s:
            pub = owned(s, Publication, pid, user)
            subscription = s.get(DataSubscription, pid)
            rows = s.scalars(
                select(DataCollection)
                .where(DataCollection.publication_id == pid)
                .order_by(DataCollection.created_at.desc())
                .limit(20)
            ).all()
            return {
                "service": collection_config(pub.platform),
                "subscription": {
                    "enabled": subscription.enabled if subscription else False,
                    "interval_minutes": subscription.interval_minutes if subscription else 360,
                    "next_at": subscription.next_at if subscription else None,
                },
                "history": [
                    {
                        k: getattr(r, k)
                        for k in ("id", "status", "observation", "error", "created_at")
                    }
                    for r in rows
                ],
            }

    @app.put("/api/publications/{pid}/collection")
    def configure_subscription(pid: str, data: SubscriptionInput, user=Depends(current_user)):
        with db.session() as s:
            pub = owned(s, Publication, pid, user)
            lock_owner(s, user.id)
            if data.enabled and (
                not settings.submissions_enabled
                or pub.status != "published"
                or not collection_config(pub.platform)["configured"]
            ):
                raise HTTPException(409, "请确认已发布、采集服务已配置且系统未暂停提交")
            row = s.get(DataSubscription, pid)
            if row:
                row.enabled, row.interval_minutes, row.next_at = (
                    data.enabled,
                    data.interval_minutes,
                    time.time(),
                )
            else:
                s.add(DataSubscription(publication_id=pid, **data.model_dump()))
        return {"ok": True}

    @app.post("/api/publications/{pid}/collect", status_code=202)
    def collect_now(pid: str, user=Depends(current_user)):
        with db.session() as s:
            pub = owned(s, Publication, pid, user)
            if (
                not settings.submissions_enabled
                or pub.status != "published"
                or not collection_config(pub.platform)["configured"]
            ):
                raise HTTPException(409, "请确认已发布、采集服务已配置且系统未暂停提交")
            return {"id": enqueue(s, pub, manual=True), "status": "queued"}
