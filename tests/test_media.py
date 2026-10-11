import base64
from types import SimpleNamespace

import httpx
import pytest
from test_api import login, make_client
from test_operations import prepared

from contentpilot_agent.database import Job, MediaAsset
from contentpilot_agent.media import execute_media
from contentpilot_agent.publishing import publish, valid_https


def asset_for(db, job, kind):
    with db.session() as s:
        asset = MediaAsset(
            owner_id=s.get(Job, job["id"]).owner_id,
            job_id=job["id"],
            revision=job["revision"],
            kind=kind,
            content=job["contents"][0],
            source=job["source"],
            prompt="通勤场景",
            status="running",
            lease_token="test-token",
        )
        s.add(asset)
        s.flush()
        return asset.id


def test_storyboard_completed_download_and_stale_worker(tmp_path, monkeypatch):
    class Provider:
        model = "test-only"

        def __init__(self):
            self.usage = {"total_tokens": 20}

        def _json_call(self, system, brief):
            assert "商品事实" in brief and "不得添加" in system
            return {
                "image_prompt": "通勤示意图",
                "shots": [
                    {"seconds": 5, "visual": "办公桌", "narration": "随身携带", "caption": "通勤"}
                ],
                "disclaimer": "商品外观需核对",
            }

    monkeypatch.setattr("contentpilot_agent.media.provider_from_env", lambda mode: Provider())
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        _, job = prepared(client, db, csrf)
        asset_id = asset_for(db, job, "storyboard")
        execute_media(db.url, asset_id, "stale-token")
        assert client.get(f"/api/media/{asset_id}/download").status_code == 409
        execute_media(db.url, asset_id, "test-token")
        downloaded = client.get(f"/api/media/{asset_id}/download")
        assert downloaded.status_code == 200
        assert downloaded.json()["shots"][0]["seconds"] == 5
        assert downloaded.json()["metrics"]["token_usage"]["total_tokens"] == 20
    db.engine.dispose()


@pytest.mark.parametrize("valid", [True, False])
def test_image_api_result_validation_and_private_download(tmp_path, monkeypatch, valid):
    # A real 1x1 PNG fixture, without calling a paid service.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII="
    )
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    b64_json=base64.b64encode(png if valid else b"not-an-image").decode()
                )
            ]
        )

    monkeypatch.setenv("CONTENTPILOT_IMAGE_BASE_URL", "https://image.example/v1")
    monkeypatch.setenv("CONTENTPILOT_IMAGE_API_KEY", "test-only")
    monkeypatch.setenv("CONTENTPILOT_IMAGE_MODEL", "image-test")
    monkeypatch.setattr(
        "openai.OpenAI", lambda **kwargs: SimpleNamespace(images=SimpleNamespace(generate=generate))
    )
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        _, job = prepared(client, db, csrf)
        asset_id = asset_for(db, job, "image")
        execute_media(db.url, asset_id, "test-token")
        response = client.get(f"/api/media/{asset_id}/download")
        assert response.status_code == (200 if valid else 409)
        if valid:
            assert response.content == png
            assert response.headers["content-type"] == "image/png"
        assert calls[0]["extra_headers"]["Idempotency-Key"] == asset_id
        assert len(calls) == 1
        with db.session() as s:
            assert s.get(MediaAsset, asset_id).status == ("completed" if valid else "failed")
    db.engine.dispose()


def test_video_gateway_contract_persists_remote_id(tmp_path, monkeypatch):
    mp4 = b"\x00\x00\x00\x18ftypmp42test"
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "task-1",
                "status": "completed",
                "b64_json": base64.b64encode(mp4).decode(),
            },
        )

    original = httpx.Client
    monkeypatch.setenv("CONTENTPILOT_VIDEO_URL", "https://video.example/tasks")
    monkeypatch.setenv("CONTENTPILOT_VIDEO_TOKEN", "test-only")
    monkeypatch.setattr(
        "contentpilot_agent.media.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        _, job = prepared(client, db, csrf)
        asset_id = asset_for(db, job, "video")
        execute_media(db.url, asset_id, "test-token")
        assert client.get(f"/api/media/{asset_id}/download").content == mp4
        with db.session() as s:
            assert s.get(MediaAsset, asset_id).remote_id == "task-1"
        assert len(captured) == 1
        assert captured[0].headers["Idempotency-Key"] == asset_id
    db.engine.dispose()


def test_publishing_requires_confirmed_post_contract(monkeypatch):
    monkeypatch.setenv("CONTENTPILOT_PUBLISH_DOUYIN_URL", "https://publish.example/posts")
    monkeypatch.setenv("CONTENTPILOT_PUBLISH_DOUYIN_TOKEN", "test-only")
    replies = [
        {"status": "published", "url": "https://www.douyin.com/video/test"},
        {"status": "queued", "url": "https://www.douyin.com/video/test"},
    ]
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=replies.pop(0))

    original = httpx.Client
    monkeypatch.setattr(
        "contentpilot_agent.publishing.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    pub = SimpleNamespace(
        id="fixed-key",
        platform="douyin",
        content={"title": "测试"},
        source={"facts": "说明书"},
        revision=2,
    )
    assert publish(pub) == "https://www.douyin.com/video/test"
    with pytest.raises(ValueError):
        publish(pub)
    assert captured[0].headers["Idempotency-Key"] == "fixed-key"
    assert not valid_https("https://[")
    assert not valid_https("https://example.com:bad")
    assert not valid_https(None)
