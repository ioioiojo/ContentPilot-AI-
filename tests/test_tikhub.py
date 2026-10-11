import json
import subprocess
import sys
import time

import httpx
import pytest
from sqlalchemy import select
from test_api import add_product, login, make_client

from contentpilot_agent.database import ReferenceSearch
from contentpilot_agent.models import ReferenceSnapshot
from contentpilot_agent.references import SearchInput, search_notes, search_service
from contentpilot_agent.tikhub import DETAIL_PATH, SEARCH_PATH, SearchProviderError
from contentpilot_agent.worker import execute_job

NOTE_ID = "697c0eee000000000a03c308"
OTHER_ID = "697c0eee000000000a03c309"


def mock_provider(monkeypatch, handler):
    monkeypatch.setenv("CONTENTPILOT_SEARCH_XIAOHONGSHU_PROVIDER", "tikhub")
    monkeypatch.setenv("TIKHUB_API_KEY", "test-token-not-real")
    original = httpx.Client
    monkeypatch.setattr(
        "contentpilot_agent.tikhub.httpx.Client",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )


def test_search_and_detail_http_contract_body_source_and_remix(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.method == "GET"
        assert request.url.host == "api.tikhub.io"
        assert request.headers["authorization"] == "Bearer test-token-not-real"
        if request.url.path == SEARCH_PATH:
            assert request.url.params["keyword"] == "通勤咖啡"
            assert request.url.params["sort_type"] == "collect_descending"
            assert request.url.params["note_type"] == "普通笔记"
            assert request.url.params["page"] == "1"
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "id": NOTE_ID,
                                    "note_card": {
                                        "display_title": "搜索摘要标题",
                                        "desc": "摘要不能代替正文",
                                    },
                                },
                                {"id": NOTE_ID},
                                {"id": "advertisement"},
                            ]
                        },
                    },
                },
            )
        assert request.url.path == DETAIL_PATH
        assert request.url.params["note_id"] == NOTE_ID
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "success": True,
                    "data": {
                        "note_list": [
                            {
                                "id": NOTE_ID,
                                "title": "通勤咖啡选择指南",
                                "desc": "根据自己的早晨安排了解饮品规格，选择方便携带的日常用品。",
                                "user": {"nickname": "参考作者"},
                                "xsec_token": "sample-access-value",
                            }
                        ]
                    },
                },
            },
        )

    mock_provider(monkeypatch, handler)
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        headers = {"X-CSRF-Token": csrf}
        capabilities = client.get("/api/capabilities").json()
        assert capabilities["reference_search_service"] == {
            "provider": "tikhub",
            "configured": True,
            "max_results": 5,
        }
        assert "test-token-not-real" not in json.dumps(capabilities)
        response = client.post(
            "/api/references/search",
            headers=headers,
            json={
                "keyword": "通勤咖啡",
                "limit": 3,
                "sort_type": "collect_descending",
            },
        )
        assert response.status_code == 200, response.text
        assert calls == [SEARCH_PATH, DETAIL_PATH]
        note = response.json()["posts"][0]
        assert response.json()["source"] == "tikhub"
        assert note["source_type"] == "tikhub"
        assert note["title"] == "通勤咖啡选择指南"
        assert note["body"] != "摘要不能代替正文"
        assert note["author"] == "参考作者"
        assert NOTE_ID in note["url"]
        ReferenceSnapshot.model_validate(note)
        product = add_product(client, csrf)
        job = client.post(
            "/api/jobs",
            headers=headers,
            json={
                "product_id": product["id"],
                "audience": "上班族",
                "content_count": 1,
                "platforms": ["xiaohongshu"],
                "reference_ids": [note["id"]],
            },
        )
        assert job.status_code == 202
        execute_job(db.url, *db.claim())
        detail = client.get("/api/jobs/" + job.json()["id"]).json()
        assert detail["status"] == "pending_review"
        assert detail["request"]["references"][0]["source_type"] == "tikhub"
        assert detail["result"]["metrics"]["reference_samples"] == 1
    db.engine.dispose()


@pytest.mark.parametrize("shape", ["notes", "note_list", "items"])
def test_known_search_shapes_and_id_deduplication(monkeypatch, shape):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == SEARCH_PATH:
            return httpx.Response(
                200, json={"code": 200, "data": {shape: [{"note_id": NOTE_ID}] * 2}}
            )
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": [
                    {
                        "note": {
                            "note_id": NOTE_ID,
                            "desc": "真实详情正文",
                            "title": "参考标题",
                        }
                    }
                ],
            },
        )

    mock_provider(monkeypatch, handler)
    assert len(search_notes(SearchInput(keyword="咖啡"), "request")) == 1
    assert calls == [SEARCH_PATH, DETAIL_PATH]


def test_empty_search_does_not_request_detail(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"code": 200, "data": {"notes": []}})

    mock_provider(monkeypatch, handler)
    assert search_notes(SearchInput(keyword="空结果"), "request") == []
    assert calls == [SEARCH_PATH]


@pytest.mark.parametrize("status", [401, 403, 402, 429, 500])
def test_provider_http_errors_are_sanitized_without_retry(tmp_path, monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, text="test-token-not-real raw-secret-provider-error")

    mock_provider(monkeypatch, handler)
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        result = client.post(
            "/api/references/search", headers={"X-CSRF-Token": csrf}, json={"keyword": "咖啡"}
        )
        assert result.status_code == 502
        assert "test-token-not-real" not in result.text and "raw-secret" not in result.text
        assert calls == [SEARCH_PATH]
        assert client.get("/api/references").json() == []
        with db.session() as session:
            assert session.scalar(select(ReferenceSearch)).status == "failed"
    db.engine.dispose()


@pytest.mark.parametrize(
    "detail",
    [
        {"id": NOTE_ID, "title": "只有标题没有正文"},
        {"id": OTHER_ID, "title": "另一帖子", "desc": "别的帖子正文"},
        {"id": "bad-id", "desc": "不能当作缺少ID放行"},
    ],
)
def test_incomplete_or_mismatched_detail_is_not_replaced_by_summary(monkeypatch, detail):
    def handler(request):
        if request.url.path == SEARCH_PATH:
            return httpx.Response(
                200, json={"code": 200, "data": {"notes": [{"id": NOTE_ID, "desc": "这是摘要"}]}}
            )
        return httpx.Response(200, json={"code": 200, "data": detail})

    mock_provider(monkeypatch, handler)
    with pytest.raises(SearchProviderError):
        search_notes(SearchInput(keyword="咖啡"), "request")


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 200, "data": None},
        {"code": 200, "data": {"success": False}},
        {"code": 200, "data": {"code": -1}},
        {"code": 200, "data": {"unrecognized": []}},
        {"code": 200, "data": {"notes": [{"id": "advertisement"}]}},
        {"code": 402},
    ],
)
def test_upstream_failure_and_unknown_structure_are_not_empty_success(monkeypatch, payload):
    mock_provider(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(SearchProviderError):
        search_notes(SearchInput(keyword="咖啡"), "request")


def test_missing_key_limits_and_no_fallback_to_other_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENTPILOT_SEARCH_XIAOHONGSHU_PROVIDER", "tikhub")
    monkeypatch.setenv("CONTENTPILOT_SEARCH_XIAOHONGSHU_TOKEN", "gateway-token")
    assert search_service()["configured"] is False
    client, db = make_client(tmp_path)
    with client:
        csrf = login(client)
        headers = {"X-CSRF-Token": csrf}
        missing = client.post("/api/references/search", headers=headers, json={"keyword": "咖啡"})
        assert missing.status_code == 409 and "TIKHUB_API_KEY" in missing.text
        monkeypatch.setenv("TIKHUB_API_KEY", "test-token-not-real")
        assert (
            client.post(
                "/api/references/search", headers=headers, json={"keyword": "咖啡", "limit": 6}
            ).status_code
            == 422
        )
        with db.session() as session:
            assert session.scalar(select(ReferenceSearch)) is None
    db.engine.dispose()


def test_detail_request_deadline_and_size_limits(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"x" * 2_000_001)

    mock_provider(monkeypatch, handler)
    with pytest.raises(SearchProviderError, match="大小限制"):
        search_notes(SearchInput(keyword="咖啡"), "request")
    from contentpilot_agent.tikhub import _get_json

    with pytest.raises(SearchProviderError, match="超时"):
        _get_json(None, SEARCH_PATH, {}, time.monotonic() - 1)


def test_key_configuration_preserves_existing_env_without_printing_key(tmp_path):
    # Invoke the interactive helper with a fake getpass; no actual credentials or network.
    path = tmp_path / ".env"
    path.write_text(
        "OPENAI_API_KEY=keep-test-only\nDATABASE_URL=sqlite:///keep.db\n", encoding="utf-8"
    )
    script = "from unittest.mock import patch; from contentpilot_agent.manage import main; import sys; sys.argv=['manage','configure-tikhub']; task_patch=patch('getpass.getpass',return_value='test-key-hidden'); task_patch.start(); main()"
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, timeout=15, check=False
    )
    assert result.returncode == 0, result.stderr
    assert b"test-key-hidden" not in result.stdout and b"test-key-hidden" not in result.stderr
    from dotenv import dotenv_values

    values = dotenv_values(path)
    assert values["OPENAI_API_KEY"] == "keep-test-only"
    assert values["DATABASE_URL"] == "sqlite:///keep.db"
    assert values["TIKHUB_API_KEY"] == "test-key-hidden"
    assert values["CONTENTPILOT_SEARCH_XIAOHONGSHU_PROVIDER"] == "tikhub"
