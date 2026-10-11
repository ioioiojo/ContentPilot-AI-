import base64
import time

import httpx
import pytest
from sqlalchemy import select, update
from test_api import MEMBER_PASSWORD, add_product, login, make_client

from contentpilot_agent.collection import execute_collection, schedule_due
from contentpilot_agent.database import (
    DataCollection,
    Job,
    MediaAsset,
    ServiceConfig,
    User,
)
from contentpilot_agent.models import ContentRequest
from contentpilot_agent.providers import MockContentProvider
from contentpilot_agent.services import service_env, service_scope
from contentpilot_agent.worker import execute_job
from contentpilot_agent.workflow import run_workflow


def headers(csrf):
    return {"X-CSRF-Token": csrf}


def configure(client, csrf, sid, values, revision=0, enabled=True, clear_secrets=None):
    response = client.put(
        "/api/admin/services/" + sid,
        headers=headers(csrf),
        json={
            "revision": revision,
            "enabled": enabled,
            "values": values,
            "clear_secrets": clear_secrets or [],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def queued_job(client, db, csrf, **extra):
    product = add_product(client, csrf)
    response = client.post(
        "/api/jobs",
        headers=headers(csrf),
        json={
            "product_id": product["id"],
            "audience": "通勤咖啡上班族",
            "platforms": ["xiaohongshu"],
            "content_count": 1,
            **extra,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def approved_job(client, db, csrf):
    jid = queued_job(client, db, csrf)
    execute_job(db.url, *db.claim())
    job = client.get("/api/jobs/" + jid).json()
    response = client.post(
        "/api/jobs/" + jid + "/review",
        headers=headers(csrf),
        json={
            "revision": job["revision"],
            "action": "approve",
            "note": "核对测试商品事实",
            "contents": job["contents"],
        },
    )
    assert response.status_code == 200, response.text
    return client.get("/api/jobs/" + jid).json()


def publication(client, csrf, job, **extra):
    response = client.post(
        "/api/publications",
        headers=headers(csrf),
        json={
            "job_id": job["id"],
            "revision": job["revision"],
            "content_index": 0,
            "request_key": "extension-publication-key",
            "url": "https://www.xiaohongshu.com/explore/697c0eee000000000a03c308",
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_service_encryption_blank_preserve_disable_and_ownership(tmp_path):
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        row = configure(client, csrf, "search", {"TIKHUB_API_KEY": "only-test-hidden-secret"})
        assert "only-test-hidden-secret" not in str(row)
        assert row["fields"][0]["has_value"] and not row["fields"][0]["value"]
        with db.session() as s:
            assert "only-test-hidden-secret" not in s.get(ServiceConfig, "search").encrypted
        with service_scope(db):
            assert service_env("TIKHUB_API_KEY") == "only-test-hidden-secret"
            assert service_env("CONTENTPILOT_SEARCH_XIAOHONGSHU_PROVIDER") == "tikhub"
        row = configure(client, csrf, "search", {"TIKHUB_API_KEY": ""}, revision=1)
        assert row["fields"][0]["has_value"]
        stale = client.put(
            "/api/admin/services/search",
            headers=headers(csrf),
            json={"revision": 1, "enabled": True, "values": {}},
        )
        assert stale.status_code == 409
        configure(
            client, csrf, "search", {}, revision=2, enabled=False, clear_secrets=["TIKHUB_API_KEY"]
        )
        with service_scope(db):
            assert service_env("TIKHUB_API_KEY") == ""
        member_csrf = login(client, "member", MEMBER_PASSWORD)
        assert client.get("/api/admin/services").status_code == 403
        assert (
            client.put(
                "/api/admin/services/search",
                headers=headers(member_csrf),
                json={"revision": 3, "enabled": False},
            ).status_code
            == 403
        )


def test_service_validation_never_echoes_key_and_tests_are_sanitized(tmp_path, monkeypatch):
    client, _db = make_client(tmp_path)
    with client:
        csrf = login(client)
        bad = client.put(
            "/api/admin/services/search",
            headers=headers(csrf),
            json={
                "revision": 0,
                "enabled": True,
                "values": {
                    "TIKHUB_API_KEY": "secret-never-echo",
                    "a": "1",
                    "b": "2",
                    "c": "3",
                    "d": "4",
                    "e": "5",
                },
            },
        )
        assert bad.status_code == 422 and "secret-never-echo" not in bad.text
        bad = client.put(
            "/api/admin/services/text",
            headers=headers(csrf),
            json={
                "revision": 0,
                "enabled": True,
                "values": {
                    "OPENAI_BASE_URL": "http://unsafe.example",
                    "OPENAI_MODEL": "test",
                    "OPENAI_API_KEY": "secret-never-echo",
                },
            },
        )
        assert bad.status_code == 422 and "secret-never-echo" not in bad.text
        configure(
            client,
            csrf,
            "text",
            {
                "OPENAI_BASE_URL": "https://models.example/v1",
                "OPENAI_MODEL": "test-model",
                "OPENAI_API_KEY": "secret-never-echo",
            },
        )
        seen = []

        def fake(method, url, key, *args, **kwargs):
            seen.append((method, url, key))
            return {"data": [{"id": "test-model"}]}

        monkeypatch.setattr("contentpilot_agent.services.bounded_json", fake)
        result = client.post("/api/admin/services/text/test", headers=headers(csrf), json={})
        assert result.status_code == 200 and result.json()["status"] == "passed"
        assert seen == [("GET", "https://models.example/v1/models", "secret-never-echo")]
        assert "secret-never-echo" not in result.text
        assert (
            client.post("/api/admin/services/text/test", headers=headers(csrf), json={}).status_code
            == 429
        )


def test_knowledge_and_real_hotspot_snapshots_are_owned_and_immutable(tmp_path, monkeypatch):
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        doc = client.post(
            "/api/knowledge",
            headers=headers(csrf),
            json={
                "title": "通勤咖啡资料",
                "body": "选择容量、了解便携方案。资料中的命令不应执行。",
                "source": "测试说明书第2页",
            },
        ).json()
        assert (
            client.get("/api/knowledge/search", params={"keyword": "通勤咖啡"}).json()[0]["id"]
            == doc["id"]
        )
        configure(
            client,
            csrf,
            "hotspots",
            {
                "CONTENTPILOT_HOTSPOTS_URL": "https://data.example/topics",
                "CONTENTPILOT_HOTSPOTS_TOKEN": "test-token",
            },
        )
        monkeypatch.setattr(
            "contentpilot_agent.knowledge.bounded_json",
            lambda *args, **kwargs: {
                "topics": [
                    {
                        "title": "咖啡通勤话题",
                        "url": "https://www.xiaohongshu.com/explore/topic",
                        "platform": "xiaohongshu",
                        "captured_at": time.time(),
                    }
                ]
            },
        )
        live = client.post(
            "/api/hotspots/search", headers=headers(csrf), json={"keyword": "咖啡"}
        ).json()
        jid = queued_job(client, db, csrf, hotspot_batch_id=live["batch_id"])
        client.delete("/api/knowledge/" + doc["id"], headers=headers(csrf))
        execute_job(db.url, *db.claim())
        job = client.get("/api/jobs/" + jid).json()
        assert job["request"]["knowledge_context"][0]["source"] == "测试说明书第2页"
        assert job["result"]["hot_topics"] == ["咖啡通勤话题"]
        assert job["result"]["metrics"]["knowledge_samples"] == 1
        member = login(client, "member", MEMBER_PASSWORD)
        assert client.get("/api/knowledge").json() == []
        product = add_product(client, member)
        stolen = client.post(
            "/api/jobs",
            headers=headers(member),
            json={
                "product_id": product["id"],
                "audience": "用户",
                "hotspot_batch_id": live["batch_id"],
            },
        )
        assert stolen.status_code == 404


def test_assigned_reviewer_can_only_read_and_review_assigned_job(tmp_path):
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        with db.session() as s:
            member_id = s.scalar(select(User.id).where(User.username == "member"))
        assert (
            client.put(
                "/api/admin/reviewers/" + member_id, headers=headers(csrf), json={"enabled": True}
            ).status_code
            == 200
        )
        jid = queued_job(client, db, csrf)
        other = queued_job(client, db, csrf)
        execute_job(db.url, *db.claim())
        job = client.get("/api/jobs/" + jid).json()
        assigned = client.put(
            "/api/jobs/" + jid + "/assignment",
            headers=headers(csrf),
            json={"revision": job["revision"], "reviewer_id": member_id},
        )
        assert assigned.status_code == 200
        job = client.get("/api/jobs/" + jid).json()
        payload = {
            "revision": job["revision"],
            "action": "approve",
            "note": "独立核对",
            "contents": job["contents"],
        }
        assert (
            client.post(
                "/api/jobs/" + jid + "/review", headers=headers(csrf), json=payload
            ).status_code
            == 403
        )
        member = login(client, "member", MEMBER_PASSWORD)
        assert client.get("/api/jobs/" + other).status_code == 404
        assert client.get("/api/jobs/" + jid).json()["is_owner"] is False
        assert client.get("/api/review-inbox").json()[0]["id"] == jid
        assert client.get("/api/jobs/" + jid + "/export").status_code == 404
        assert (
            client.post(
                "/api/jobs/" + jid + "/review", headers=headers(member), json=payload
            ).status_code
            == 200
        )
        assert client.get("/api/review-inbox").json() == []
        admin = login(client)
        detail = client.get("/api/jobs/" + jid).json()
        assert detail["audit"][-1]["reviewer"] == "member"
        assert client.get("/api/jobs/" + jid + "/export").status_code == 200
        assert (
            client.put(
                "/api/jobs/" + jid + "/assignment",
                headers=headers(admin),
                json={"revision": detail["revision"], "reviewer_id": None},
            ).status_code
            == 409
        )


def test_checkpoint_resume_skips_successful_model_steps_and_fences_old_worker(
    tmp_path, monkeypatch
):
    class InterruptedProvider(MockContentProvider):
        def generate_content(self, request, plan):
            raise ValueError("interrupted after plan")

    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        jid = queued_job(client, db, csrf)
        monkeypatch.setattr(
            "contentpilot_agent.worker.provider_from_env", lambda mode: InterruptedProvider()
        )
        claim = db.claim()
        execute_job(db.url, *claim)
        job = client.get("/api/jobs/" + jid).json()
        assert job["status"] == "failed" and job["checkpoint"] == "generate"
        with pytest.raises(RuntimeError):
            db.save_checkpoint(jid, claim[1], {})

        class ResumeProvider(MockContentProvider):
            def create_plan(self, *args, **kwargs):
                raise AssertionError("plan must not be repeated")

        monkeypatch.setattr(
            "contentpilot_agent.worker.provider_from_env", lambda mode: ResumeProvider()
        )
        assert client.post("/api/jobs/" + jid + "/resume", headers=headers(csrf)).status_code == 202
        execute_job(db.url, *db.claim())
        job = client.get("/api/jobs/" + jid).json()
        assert job["status"] == "pending_review"
        assert job["result"]["metrics"]["resumed_from"] == "generate"
        assert job["attempts"] == 2


def test_checkpoint_finishes_without_repeating_review_and_rejects_changed_brief():
    brief = ContentRequest(
        product_name="咖啡", product_description="便携", audience="通勤用户", content_count=1
    )
    saved = []
    original = run_workflow(brief, provider=MockContentProvider(), checkpoint=saved.append)

    class NoCalls(MockContentProvider):
        def create_plan(self, *args, **kwargs):
            raise AssertionError("no calls expected")

        generate_content = review_content = revise_content = create_plan

    resumed = run_workflow(brief, provider=NoCalls(), initial_checkpoint=saved[-1])
    assert resumed.contents == original.contents
    assert resumed.metrics["resumed_from"] == "finish"
    with pytest.raises(ValueError, match="输入不一致"):
        run_workflow(
            brief.model_copy(update={"audience": "changed"}),
            provider=NoCalls(),
            initial_checkpoint=saved[-1],
        )


@pytest.mark.parametrize("complete", [False, True])
def test_automatic_collection_schedules_deduplicates_and_preserves_unknowns(
    tmp_path, monkeypatch, complete
):
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        configure(
            client,
            csrf,
            "metrics_xiaohongshu",
            {
                "CONTENTPILOT_METRICS_XIAOHONGSHU_URL": "https://data.example/metrics",
                "CONTENTPILOT_METRICS_XIAOHONGSHU_TOKEN": "test-token",
            },
        )
        job = approved_job(client, db, csrf)
        pub = publication(client, csrf, job)
        assert (
            client.put(
                "/api/publications/" + pub["id"] + "/collection",
                headers=headers(csrf),
                json={"enabled": True, "interval_minutes": 60},
            ).status_code
            == 200
        )
        with service_scope(db):
            schedule_due(db)
            schedule_due(db)
        with db.session() as s:
            rows = s.scalars(select(DataCollection)).all()
            assert len(rows) == 1
            cid = rows[0].id
            s.execute(
                update(DataCollection)
                .where(DataCollection.id == cid)
                .values(status="running", lease_token="test-lease")
            )

        def upstream(method, url, key, payload, **kwargs):
            assert payload["url"] == pub["url"] and payload["publication_id"] == pub["id"]
            assert kwargs["request_id"] == cid
            metrics = {"captured_at": time.time(), "evidence": "平台测试报表", "likes": 7}
            if complete:
                metrics.update(impressions=100, clicks=10, conversions=1, revenue=20, cost=5)
            return {"publication_id": pub["id"], "metrics": metrics}

        monkeypatch.setattr("contentpilot_agent.collection.bounded_json", upstream)
        execute_collection(db.url, cid, "test-lease")
        state = client.get("/api/publications/" + pub["id"] + "/collection").json()
        assert state["history"][0]["status"] == "completed"
        assert state["history"][0]["observation"]["likes"] == 7
        current = client.get("/api/publications").json()[0]
        if complete:
            assert current["metrics"]["ctr"] == 0.1 and current["metrics"]["roi"] == 3
        else:
            assert current["metrics"] is None
            assert state["history"][0]["observation"]["impressions"] is None
        member = login(client, "member", MEMBER_PASSWORD)
        assert client.get("/api/publications/" + pub["id"] + "/collection").status_code == 404
        assert (
            client.post(
                "/api/publications/" + pub["id"] + "/collect", headers=headers(member)
            ).status_code
            == 404
        )


def test_publish_includes_only_current_owner_verified_assets_and_does_not_retry(
    tmp_path, monkeypatch
):
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        configure(
            client,
            csrf,
            "publish_xiaohongshu",
            {
                "CONTENTPILOT_PUBLISH_XIAOHONGSHU_URL": "https://publish.example/posts",
                "CONTENTPILOT_PUBLISH_XIAOHONGSHU_TOKEN": "test-token",
            },
        )
        job = approved_job(client, db, csrf)
        with db.session() as s:
            asset = MediaAsset(
                owner_id=s.get(Job, job["id"]).owner_id,
                job_id=job["id"],
                revision=job["revision"],
                content=job["contents"][0],
                source={},
                prompt="测试图片",
                kind="image",
                status="completed",
                mime="image/png",
                blob=base64.b64encode(b"\x89PNG\r\n\x1a\nTEST").decode(),
            )
            s.add(asset)
            s.flush()
            aid = asset.id
        calls = []
        original = httpx.Client

        def upstream(request):
            import json

            payload = json.loads(request.content)
            calls.append(payload)
            assert payload["assets"][0]["id"] == aid
            assert payload["assets"][0]["mime"] == "image/png"
            return httpx.Response(
                200,
                json={
                    "status": "published",
                    "url": "https://www.xiaohongshu.com/explore/published",
                },
            )

        monkeypatch.setattr(
            "contentpilot_agent.publishing.httpx.Client",
            lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(upstream)),
        )
        first = publication(client, csrf, job, channel="gateway", media_ids=[aid])
        second = publication(client, csrf, job, channel="gateway", media_ids=[aid])
        assert first["id"] == second["id"] and len(calls) == 1
        assert "b64_json" not in str(first["source"])
        changed = client.post(
            "/api/publications",
            headers=headers(csrf),
            json={
                "job_id": job["id"],
                "revision": job["revision"],
                "content_index": 0,
                "request_key": "extension-publication-key",
                "channel": "gateway",
                "media_ids": [],
            },
        )
        assert changed.status_code == 409


def test_service_snapshot_keeps_endpoint_and_key_paired_during_updates(tmp_path):
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        configure(
            client,
            csrf,
            "text",
            {
                "OPENAI_BASE_URL": "https://first.example/v1",
                "OPENAI_MODEL": "model",
                "OPENAI_API_KEY": "first-test-key",
            },
        )
        with service_scope(db):
            assert service_env("OPENAI_BASE_URL") == "https://first.example/v1"
            configure(
                client,
                csrf,
                "text",
                {"OPENAI_BASE_URL": "https://next.example/v1", "OPENAI_API_KEY": "next-test-key"},
                revision=1,
            )
            assert service_env("OPENAI_API_KEY") == "first-test-key"
        with service_scope(db):
            assert service_env("OPENAI_BASE_URL") == "https://next.example/v1"
            assert service_env("OPENAI_API_KEY") == "next-test-key"


def test_encrypted_configuration_backup_and_matching_key_recovery(tmp_path):
    import os
    import subprocess
    import sys

    from contentpilot_agent.config_backup import existing_key, restore_key, verify_key
    from contentpilot_agent.database import Database

    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        configure(client, csrf, "search", {"TIKHUB_API_KEY": "key-for-tests"})
    key = existing_key()
    verify_key(db, key)
    from cryptography.fernet import Fernet, InvalidToken

    with pytest.raises(InvalidToken):
        restore_key(db, Fernet.generate_key())
    restore_key(db, key)
    target = tmp_path / "copy.db"
    result = subprocess.run(
        [sys.executable, "-m", "contentpilot_agent.manage", "backup-local", str(target)],
        env={**os.environ, "DATABASE_URL": db.url},
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert key not in result.stdout and key not in result.stderr
    assert target.with_suffix(".db.key").read_bytes() == key
    backup_db = Database("sqlite:///" + target.as_posix())
    try:
        verify_key(backup_db, key)
    finally:
        backup_db.engine.dispose()


def test_tikhub_public_metrics_do_not_invent_business_metrics(tmp_path, monkeypatch):
    from contentpilot_agent.database import PerformanceSnapshot

    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        configure(client, csrf, "search", {"TIKHUB_API_KEY": "test-public-key"})
        job = approved_job(client, db, csrf)
        pub = publication(client, csrf, job)
        response = client.post("/api/publications/" + pub["id"] + "/collect", headers=headers(csrf))
        assert response.status_code == 202
        cid = response.json()["id"]
        with db.session() as s:
            s.execute(
                update(DataCollection)
                .where(DataCollection.id == cid)
                .values(status="running", lease_token="public-test-token")
            )
        original = httpx.Client
        seen = []

        def upstream(request):
            seen.append(request.url.path)
            assert request.headers["authorization"] == "Bearer test-public-key"
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "notes": [
                            {
                                "id": "697c0eee000000000a03c308",
                                "interact_info": {
                                    "liked_count": "12",
                                    "comment_count": 0,
                                    "collected_count": "3",
                                    "share_count": "2",
                                    "views": 999,
                                },
                            }
                        ]
                    },
                },
            )

        monkeypatch.setattr(
            "contentpilot_agent.collection.httpx.Client",
            lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(upstream)),
        )
        execute_collection(db.url, cid, "obsolete-token")
        assert not seen
        execute_collection(db.url, cid, "public-test-token")
        observation = client.get("/api/publications/" + pub["id"] + "/collection").json()[
            "history"
        ][0]["observation"]
        assert observation["likes"] == 12 and observation["comments"] == 0
        assert observation["impressions"] is None and observation["conversions"] is None
        with db.session() as s:
            assert s.scalar(select(PerformanceSnapshot.id)) is None
