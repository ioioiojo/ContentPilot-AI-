"""Owner-scoped publishing, evaluation, media, and feedback HTTP routes."""

import base64
import json
import time
from typing import Literal

from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .collection import collection_config
from .database import HumanEvaluation, Job, MediaAsset, PerformanceSnapshot, Publication
from .growth import evaluation_data, growth_report, latest_evaluations, publication_data
from .limits import enforce_limits, lock_owner
from .media import media_capabilities
from .publishing import gateway_config, publish, valid_https
from .references import search_configured, search_service
from .services import service_env
from .skills import SKILLS


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class PublicationInput(Input):
    job_id: str
    revision: int = Field(ge=0)
    content_index: int = Field(ge=0, le=9)
    request_key: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    channel: Literal["manual", "gateway"] = "manual"
    url: str = Field(default="", max_length=2000)
    experiment: str = Field(default="", max_length=120)
    variant: str = Field(default="A", min_length=1, max_length=32)
    media_ids: list[str] = Field(default_factory=list, max_length=5)


class MetricsInput(Input):
    impressions: int = Field(ge=0, le=10**12)
    clicks: int = Field(ge=0, le=10**12)
    conversions: int = Field(ge=0, le=10**12)
    revenue: float = Field(ge=0, le=10**12)
    cost: float = Field(ge=0, le=10**12)
    evidence: str = Field(min_length=1, max_length=500)
    captured_at: float = Field(gt=0)

    @model_validator(mode="after")
    def valid_funnel(self):
        if not self.conversions <= self.clicks <= self.impressions:
            raise ValueError("需满足成交数 ≤ 点击数 ≤ 曝光数；请使用同一统计口径")
        if self.captured_at > time.time() + 60:
            raise ValueError("统计时间不能在未来")
        return self


class EvaluationInput(Input):
    revision: int = Field(ge=0)
    content_index: int = Field(ge=0, le=9)
    facts_score: int = Field(ge=1, le=5)
    platform_score: int = Field(ge=1, le=5)
    usefulness_score: int = Field(ge=1, le=5)
    factual_errors: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(min_length=1, max_length=2000)


class MediaInput(Input):
    revision: int = Field(ge=0)
    content_index: int = Field(ge=0, le=9)
    kind: Literal["storyboard", "image", "video"]
    prompt: str = Field(min_length=1, max_length=3000)


def asset_data(asset):
    return {
        key: getattr(asset, key)
        for key in (
            "id",
            "job_id",
            "kind",
            "revision",
            "status",
            "prompt",
            "result",
            "mime",
            "error",
            "created_at",
            "remote_id",
            "content",
        )
    }


def register_operations(app, db, current_user, owned, settings):
    def content_snapshot(session, job_id, revision, index, user, approved=True):
        job = owned(session, Job, job_id, user)
        if approved and job.status != "approved":
            raise HTTPException(409, "请先完成内容人工审核")
        if not job.contents or index >= len(job.contents):
            raise HTTPException(422, "内容序号无效")
        if job.revision != revision:
            raise HTTPException(409, "内容版本已变化，请刷新后重试")
        return job

    @app.get("/api/capabilities")
    def capabilities(user=Depends(current_user)):
        channels = {}
        for platform in ("xiaohongshu", "douyin", "wechat"):
            url, token = gateway_config(platform)
            channels[platform] = bool(valid_https(url) and token)
        return {
            "publishing": channels,
            "media": media_capabilities(settings.provider),
            "tools_mode": settings.tools_mode,
            "skills": list(SKILLS.values()),
            "reference_search": {"xiaohongshu": search_configured()},
            "reference_search_service": search_service(),
            "collection": {p: collection_config(p) for p in ("xiaohongshu", "douyin", "wechat")},
            "hotspots": bool(
                service_env("CONTENTPILOT_HOTSPOTS_URL", "")
                and service_env("CONTENTPILOT_HOTSPOTS_TOKEN", "")
            ),
        }

    @app.post("/api/publications", status_code=201)
    def create_publication(data: PublicationInput, user=Depends(current_user)):
        with db.session() as s:
            previous = s.scalar(
                select(Publication).where(Publication.request_key == data.request_key)
            )
            if previous:
                if previous.owner_id != user.id:
                    raise HTTPException(409, "重复请求编号")
                if (
                    previous.job_id,
                    previous.content_index,
                    previous.revision,
                    previous.channel,
                    previous.experiment,
                    previous.variant,
                ) != (
                    data.job_id,
                    data.content_index,
                    data.revision,
                    data.channel,
                    data.experiment,
                    data.variant,
                ):
                    raise HTTPException(409, "同一请求编号不能用于不同内容")
                if data.channel == "manual" and previous.url != data.url:
                    raise HTTPException(409, "同一请求编号不能用于不同发布链接")
                if data.media_ids != [a["id"] for a in previous.source.get("media_assets", [])]:
                    raise HTTPException(409, "同一请求编号不能用于不同素材")
                return publication_data(s, previous)
            job = content_snapshot(s, data.job_id, data.revision, data.content_index, user)
            content = job.contents[data.content_index]
            if len(data.media_ids) != len(set(data.media_ids)):
                raise HTTPException(422, "素材不能重复")
            attachments, asset_info = [], []
            for mid in data.media_ids:
                asset = owned(s, MediaAsset, mid, user)
                if (
                    asset.job_id != job.id
                    or asset.revision != job.revision
                    or asset.content != content
                    or asset.status != "completed"
                    or asset.kind not in {"image", "video"}
                    or not asset.blob
                ):
                    raise HTTPException(422, "素材须已完成并属于当前已审核文案及版本")
                asset_info.append({"id": asset.id, "mime": asset.mime, "revision": asset.revision})
                attachments.append({"id": asset.id, "mime": asset.mime, "b64_json": asset.blob})
            if sum(len(a["b64_json"]) for a in attachments) > 24_000_000:
                raise HTTPException(422, "发布素材合计过大，请减少素材")
            if data.channel == "manual" and not valid_https(data.url):
                raise HTTPException(422, "请填写已实际发布的 HTTPS 链接")
            if data.channel == "gateway":
                url, token = gateway_config(content["platform"])
                if not valid_https(url) or not token:
                    raise HTTPException(409, "该平台未连接发布网关，请先手动发布并登记链接")
            pub = Publication(
                owner_id=user.id,
                job_id=job.id,
                request_key=data.request_key,
                revision=job.revision,
                content_index=data.content_index,
                platform=content["platform"],
                content=content,
                source={**job.source, **({"media_assets": asset_info} if asset_info else {})},
                channel=data.channel,
                experiment=data.experiment,
                variant=data.variant,
                status="published" if data.channel == "manual" else "sending",
                url=data.url if data.channel == "manual" else None,
            )
            s.add(pub)
            try:
                s.flush()
            except IntegrityError:
                raise HTTPException(409, "请求正在处理，请刷新发布列表，不要重复发布")
            pub_id = pub.id
        if data.channel == "gateway":
            try:
                confirmed_url = (
                    publish(pub, attachments=attachments) if attachments else publish(pub)
                )
                status, error = "published", None
            except Exception:  # noqa: BLE001 - ambiguous public side effects must never auto-retry
                confirmed_url = None
                status, error = "unknown", "发布结果未确认，请核对平台或网关；不会自动重发"
            with db.session() as s:
                record = s.get(Publication, pub_id)
                record.status, record.url, record.error = status, confirmed_url, error
        with db.session() as s:
            return publication_data(s, s.get(Publication, pub_id))

    @app.get("/api/publications")
    def list_publications(job_id: str | None = None, user=Depends(current_user)):
        with db.session() as s:
            query = select(Publication).where(Publication.owner_id == user.id)
            if job_id:
                owned(s, Job, job_id, user)
                query = query.where(Publication.job_id == job_id)
            return [
                publication_data(s, pub)
                for pub in s.scalars(query.order_by(Publication.created_at.desc()).limit(500))
            ]

    @app.post("/api/publications/{publication_id}/metrics", status_code=201)
    def record_metrics(publication_id: str, data: MetricsInput, user=Depends(current_user)):
        with db.session() as s:
            pub = owned(s, Publication, publication_id, user)
            if pub.status != "published":
                raise HTTPException(409, "发布未确认，不能录入效果数据")
            s.add(PerformanceSnapshot(publication_id=pub.id, **data.model_dump()))
            s.flush()
            return publication_data(s, pub)

    @app.get("/api/publications/{publication_id}/metrics")
    def metric_history(publication_id: str, user=Depends(current_user)):
        with db.session() as s:
            owned(s, Publication, publication_id, user)
            return [
                {
                    key: getattr(m, key)
                    for key in (
                        "id",
                        "impressions",
                        "clicks",
                        "conversions",
                        "revenue",
                        "cost",
                        "evidence",
                        "captured_at",
                        "created_at",
                    )
                }
                for m in s.scalars(
                    select(PerformanceSnapshot)
                    .where(PerformanceSnapshot.publication_id == publication_id)
                    .order_by(PerformanceSnapshot.captured_at.desc())
                    .limit(200)
                )
            ]

    @app.post("/api/jobs/{job_id}/evaluations", status_code=201)
    def label_content(job_id: str, data: EvaluationInput, user=Depends(current_user)):
        if any(not x or len(x) > 500 for x in data.factual_errors):
            raise HTTPException(422, "事实问题应为 1–500 字说明")
        with db.session() as s:
            job = content_snapshot(
                s, job_id, data.revision, data.content_index, user, approved=False
            )
            label = HumanEvaluation(
                owner_id=user.id,
                job_id=job.id,
                content=job.contents[data.content_index],
                **data.model_dump(),
            )
            s.add(label)
            s.flush()
            return evaluation_data(label)

    @app.get("/api/evaluations/export")
    def export_labels(user=Depends(current_user)):
        with db.session() as s:
            rows = list(
                s.scalars(
                    select(HumanEvaluation)
                    .where(HumanEvaluation.owner_id == user.id)
                    .order_by(HumanEvaluation.created_at)
                    .limit(5000)
                )
            )
            body = "\n".join(
                json.dumps({**evaluation_data(e), "content": e.content}, ensure_ascii=False)
                for e in rows
            )
        return Response(
            body,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="evaluations.jsonl"'},
        )

    @app.get("/api/jobs/{job_id}/evaluations")
    def job_labels(job_id: str, user=Depends(current_user)):
        with db.session() as s:
            owned(s, Job, job_id, user)
            return [e for e in latest_evaluations(s, user.id) if e["job_id"] == job_id]

    @app.get("/api/growth")
    def growth(user=Depends(current_user)):
        with db.session() as s:
            return growth_report(s, user.id)

    @app.post("/api/jobs/{job_id}/media", status_code=202)
    def create_media(job_id: str, data: MediaInput, user=Depends(current_user)):
        if not media_capabilities(settings.provider)[data.kind]["configured"]:
            raise HTTPException(409, "该素材服务未配置；请联系管理员配置对应模型或视频网关")
        with db.session() as s:
            lock_owner(s, user.id)
            job = content_snapshot(s, job_id, data.revision, data.content_index, user)
            enforce_limits(
                s,
                MediaAsset,
                user.id,
                settings.max_active_media,
                settings.daily_media,
                settings.submissions_enabled,
            )
            asset = MediaAsset(
                owner_id=user.id,
                job_id=job.id,
                kind=data.kind,
                revision=job.revision,
                content=job.contents[data.content_index],
                source=job.source,
                prompt=data.prompt,
            )
            s.add(asset)
            s.flush()
            return asset_data(asset)

    @app.get("/api/jobs/{job_id}/media")
    def list_media(job_id: str, user=Depends(current_user)):
        with db.session() as s:
            owned(s, Job, job_id, user)
            return [
                asset_data(a)
                for a in s.scalars(
                    select(MediaAsset)
                    .where(MediaAsset.job_id == job_id)
                    .order_by(MediaAsset.created_at.desc())
                )
            ]

    @app.get("/api/media/{asset_id}/download")
    def download_media(asset_id: str, user=Depends(current_user)):
        with db.session() as s:
            asset = owned(s, MediaAsset, asset_id, user)
            if asset.status != "completed":
                raise HTTPException(409, "素材尚未完成")
            if asset.kind == "storyboard":
                body = json.dumps(asset.result, ensure_ascii=False, indent=2).encode()
                mime, extension = "application/json", "json"
            else:
                body = base64.b64decode(asset.blob)
                mime, extension = asset.mime, "png" if asset.kind == "image" else "mp4"
        return Response(
            body,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{asset_id}.{extension}"'},
        )
