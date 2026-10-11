"""Owner-isolated local retrieval and explicit live hotspot gateway."""

import re
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .database import KnowledgeDocument
from .models import Platform
from .publishing import valid_https
from .services import bounded_json, service_env


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    source: str = Field(min_length=1, max_length=1000)


class HotspotInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    keyword: str = Field(min_length=1, max_length=100)
    platforms: list[Platform] = Field(
        default_factory=lambda: ["xiaohongshu"], min_length=1, max_length=3
    )


class Hotspot(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    platform: Platform
    captured_at: float = Field(gt=0)


def fetch_hotspots(keyword, platforms):
    url, key = (
        service_env("CONTENTPILOT_HOTSPOTS_URL", ""),
        service_env("CONTENTPILOT_HOTSPOTS_TOKEN", ""),
    )
    if not valid_https(url) or not key:
        raise ValueError("实时热点服务未配置")
    result = bounded_json(
        "POST", url, key, {"keyword": keyword, "platforms": platforms, "limit": 10}
    )
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("topics"), list)
        or len(result["topics"]) > 10
    ):
        raise ValueError("热点数据格式无效")
    topics = [Hotspot.model_validate(row) for row in result["topics"]]
    if any(
        not valid_https(t.url)
        or t.platform not in platforms
        or not time.time() - 86400 <= t.captured_at <= time.time() + 60
        for t in topics
    ):
        raise ValueError("热点来源、平台或时间无效")
    return [t.model_dump() for t in topics]


def document_data(row):
    return {key: getattr(row, key) for key in ("id", "title", "body", "source", "created_at")}


def retrieve(session, owner_id, query, limit=5):
    text = query.casefold()
    tokens = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text))
    tokens.update(
        text[i : i + 2]
        for i in range(len(text) - 1)
        if all("\u4e00" <= c <= "\u9fff" for c in text[i : i + 2])
    )
    rows = session.scalars(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.owner_id == owner_id)
        .order_by(KnowledgeDocument.created_at.desc())
        .limit(500)
    ).all()
    ranked = []
    for row in rows:
        content = (row.title + " " + row.body).casefold()
        score = sum(2 if len(t) > 1 else 1 for t in tokens if t in content)
        if score:
            ranked.append((score, row))
    return [
        {
            "id": row.id,
            "title": row.title,
            "excerpt": row.body[:2400],
            "source": row.source,
            "created_at": row.created_at,
            "score": score,
        }
        for score, row in sorted(ranked, key=lambda item: (-item[0], item[1].id))[:limit]
    ]


def register_knowledge(app, db, current_user, owned):
    @app.get("/api/knowledge")
    def documents(user=Depends(current_user)):
        with db.session() as s:
            return [
                document_data(row)
                for row in s.scalars(
                    select(KnowledgeDocument)
                    .where(KnowledgeDocument.owner_id == user.id)
                    .order_by(KnowledgeDocument.created_at.desc())
                    .limit(500)
                )
            ]

    @app.post("/api/knowledge", status_code=201)
    def add_document(data: DocumentInput, user=Depends(current_user)):
        from .limits import lock_owner

        with db.session() as s:
            lock_owner(s, user.id)
            if (
                s.scalar(
                    select(func.count())
                    .select_from(KnowledgeDocument)
                    .where(KnowledgeDocument.owner_id == user.id)
                )
                >= 500
            ):
                raise HTTPException(429, "每账号最多 500 份知识资料")
            row = KnowledgeDocument(owner_id=user.id, **data.model_dump())
            s.add(row)
            s.flush()
            return document_data(row)

    @app.put("/api/knowledge/{document_id}")
    def edit_document(document_id: str, data: DocumentInput, user=Depends(current_user)):
        with db.session() as s:
            row = owned(s, KnowledgeDocument, document_id, user)
            for key, value in data.model_dump().items():
                setattr(row, key, value)
            return document_data(row)

    @app.delete("/api/knowledge/{document_id}")
    def delete_document(document_id: str, user=Depends(current_user)):
        with db.session() as s:
            s.delete(owned(s, KnowledgeDocument, document_id, user))
        return {"ok": True}

    @app.get("/api/knowledge/search")
    def search_knowledge(keyword: str, user=Depends(current_user)):
        if not keyword.strip() or len(keyword) > 300:
            raise HTTPException(422, "关键词须为 1–300 字")
        with db.session() as s:
            return retrieve(s, user.id, keyword)

    @app.post("/api/hotspots/search")
    def hotspots(data: HotspotInput, user=Depends(current_user)):
        # Share the existing per-owner search budget with Xiaohongshu search.
        from .database import ReferenceSearch
        from .limits import lock_owner

        if not service_env("CONTENTPILOT_HOTSPOTS_TOKEN", ""):
            raise HTTPException(409, "实时热点服务未配置，请联系管理员")
        with db.session() as s:
            lock_owner(s, user.id)
            recent = s.scalars(
                select(ReferenceSearch).where(
                    ReferenceSearch.owner_id == user.id,
                    ReferenceSearch.created_at >= time.time() - 86400,
                )
            ).all()
            if len(recent) >= 30 or any(r.created_at >= time.time() - 60 for r in recent):
                raise HTTPException(429, "数据搜索每分钟最多 1 次、每天 30 次")
            row = ReferenceSearch(owner_id=user.id, keyword=data.keyword)
            s.add(row)
            s.flush()
            rid = row.id
        try:
            topics = fetch_hotspots(data.keyword, data.platforms)
        except Exception:  # noqa: BLE001 - never expose raw provider responses
            with db.session() as s:
                s.get(ReferenceSearch, rid).status = "failed"
            raise HTTPException(502, "热点获取失败，请检查服务；未使用示例替代") from None
        with db.session() as s:
            s.get(ReferenceSearch, rid).status = "completed"
            from .database import HotspotBatch

            batch = HotspotBatch(owner_id=user.id, topics=topics)
            s.add(batch)
            s.flush()
            batch_id = batch.id
        return {"topics": topics, "source": "gateway", "batch_id": batch_id}
