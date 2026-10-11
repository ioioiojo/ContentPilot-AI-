"""Exercise a disposable staging deployment. Never use for production acceptance."""

import argparse
import os
import time

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    password = os.environ["SMOKE_PASSWORD"]
    if args.seed:
        from contentpilot_agent.config import Settings
        from contentpilot_agent.database import Database, User
        from contentpilot_agent.security import password_hash

        settings = Settings.from_env()
        if settings.app_env != "staging" or settings.provider != "mock":
            raise RuntimeError("Only a disposable Mock staging deployment is allowed")
        db = Database(settings.database_url)
        try:
            with db.session() as session:
                session.add(User(username="ci-admin", password=password_hash(password), admin=True))
        finally:
            db.engine.dispose()
        return

    with httpx.Client(base_url="http://127.0.0.1:18080", timeout=10) as client:
        client.get("/ready").raise_for_status()
        response = client.post("/api/login", json={"username": "ci-admin", "password": password})
        response.raise_for_status()
        assert "secure" in response.headers["set-cookie"].lower()
        # This tests the internal HTTP port, deliberately send the Secure cookie.
        # Real production acceptance must use an actual HTTPS browser session.
        client.headers["Cookie"] = "contentpilot_session=" + client.cookies["contentpilot_session"]
        client.headers["X-CSRF-Token"] = response.json()["csrf"]
        state = client.get("/api/admin/runtime")
        state.raise_for_status()
        assert state.json()["environment"] == "staging", "Refuse production writes"
        product = client.post(
            "/api/products",
            json={
                "name": "CI通勤杯",
                "facts": "容量300ml",
                "source": "CI测试说明书",
            },
        )
        product.raise_for_status()
        job = client.post(
            "/api/jobs",
            json={
                "product_id": product.json()["id"],
                "audience": "通勤用户",
                "platforms": ["xiaohongshu"],
            },
        )
        job.raise_for_status()
        job_id = job.json()["id"]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            response = client.get(f"/api/jobs/{job_id}")
            response.raise_for_status()
            detail = response.json()
            if detail["status"] == "pending_review":
                break
            assert detail["status"] != "failed", "Worker failed"
            time.sleep(1)
        else:
            raise RuntimeError("Worker did not finish in time")
        approved = client.post(
            f"/api/jobs/{job_id}/review",
            json={
                "revision": detail["revision"],
                "action": "approve",
                "note": "CI事实核对",
                "contents": detail["contents"],
            },
        )
        approved.raise_for_status()
        assert approved.json()["status"] == "approved"
        export = client.get(f"/api/jobs/{job_id}/export")
        export.raise_for_status()
        assert export.json()["contents"] == detail["contents"]
        client.get("/ready").raise_for_status()
    print("Staging: PostgreSQL, queue worker, review and export passed")


if __name__ == "__main__":
    main()
