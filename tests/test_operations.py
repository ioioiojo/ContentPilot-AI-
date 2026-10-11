import time

from fastapi.testclient import TestClient
from sqlalchemy import select
from test_api import MEMBER_PASSWORD, add_product, login, make_client

from contentpilot_agent.database import MediaAsset, SchemaVersion
from contentpilot_agent.growth import ratios
from contentpilot_agent.media import run_media_once
from contentpilot_agent.worker import execute_job


def prepared(client, db, csrf):
    product = add_product(client, csrf)
    job = client.post(
        "/api/jobs",
        headers={"X-CSRF-Token": csrf},
        json={
            "product_id": product["id"],
            "audience": "通勤人群",
            "skill_id": "decision",
        },
    ).json()
    execute_job(db.url, *db.claim())
    detail = client.get("/api/jobs/" + job["id"]).json()
    approved = client.post(
        "/api/jobs/" + job["id"] + "/review",
        headers={"X-CSRF-Token": csrf},
        json={
            "revision": detail["revision"],
            "action": "approve",
            "note": "事实核对",
            "contents": detail["contents"],
        },
    )
    assert approved.status_code == 200
    return product, client.get("/api/jobs/" + job["id"]).json()


def pub_payload(job, key="request-key-for-publication-0001"):
    return {
        "job_id": job["id"],
        "revision": job["revision"],
        "content_index": 0,
        "request_key": key,
        "url": "https://www.xiaohongshu.com/explore/test",
        "experiment": "通勤主题",
        "variant": "A",
    }


def test_publish_metrics_feedback_and_human_evaluation(tmp_path):
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        headers = {"X-CSRF-Token": csrf}
        product, job = prepared(client, db, csrf)
        payload = pub_payload(job)
        response = client.post("/api/publications", headers=headers, json=payload)
        assert response.status_code == 201, response.text
        pub = response.json()
        assert (
            client.post("/api/publications", headers=headers, json=payload).json()["id"]
            == pub["id"]
        )
        assert (
            client.post(
                "/api/publications", headers=headers, json={**payload, "content_index": 1}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/publications",
                headers=headers,
                json={**payload, "request_key": "new-request-key-0002", "revision": 0},
            ).status_code
            == 409
        )
        observed = {
            "impressions": 1000,
            "clicks": 100,
            "conversions": 10,
            "revenue": 790,
            "cost": 100,
            "captured_at": time.time() - 10,
            "evidence": "人工测试报表",
        }
        url = "/api/publications/" + pub["id"] + "/metrics"
        assert client.post(url, headers=headers, json=observed).status_code == 201
        newer = {**observed, "impressions": 2000, "captured_at": time.time() - 5}
        assert client.post(url, headers=headers, json=newer).status_code == 201
        # Backfilled older snapshots don't overwrite the newest observation.
        assert (
            client.post(
                url,
                headers=headers,
                json={**observed, "impressions": 800, "captured_at": time.time() - 20},
            ).status_code
            == 201
        )
        assert (
            client.post(url, headers=headers, json={**observed, "clicks": 2000}).status_code == 422
        )
        assert (
            client.post(
                url, headers=headers, json={**observed, "captured_at": time.time() + 999}
            ).status_code
            == 422
        )
        report = client.get("/api/growth").json()
        assert report["totals"]["impressions"] == 2000  # not 1000 + 2000 + 800
        assert report["totals"]["ctr"] == 0.05
        assert report["totals"]["roi"] == 6.9
        assert report["experiments"][0]["samples"] == 1
        assert client.get(url).status_code == 200
        new_job = client.post(
            "/api/jobs",
            headers=headers,
            json={
                "product_id": product["id"],
                "audience": "通勤人群",
                "skill_id": "tutorial",
            },
        ).json()
        assert new_job["request"]["skill"]["name"] == "使用指南"
        assert new_job["request"]["performance_context"][0]["metrics"]["ctr"] == 0.05
        label = {
            "revision": job["revision"],
            "content_index": 0,
            "facts_score": 5,
            "platform_score": 4,
            "usefulness_score": 4,
            "factual_errors": [],
            "note": "逐项核对",
        }
        assert client.post(
            "/api/jobs/" + job["id"] + "/evaluations", headers=headers, json=label
        ).json()["passed"]
        assert not client.post(
            "/api/jobs/" + job["id"] + "/evaluations",
            headers=headers,
            json={**label, "factual_errors": ["编造认证"]},
        ).json()["passed"]
        quality = client.get("/api/growth").json()["evaluation"]
        assert quality["sample_size"] == 1  # latest label per version/content
        assert quality["pass_rate"] == 0
        assert quality["factual_error_rate"] == 1
        assert "编造认证" in client.get("/api/evaluations/export").text
        assert (
            client.post(
                "/api/jobs/" + job["id"] + "/evaluations",
                headers=headers,
                json={**label, "revision": 0},
            ).status_code
            == 409
        )
    db.engine.dispose()


def test_extended_resources_enforce_owner_and_csrf(tmp_path):
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        _, job = prepared(client, db, csrf)
        pub = client.post(
            "/api/publications", headers={"X-CSRF-Token": csrf}, json=pub_payload(job)
        ).json()
        with db.session() as s:
            from contentpilot_agent.database import Job

            owner = s.get(Job, job["id"]).owner_id
            asset = MediaAsset(
                owner_id=owner,
                job_id=job["id"],
                kind="storyboard",
                revision=job["revision"],
                content={},
                source={},
                prompt="测试",
            )
            s.add(asset)
            s.flush()
            asset_id = asset.id
        assert client.post("/api/publications", json=pub_payload(job)).status_code == 403
        with TestClient(client.app) as member:
            member_csrf = login(member, "member", MEMBER_PASSWORD)
            assert member.get("/api/publications").json() == []
            assert member.get("/api/growth").json()["observed_samples"] == 0
            assert member.get("/api/evaluations/export").text == ""
            for url in (
                "/api/publications?job_id=" + job["id"],
                "/api/jobs/" + job["id"] + "/evaluations",
                "/api/jobs/" + job["id"] + "/media",
                "/api/media/" + asset_id + "/download",
                "/api/publications/" + pub["id"] + "/metrics",
            ):
                assert member.get(url).status_code == 404
            assert (
                member.post(
                    "/api/publications",
                    headers={"X-CSRF-Token": member_csrf},
                    json=pub_payload(job, "different-request-0003"),
                ).status_code
                == 404
            )
    db.engine.dispose()


def test_publish_gateway_unknown_is_not_resent(tmp_path, monkeypatch):
    client, db = make_client(tmp_path)
    monkeypatch.setenv("CONTENTPILOT_PUBLISH_XIAOHONGSHU_URL", "https://publisher.example/posts")
    monkeypatch.setenv("CONTENTPILOT_PUBLISH_XIAOHONGSHU_TOKEN", "test-only")
    calls = []

    def failed(pub):
        calls.append(pub.id)
        raise TimeoutError("sensitive upstream error must not be exposed")

    monkeypatch.setattr("contentpilot_agent.operations.publish", failed)
    with client:
        csrf = login(client)
        _, job = prepared(client, db, csrf)
        payload = {**pub_payload(job), "channel": "gateway", "url": ""}
        headers = {"X-CSRF-Token": csrf}
        one = client.post("/api/publications", headers=headers, json=payload)
        assert one.json()["status"] == "unknown"
        two = client.post("/api/publications", headers=headers, json=payload)
        assert two.json()["id"] == one.json()["id"]
        assert len(calls) == 1
        assert "sensitive" not in one.text
    db.engine.dispose()


def test_no_fake_publish_unconfigured_media_or_unknown_skill(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTENTPILOT_IMAGE_MODEL", raising=False)
    monkeypatch.delenv("CONTENTPILOT_VIDEO_URL", raising=False)
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        product, job = prepared(client, db, csrf)
        headers = {"X-CSRF-Token": csrf}
        assert (
            client.post(
                "/api/publications",
                headers=headers,
                json={**pub_payload(job), "channel": "gateway", "url": ""},
            ).status_code
            == 409
        )
        for bad_url in (
            "javascript:alert(1)",
            "http://example.com",
            "https://user:pass@example.com",
        ):
            assert (
                client.post(
                    "/api/publications", headers=headers, json={**pub_payload(job), "url": bad_url}
                ).status_code
                == 422
            )
        for kind in ("image", "video", "storyboard"):
            response = client.post(
                "/api/jobs/" + job["id"] + "/media",
                headers=headers,
                json={
                    "revision": job["revision"],
                    "content_index": 0,
                    "kind": kind,
                    "prompt": "测试",
                },
            )
            assert response.status_code == 409  # mock app never fabricates AI assets
        assert (
            client.post(
                "/api/jobs",
                headers=headers,
                json={"product_id": product["id"], "audience": "测试", "skill_id": "unknown"},
            ).status_code
            == 422
        )
        assert ratios(0, 0, 0, 0, 0) == dict.fromkeys(("ctr", "conversion_rate", "roi", "roas"))
    db.engine.dispose()


def test_schema_v1_additive_upgrade_preserves_existing_accounts(tmp_path):
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        _, job = prepared(client, db, csrf)
    from contentpilot_agent.database import HumanEvaluation, PerformanceSnapshot, Publication

    for model in (PerformanceSnapshot, Publication, HumanEvaluation, MediaAsset):
        model.__table__.drop(db.engine)
    with db.session() as s:
        s.get(SchemaVersion, 1).version = 1
    db.initialize()
    with client:
        login(client)
        upgraded = client.get("/api/jobs/" + job["id"]).json()
        assert upgraded["contents"] == job["contents"]
        assert upgraded["status"] == "approved"
        assert client.get("/api/growth").json()["observed_samples"] == 0
        with db.session() as s:
            assert s.get(SchemaVersion, 1).version == 5
    with db.session() as s:
        s.get(SchemaVersion, 1).version = 999
    import pytest

    with pytest.raises(RuntimeError):
        db.initialize()
    db.engine.dispose()


def test_mcp_client_uses_real_stdio_protocol():
    from contentpilot_agent.tools import ContentTools

    tools = ContentTools(mode="mcp")
    topics = tools.collect_context("通勤杯", ["xiaohongshu"])
    assert "通勤好物" in topics
    assert tools.calls == ["mcp:collect_content_context"]


def test_media_lease_expiration_does_not_repeat_external_generation(tmp_path):
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        _, job = prepared(client, db, csrf)
        with db.session() as s:
            from contentpilot_agent.database import Job

            owner = s.get(Job, job["id"]).owner_id
            asset = MediaAsset(
                owner_id=owner,
                job_id=job["id"],
                kind="video",
                revision=job["revision"],
                content={},
                source={},
                prompt="测试",
                status="running",
                lease_token="old",
                lease_until=time.time() - 1,
                remote_id="already-charged-task",
            )
            s.add(asset)
        assert not run_media_once(db, client.app.state.settings)
        with db.session() as s:
            asset = s.scalar(select(MediaAsset))
            assert asset.status == "failed"
            assert asset.remote_id == "already-charged-task"
    db.engine.dispose()
