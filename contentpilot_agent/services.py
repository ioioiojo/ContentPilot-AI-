"""Admin-managed encrypted service settings, shared by web and worker."""

import json
import os
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .database import ServiceAudit, ServiceConfig

SERVICE_DB = ContextVar("service_database", default=None)
SERVICE_CACHE = ContextVar("service_config_snapshot", default=None)


def definition(label, fields, description, probe=""):
    return {"label": label, "fields": fields, "description": description, "probe": probe}


# Only these fields may be changed from the UI. Database/hosts/security stay server-side.
DEFINITIONS = {
    "text": definition(
        "文字 AI",
        {"OPENAI_BASE_URL": "接口地址", "OPENAI_MODEL": "模型名称", "OPENAI_API_KEY": "API 密钥"},
        "兼容 Chat Completions JSON 的接口；保留现有文字配置",
        "models",
    ),
    "search": definition(
        "小红书搜索 · TikHub",
        {"TIKHUB_API_KEY": "API 密钥"},
        "使用 TikHub 小红书搜索及正文读取；测试会读取 1 条，可能计费",
        "tikhub",
    ),
    "search_gateway": definition(
        "小红书搜索网关",
        {
            "CONTENTPILOT_SEARCH_XIAOHONGSHU_URL": "接口地址",
            "CONTENTPILOT_SEARCH_XIAOHONGSHU_TOKEN": "授权令牌",
        },
        "自有服务须遵守项目搜索协议",
        "search",
    ),
    "image": definition(
        "图片生成",
        {
            "CONTENTPILOT_IMAGE_BASE_URL": "接口地址",
            "CONTENTPILOT_IMAGE_MODEL": "图片模型",
            "CONTENTPILOT_IMAGE_API_KEY": "API 密钥",
        },
        "支持 images.generate 和 PNG base64 的服务；连接测试不生成图片",
        "models",
    ),
    "video": definition(
        "视频生成",
        {"CONTENTPILOT_VIDEO_URL": "任务接口地址", "CONTENTPILOT_VIDEO_TOKEN": "授权令牌"},
        "须支持项目视频任务协议；连接测试不生成视频",
        "health",
    ),
    "hotspots": definition(
        "实时热点",
        {"CONTENTPILOT_HOTSPOTS_URL": "接口地址", "CONTENTPILOT_HOTSPOTS_TOKEN": "授权令牌"},
        "返回真实话题、来源链接和采集时间；须遵守热点协议",
        "hotspots",
    ),
}
for platform, label in (("xiaohongshu", "小红书"), ("douyin", "抖音"), ("wechat", "公众号")):
    prefix = "CONTENTPILOT_PUBLISH_" + platform.upper()
    DEFINITIONS["publish_" + platform] = definition(
        label + "自动发布",
        {prefix + "_URL": "发布接口地址", prefix + "_TOKEN": "授权令牌"},
        "网关需实现平台授权及发布；连接测试不执行发布",
        "health",
    )
    prefix = "CONTENTPILOT_METRICS_" + platform.upper()
    DEFINITIONS["metrics_" + platform] = definition(
        label + "运营数据",
        {prefix + "_URL": "采集接口地址", prefix + "_TOKEN": "授权令牌"},
        "返回累计指标，缺失值保留为空；须遵守数据采集协议",
        "health",
    )


def secret_field(name):
    return name.endswith(("_KEY", "_TOKEN"))


@contextmanager
def service_scope(db):
    token = SERVICE_DB.set(db)
    cache_token = SERVICE_CACHE.set({})
    try:
        yield
    finally:
        SERVICE_CACHE.reset(cache_token)
        SERVICE_DB.reset(token)


class ServiceContext:
    def __init__(self, app, db):
        self.app, self.db = app, db

    async def __call__(self, scope, receive, send):
        with service_scope(self.db):
            await self.app(scope, receive, send)


def cipher():
    key = os.getenv("CONTENTPILOT_CONFIG_KEY", "")
    if not key:
        path = Path(os.getenv("CONTENTPILOT_CONFIG_KEY_FILE", "data/service-config.key"))
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                temporary.chmod(0o600)
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink()
        key = path.read_bytes()
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise ValueError("服务加密密钥无效，请联系服务器管理员") from None


def decrypt(row):
    try:
        return json.loads(cipher().decrypt(row.encrypted.encode()))
    except (InvalidToken, ValueError):
        raise ValueError("服务配置无法解密；请恢复原加密密钥") from None


def service_env(name, default=None):
    db = SERVICE_DB.get()
    if db is not None:
        cache = SERVICE_CACHE.get()
        if cache is not None:
            if "values" not in cache:
                # Freeze endpoint+credential pairs for the whole request/task.
                names = {n for d in DEFINITIONS.values() for n in d["fields"]}
                names.add("CONTENTPILOT_SEARCH_XIAOHONGSHU_PROVIDER")
                values = {n: os.getenv(n) for n in names}
                enabled = set()
                with db.session() as s:
                    for row in s.scalars(select(ServiceConfig)):
                        if row.id not in DEFINITIONS:
                            continue
                        data = decrypt(row)
                        if data["enabled"]:
                            enabled.add(row.id)
                        for key in DEFINITIONS[row.id]["fields"]:
                            values[key] = data["values"].get(key) if data["enabled"] else ""
                if "search" in enabled:
                    values["CONTENTPILOT_SEARCH_XIAOHONGSHU_PROVIDER"] = "tikhub"
                elif "search_gateway" in enabled:
                    values["CONTENTPILOT_SEARCH_XIAOHONGSHU_PROVIDER"] = "gateway"
                cache["values"] = values
            if name in cache["values"]:
                value = cache["values"][name]
                return default if value is None else value
            return os.getenv(name, default)
        service_id = next((k for k, d in DEFINITIONS.items() if name in d["fields"]), None)
        if service_id:
            with db.session() as s:
                row = s.get(ServiceConfig, service_id)
                if row:
                    data = decrypt(row)
                    return data.get("values", {}).get(name, default) if data["enabled"] else ""
        if name == "CONTENTPILOT_SEARCH_XIAOHONGSHU_PROVIDER":
            with db.session() as s:
                for sid, mode in (("search", "tikhub"), ("search_gateway", "gateway")):
                    row = s.get(ServiceConfig, sid)
                    if row and decrypt(row)["enabled"]:
                        return mode
    return os.getenv(name, default)


def bounded_json(method, url, key, payload=None, request_id=None):
    """No redirects or retries; credentials never surface in failures."""
    headers = {"Authorization": "Bearer " + key}
    if request_id:
        headers["Idempotency-Key"] = request_id
    with httpx.Client(timeout=15, follow_redirects=False) as client:  # noqa: SIM117 - dependent stream
        with client.stream(
            method, url, headers=headers, **({"json": payload} if payload is not None else {})
        ) as response:
            response.raise_for_status()
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 2_000_000:
                    raise ValueError("服务响应超过限制")
            return json.loads(raw)


class ServiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int = Field(ge=0)
    enabled: bool
    values: dict[str, str] = Field(default_factory=dict, max_length=5)
    clear_secrets: list[str] = Field(default_factory=list, max_length=3)


class ProbeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword: str = Field(default="通勤咖啡", min_length=1, max_length=100)


def service_public(sid, row):
    definition = DEFINITIONS[sid]
    data = (
        decrypt(row)
        if row
        else {"enabled": True, "values": {n: os.getenv(n, "") for n in definition["fields"]}}
    )
    values = data["values"]
    return {
        "id": sid,
        "label": definition["label"],
        "description": definition["description"],
        "enabled": data["enabled"],
        "revision": row.revision if row else 0,
        "test_status": row.test_status if row else "untested",
        "tested_at": row.tested_at if row else None,
        "configured": data["enabled"] and all(bool(values.get(n)) for n in definition["fields"]),
        "source": "page" if row else "environment",
        "fields": [
            {
                "name": n,
                "label": label,
                "secret": secret_field(n),
                "value": "" if secret_field(n) else values.get(n, ""),
                "has_value": bool(values.get(n)),
            }
            for n, label in definition["fields"].items()
        ],
    }


def register_services(app, db, admin_user):
    @app.get("/api/admin/services")
    def list_services(user=Depends(admin_user)):
        with db.session() as s:
            return [service_public(sid, s.get(ServiceConfig, sid)) for sid in DEFINITIONS]

    @app.put("/api/admin/services/{sid}")
    def save_service(sid: str, data: ServiceInput, user=Depends(admin_user)):
        from .limits import lock_owner
        from .publishing import valid_https

        if sid not in DEFINITIONS:
            raise HTTPException(404, "服务不存在")
        fields = DEFINITIONS[sid]["fields"]
        if set(data.values) - fields.keys() or any(
            not secret_field(n) or n not in fields for n in data.clear_secrets
        ):
            raise HTTPException(422, "服务字段无效")
        with db.session() as s:
            # Serialize configuration writes across admins via a stable account lock.
            from .database import User

            first_admin = s.scalar(
                select(User.id).where(User.admin.is_(True)).order_by(User.id).limit(1)
            )
            lock_owner(s, first_admin)
            row = s.get(ServiceConfig, sid)
            if data.revision != (row.revision if row else 0):
                raise HTTPException(409, "配置已被更新，请刷新")
            values = decrypt(row)["values"] if row else {n: os.getenv(n, "") for n in fields}
            for name, value in data.values.items():
                if len(value) > 4000 or (secret_field(name) and any(c.isspace() for c in value)):
                    raise HTTPException(422, "配置值过长或密钥包含空白")
                if value or not secret_field(name):
                    values[name] = value
            for name in data.clear_secrets:
                values[name] = ""
            for name, value in values.items():
                if name.endswith("_URL") and value:
                    from urllib.parse import urlsplit

                    if not valid_https(value) or urlsplit(value).query or urlsplit(value).fragment:
                        raise HTTPException(
                            422, "接口地址须为 HTTPS，不能包含用户名、密码、查询参数或片段"
                        )
            if data.enabled and not all(values.get(n) for n in fields):
                raise HTTPException(422, "启用前请填全接口、模型和密钥")
            encrypted = (
                cipher()
                .encrypt(json.dumps({"enabled": data.enabled, "values": values}).encode())
                .decode()
            )
            if row:
                row.encrypted, row.revision, row.updated_at = (
                    encrypted,
                    row.revision + 1,
                    time.time(),
                )
                row.test_status, row.tested_at = "untested", None
            else:
                row = ServiceConfig(id=sid, encrypted=encrypted)
                s.add(row)
            if data.enabled and sid in {"search", "search_gateway"}:
                other = s.get(ServiceConfig, "search_gateway" if sid == "search" else "search")
                if other:
                    previous = decrypt(other)
                    previous["enabled"] = False
                    other.encrypted = cipher().encrypt(json.dumps(previous).encode()).decode()
                    other.revision += 1
                    other.updated_at = time.time()
                    other.test_status, other.tested_at = "untested", None
            s.add(ServiceAudit(user_id=user.id, service_id=sid, action="save"))
            s.flush()
            return service_public(sid, row)

    @app.post("/api/admin/services/{sid}/test")
    def test_service(sid: str, data: ProbeInput, user=Depends(admin_user)):
        from .limits import lock_owner

        if sid not in DEFINITIONS:
            raise HTTPException(404, "服务不存在")
        with db.session() as s:
            lock_owner(s, user.id)
            row = s.get(ServiceConfig, sid)
            meta = service_public(sid, row)
            revision = meta["revision"]
            recent = s.scalar(
                select(ServiceAudit.created_at)
                .where(
                    ServiceAudit.user_id == user.id,
                    ServiceAudit.action == "test",
                    ServiceAudit.created_at > time.time() - 60,
                )
                .limit(1)
            )
            if recent:
                raise HTTPException(429, "连接测试每分钟最多一次")
            if not meta["configured"]:
                raise HTTPException(409, "请先配置并启用服务")
            s.add(ServiceAudit(user_id=user.id, service_id=sid, action="test"))
        try:
            mode = DEFINITIONS[sid]["probe"]
            names = list(DEFINITIONS[sid]["fields"])
            values = {n: service_env(n, "") for n in names}
            url = next((v for n, v in values.items() if n.endswith("_URL")), "")
            key = next((v for n, v in values.items() if secret_field(n)), "")
            if mode == "tikhub":
                from .references import SearchInput
                from .tikhub import search_tikhub

                count = len(search_tikhub(SearchInput(keyword=data.keyword, limit=1), key))
                message = f"真实搜索和正文读取成功，返回 {count} 条；请在参考帖页面核对正文"
            elif mode == "search":
                result = bounded_json(
                    "POST",
                    url,
                    key,
                    {"platform": "xiaohongshu", "keyword": data.keyword, "limit": 1},
                )
                from .references import NoteInput

                assert isinstance(result["posts"], list) and len(result["posts"]) <= 1
                for note in result["posts"]:
                    NoteInput.model_validate(note)
                message = "搜索协议测试通过；未导入参考库"
            elif mode == "hotspots":
                from .knowledge import fetch_hotspots

                fetch_hotspots(data.keyword, ["xiaohongshu"])
                message = "热点数据格式测试通过"
            elif mode == "models":
                result = bounded_json("GET", url.rstrip("/") + "/models", key)
                model = next(v for n, v in values.items() if n.endswith("_MODEL"))
                assert any(item.get("id") == model for item in result["data"])
                message = "模型列表可访问且包含指定模型；生成能力与质量仍需实际任务验证"
            else:
                result = bounded_json("GET", url.rstrip("/") + "/health", key)
                assert result.get("status") == "ok"
                message = "网关健康检查通过；不代表已生成、发布或取得平台数据"
            status = "passed"
        except Exception:  # noqa: BLE001 - never leak provider text or secrets
            status, message = "failed", "连接或协议验证失败，请检查接口、权限和额度；未自动重试"
        with db.session() as s:
            row = s.get(ServiceConfig, sid)
            if row and row.revision == revision:
                row.test_status, row.tested_at = status, time.time()
        return {"status": status, "message": message}
