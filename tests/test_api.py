import time

from fastapi.testclient import TestClient

from contentpilot_agent.api import create_app
from contentpilot_agent.config import Settings
from contentpilot_agent.database import Database, Job, User
from contentpilot_agent.security import password_hash
from contentpilot_agent.worker import execute_job

ADMIN_PASSWORD = "a-long-test-password-123"
MEMBER_PASSWORD = "another-long-test-pass-456"


def make_client(tmp_path, filename="app.db"):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / filename}", cookie_secure=False, provider="mock"
    )
    db = Database(settings.database_url)
    db.initialize()
    with db.session() as s:
        s.add_all(
            [
                User(username="admin", password=password_hash(ADMIN_PASSWORD), admin=True),
                User(username="member", password=password_hash(MEMBER_PASSWORD)),
            ]
        )
    client = TestClient(create_app(settings, db))
    return client, db


def login(client, username="admin", password=ADMIN_PASSWORD):
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf"]


def add_product(client, csrf):
    response = client.post(
        "/api/products",
        headers={"X-CSRF-Token": csrf},
        json={
            "name": "低糖咖啡",
            "facts": "低糖、便携",
            "source": "商品说明书 v1 第 2 页",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_full_team_trial_flow_and_ownership(tmp_path):
    client, db = make_client(tmp_path)
    with client:
        admin_csrf = login(client)
        assert client.get("/api/users").status_code == 200
        product = add_product(client, admin_csrf)
        queued = client.post(
            "/api/jobs",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "product_id": product["id"],
                "audience": "通勤上班族",
                "platforms": ["xiaohongshu", "douyin"],
                "content_count": 3,
            },
        )
        assert queued.status_code == 202, queued.text
        job_id = queued.json()["id"]
        assert queued.json()["status"] == "queued"
        stats = client.get("/api/stats")
        assert stats.status_code == 200
        assert stats.json()["sample_size"] == 0
        assert queued.json()["source"]["source"] == "商品说明书 v1 第 2 页"
        changed_product = client.put(
            f"/api/products/{product['id']}",
            headers={"X-CSRF-Token": admin_csrf},
            json={"name": "新版低糖咖啡", "facts": "新事实", "source": "新说明书"},
        )
        assert changed_product.status_code == 200

        claim = db.claim()
        assert claim and claim[0] == job_id
        execute_job(db.url, *claim)
        detail = client.get(f"/api/jobs/{job_id}").json()
        assert detail["status"] == "pending_review"
        assert len(detail["contents"]) == 3
        assert detail["events"]
        assert detail["result"]["metrics"]["elapsed_ms"] >= 0

        payload = {
            "revision": detail["revision"],
            "action": "approve",
            "note": "已核对商品资料与文案",
            "contents": detail["contents"],
        }
        approved = client.post(
            f"/api/jobs/{job_id}/review", headers={"X-CSRF-Token": admin_csrf}, json=payload
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"
        assert client.get(f"/api/jobs/{job_id}/export").status_code == 200

        member_client = TestClient(client.app)
        with member_client:
            member_csrf = login(member_client, "member", MEMBER_PASSWORD)
            assert member_client.get("/api/products").json() == []
            assert member_client.get(f"/api/jobs/{job_id}").status_code == 404
            assert member_client.get(f"/api/jobs/{job_id}/export").status_code == 404
            assert (
                member_client.post(
                    "/api/products",
                    headers={"X-CSRF-Token": member_csrf},
                    json={
                        "name": "成员产品",
                        "facts": "成员事实",
                        "source": "内部资料",
                    },
                ).status_code
                == 201
            )


def test_login_csrf_stale_revision_and_admin_controls(tmp_path):
    client, _db = make_client(tmp_path)
    with client:
        csrf = login(client)
        add_product(client, csrf)
        assert (
            client.post(
                "/api/products", json={"name": "CSRF", "facts": "x", "source": "y"}
            ).status_code
            == 403
        )
        created = client.post(
            "/api/users",
            headers={"X-CSRF-Token": csrf},
            json={"username": "new.member", "password": "a-secure-password-789"},
        )
        assert created.status_code == 201
        member = created.json()
        disabled = client.patch(
            f"/api/users/{member['id']}", headers={"X-CSRF-Token": csrf}, json={"active": False}
        )
        assert disabled.status_code == 200
        assert (
            client.post(
                "/api/login", json={"username": "new.member", "password": "a-secure-password-789"}
            ).status_code
            == 401
        )
        assert client.get("/api/jobs/not-a-job").status_code == 404
        # Password-changing route requires CSRF even when the session is valid.
        assert (
            client.post(
                "/api/password",
                json={
                    "current_password": ADMIN_PASSWORD,
                    "new_password": "brand-new-strong-password",
                },
            ).status_code
            == 403
        )
        changed = client.post(
            "/api/password",
            headers={"X-CSRF-Token": csrf},
            json={"current_password": ADMIN_PASSWORD, "new_password": "brand-new-strong-password"},
        )
        assert changed.status_code == 200
        assert client.get("/api/me").status_code == 401
        assert (
            client.post(
                "/api/login", json={"username": "admin", "password": "brand-new-strong-password"}
            ).status_code
            == 200
        )


def test_review_cannot_approve_rule_failure_or_stale_version(tmp_path):
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        product = add_product(client, csrf)
        job = client.post(
            "/api/jobs",
            headers={"X-CSRF-Token": csrf},
            json={"product_id": product["id"], "audience": "通勤用户"},
        ).json()
        claim = db.claim()
        execute_job(db.url, *claim)
        detail = client.get(f"/api/jobs/{job['id']}").json()
        bad = [*detail["contents"]]
        bad[0] = {**bad[0], "body": "这个产品100%有效"}
        payload = {
            "revision": detail["revision"],
            "action": "approve",
            "note": "尝试批准违规内容",
            "contents": bad,
        }
        rejected = client.post(
            f"/api/jobs/{job['id']}/review", headers={"X-CSRF-Token": csrf}, json=payload
        )
        assert rejected.status_code == 422
        payload["contents"] = detail["contents"]
        saved = client.post(
            f"/api/jobs/{job['id']}/review",
            headers={"X-CSRF-Token": csrf},
            json={**payload, "action": "save"},
        )
        assert saved.status_code == 200
        stale = client.post(
            f"/api/jobs/{job['id']}/review", headers={"X-CSRF-Token": csrf}, json=payload
        )
        assert stale.status_code == 409


def test_queue_claim_fences_competing_workers_and_recovers_expired_lease(tmp_path):
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        product = add_product(client, csrf)
        job = client.post(
            "/api/jobs",
            headers={"X-CSRF-Token": csrf},
            json={"product_id": product["id"], "audience": "上班族"},
        ).json()
        claim = db.claim(lease_seconds=1)
        assert claim and claim[0] == job["id"]
        assert db.claim(lease_seconds=1) is None
        with db.session() as s:
            record = s.get(Job, job["id"])
            record.lease_until = time.time() - 2
        next_claim = db.claim(lease_seconds=10)
        assert next_claim and next_claim[0] == job["id"]
        assert next_claim[1] != claim[1]
        assert not db.complete(job["id"], claim[1], {"contents": []})


def test_login_rate_limit_and_deactivated_user_sessions(tmp_path):
    client, _db = make_client(tmp_path)
    with client:
        admin_csrf = login(client)
        temporary = client.post(
            "/api/users",
            headers={"X-CSRF-Token": admin_csrf},
            json={"username": "temporary.member", "password": "a-secure-password-789"},
        )
        assert temporary.status_code == 201
        member_client = TestClient(client.app)
        with member_client:
            login(member_client, "temporary.member", "a-secure-password-789")
            disabled = client.patch(
                f"/api/users/{temporary.json()['id']}",
                headers={"X-CSRF-Token": admin_csrf},
                json={"active": False},
            )
            assert disabled.status_code == 200
            assert member_client.get("/api/me").status_code == 401
        for _ in range(5):
            client.post("/api/login", json={"username": "admin", "password": "wrong"})
        limited = client.post("/api/login", json={"username": "admin", "password": ADMIN_PASSWORD})
        assert limited.status_code == 429


def test_origin_body_limit_and_trusted_host_guards(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'guard.db'}",
        provider="mock",
        allowed_hosts=("testserver",),
    )
    db = Database(settings.database_url)
    db.initialize()
    client = TestClient(create_app(settings, db))
    with client:
        bad_origin = client.post(
            "/api/login",
            headers={"Origin": "https://evil.example"},
            json={"username": "admin", "password": "wrong"},
        )
        assert bad_origin.status_code == 403
        large = client.post(
            "/api/login", content=b"x" * 256_001, headers={"Content-Type": "application/json"}
        )
        assert large.status_code == 413
        bad_host = client.get("/health", headers={"Host": "evil.example"})
        assert bad_host.status_code == 400
        ok = client.get("/health")
        assert ok.status_code == 200
        assert ok.headers["x-content-type-options"] == "nosniff"
    db.engine.dispose()
