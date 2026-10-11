"""Owner-scoped reference notes, TikHub adapter and configurable search gateway."""

import json
import time
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from .database import ReferencePost, ReferenceSearch
from .limits import lock_owner
from .publishing import valid_https
from .services import service_env
from .tikhub import SearchProviderError, search_tikhub


def valid_note_url(url):
    if not valid_https(url):
        return False
    parsed = urlsplit(url)
    return parsed.port in (None, 443) and (
        parsed.hostname in {"xiaohongshu.com", "www.xiaohongshu.com", "m.xiaohongshu.com"}
        and parsed.path.startswith(("/explore/", "/discovery/item/"))
        or parsed.hostname in {"xhslink.com", "www.xhslink.com"}
        and len(parsed.path) > 1
    )


class NoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=6000)
    url: str = Field(min_length=1, max_length=2000)
    author: str = Field(default="", max_length=120)
    keyword: str = Field(default="", max_length=100)

    @field_validator("url")
    @classmethod
    def note_url(cls, value):
        if not valid_note_url(value):
            raise ValueError("请填写小红书笔记或 xhslink 分享链接（HTTPS）")
        return value


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    keyword: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=5, ge=1, le=10)
    sort_type: Literal[
        "general", "time_descending", "popularity_descending", "collect_descending"
    ] = "general"
    note_type: Literal["不限", "普通笔记", "视频笔记"] = "普通笔记"


def search_config():
    return service_env("CONTENTPILOT_SEARCH_XIAOHONGSHU_URL", ""), service_env(
        "CONTENTPILOT_SEARCH_XIAOHONGSHU_TOKEN", ""
    )


def search_configured():
    return search_service()["configured"]


def search_service():
    mode = service_env("CONTENTPILOT_SEARCH_XIAOHONGSHU_PROVIDER", "auto")
    url, token = search_config()
    if mode == "auto":
        mode = "gateway" if valid_https(url) and token else "tikhub"
    key = service_env("TIKHUB_API_KEY", "").strip()
    configured = (
        (bool(key) and not any(char.isspace() for char in key))
        if mode == "tikhub"
        else (bool(valid_https(url) and token) if mode == "gateway" else False)
    )
    return {
        "provider": mode,
        "configured": configured,
        "max_results": 5 if mode == "tikhub" else 10,
    }


def note_data(note):
    return {
        key: getattr(note, key)
        for key in ("id", "title", "body", "url", "author", "keyword", "source_type", "captured_at")
    }


def search_notes(data, request_id):
    if search_service()["provider"] == "tikhub":
        raw = search_tikhub(data, service_env("TIKHUB_API_KEY", "").strip())
        return [NoteInput.model_validate({**note, "keyword": data.keyword}) for note in raw]
    url, token = search_config()
    if not valid_https(url) or not token:
        raise ValueError("Search service not configured")
    # Fixed administrator endpoint only; no direct fetch of user-supplied note URLs.
    with httpx.Client(timeout=15, follow_redirects=False) as client:  # noqa: SIM117 - dependent stream
        with client.stream(
            "POST",
            url,
            headers={
                "Authorization": "Bearer " + token,
                "Idempotency-Key": request_id,
            },
            json={"platform": "xiaohongshu", **data.model_dump(include={"keyword", "limit"})},
        ) as response:
            response.raise_for_status()
            payload = bytearray()
            for chunk in response.iter_bytes():
                payload.extend(chunk)
                if len(payload) > 512_000:
                    raise ValueError("Search response too large")
    result = json.loads(payload)
    # Missing/empty/incomplete payloads are not invented or silently accepted.
    if not isinstance(result, dict) or not isinstance(result.get("posts"), list):
        raise TypeError("Search response format invalid")
    if len(result["posts"]) > data.limit:
        raise ValueError("Search result limit exceeded")
    notes = []
    seen = set()
    for raw in result["posts"]:
        if not isinstance(raw, dict):
            raise TypeError("Search note format invalid")
        note = NoteInput.model_validate({**raw, "keyword": data.keyword})
        parsed = urlsplit(note.url)
        identity = (parsed.hostname, parsed.path)
        if identity not in seen:
            seen.add(identity)
            notes.append(note)
    return notes


def selected_snapshots(session, owner_id, ids):
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "参考帖不能重复")
    notes = (
        session.scalars(
            select(ReferencePost).where(
                ReferencePost.owner_id == owner_id, ReferencePost.id.in_(ids)
            )
        ).all()
        if ids
        else []
    )
    by_id = {note.id: note for note in notes}
    if len(by_id) != len(ids):
        raise HTTPException(404, "参考帖不存在或不属于当前账号")
    return [note_data(by_id[note_id]) for note_id in ids]


def register_references(app, db, current_user, owned):
    @app.get("/api/references")
    def list_notes(keyword: str = "", offset: int = 0, user=Depends(current_user)):
        if len(keyword) > 100 or not 0 <= offset <= 100000:
            raise HTTPException(422, "筛选或分页参数无效")
        with db.session() as session:
            query = select(ReferencePost).where(ReferencePost.owner_id == user.id)
            if keyword.strip():
                query = query.where(
                    ReferencePost.keyword.contains(keyword.strip(), autoescape=True)
                )
            return [
                note_data(note)
                for note in session.scalars(
                    query.order_by(ReferencePost.captured_at.desc(), ReferencePost.id)
                    .offset(offset)
                    .limit(50)
                )
            ]

    @app.post("/api/references", status_code=201)
    def add_note(data: NoteInput, user=Depends(current_user)):
        with db.session() as session:
            lock_owner(session, user.id)
            count = session.scalar(
                select(func.count())
                .select_from(ReferencePost)
                .where(ReferencePost.owner_id == user.id)
            )
            if count >= 1000:
                raise HTTPException(429, "参考库已达 1000 条，请先删除不需要的参考帖")
            note = ReferencePost(owner_id=user.id, source_type="manual", **data.model_dump())
            session.add(note)
            session.flush()
            return note_data(note)

    @app.delete("/api/references/{note_id}")
    def delete_note(note_id: str, user=Depends(current_user)):
        with db.session() as session:
            session.delete(owned(session, ReferencePost, note_id, user))
        return {"ok": True}

    @app.post("/api/references/search")
    def search(data: SearchInput, user=Depends(current_user)):
        service = search_service()
        if not search_configured():
            if service["provider"] == "tikhub":
                raise HTTPException(
                    409, "小红书自动搜索需要 TikHub 密钥，请管理员配置 TIKHUB_API_KEY"
                )
            raise HTTPException(409, "小红书搜索服务未接通；可打开小红书搜索后手动录入参考帖")
        if data.limit > service["max_results"]:
            raise HTTPException(422, "TikHub 每次最多读取 5 条笔记正文，请减少搜索数量")
        now = time.time()
        with db.session() as session:
            lock_owner(session, user.id)
            recent = session.scalars(
                select(ReferenceSearch).where(
                    ReferenceSearch.owner_id == user.id, ReferenceSearch.created_at >= now - 86400
                )
            ).all()
            if len(recent) >= 30 or any(row.created_at >= now - 60 for row in recent):
                raise HTTPException(429, "搜索最多每分钟 1 次、每 24 小时 30 次（含失败请求）")
            count = session.scalar(
                select(func.count())
                .select_from(ReferencePost)
                .where(ReferencePost.owner_id == user.id)
            )
            if count + data.limit > 1000:
                raise HTTPException(429, "参考库容量不足，请先删除不需要的参考帖")
            record = ReferenceSearch(owner_id=user.id, keyword=data.keyword)
            session.add(record)
            session.flush()
            search_id = record.id
        try:
            notes = search_notes(data, search_id)
        except Exception as exc:  # noqa: BLE001 - never expose provider keys or raw response errors
            with db.session() as session:
                session.get(ReferenceSearch, search_id).status = "failed"
            message = (
                str(exc)
                if isinstance(exc, SearchProviderError)
                else "搜索服务请求失败或结果格式无效，请联系管理员；未自动重试"
            )
            raise HTTPException(502, message)
        with db.session() as session:
            lock_owner(session, user.id)
            # Recheck after the network call because manual imports may have run meanwhile.
            count = session.scalar(
                select(func.count())
                .select_from(ReferencePost)
                .where(ReferencePost.owner_id == user.id)
            )
            if count + len(notes) > 1000:
                session.get(ReferenceSearch, search_id).status = "failed"
                output = None
            else:
                stored = [
                    ReferencePost(
                        owner_id=user.id, source_type=service["provider"], **note.model_dump()
                    )
                    for note in notes
                ]
                session.add_all(stored)
                session.flush()
                session.get(ReferenceSearch, search_id).status = "completed"
                output = [note_data(note) for note in stored]
        if output is None:
            raise HTTPException(429, "参考库容量不足；搜索结果未保存，请清理后重试")
        return {"keyword": data.keyword, "posts": output, "source": service["provider"]}
