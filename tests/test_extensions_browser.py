"""Browser coverage for settings, knowledge, delegated review and collection UI."""

import os
import socket
import time
from pathlib import Path
from threading import Thread

import pytest

playwright = pytest.importorskip("playwright.sync_api")
import uvicorn
from test_api import ADMIN_PASSWORD, MEMBER_PASSWORD, make_client

from contentpilot_agent.worker import execute_job


def browser_login(page, url, username, password):
    page.goto(url)
    page.locator("#login-form").get_by_label("用户名", exact=True).fill(username)
    page.locator("#login-form").get_by_label("密码", exact=True).fill(password)
    page.get_by_role("button", name="进入工作台").click()
    page.get_by_role("heading", name="任务工作台", exact=True).wait_for()


def test_admin_settings_knowledge_review_and_collection_ui(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "contentpilot_agent.services.bounded_json",
        lambda *args, **kwargs: {"data": [{"id": "test-image-model"}]},
    )
    client, db = make_client(tmp_path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(client.app, lifespan="on", log_level="error"))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    manager = playwright.sync_playwright().start()
    browser = manager.chromium.launch(headless=True, channel="chromium")
    errors = []
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        browser_login(page, url, "admin", ADMIN_PASSWORD)
        page.get_by_role("button", name="服务配置", exact=True).click()
        image = page.locator('form[data-service="image"]')
        image.get_by_label("接口地址", exact=True).fill("https://image.example/v1")
        image.get_by_label("图片模型", exact=True).fill("test-image-model")
        image.get_by_label("API 密钥", exact=True).fill("browser-test-secret-only")
        image.get_by_label("启用此服务", exact=True).check()
        image.get_by_role("button", name="保存配置", exact=True).click()
        image.get_by_role("button", name="测试连接", exact=True).wait_for()
        playwright.expect(image.get_by_label("API 密钥", exact=True)).to_have_value("")
        assert "browser-test-secret-only" not in page.inner_text("body")
        image.get_by_role("button", name="测试连接", exact=True).click()
        image.get_by_text("测试通过", exact=False).wait_for()
        if os.getenv("CONTENTPILOT_SCREENSHOTS"):
            out = Path(os.environ["CONTENTPILOT_SCREENSHOTS"]) / "extensions"
            out.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out / "services.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(out / "services-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1280, "height": 900})
        page.get_by_role("button", name="知识库与热点", exact=True).click()
        page.get_by_label("资料标题", exact=True).fill("通勤咖啡知识")
        page.get_by_label("资料正文", exact=True).fill("按自己的通勤安排了解咖啡规格与携带方式。")
        page.get_by_label("知识资料来源", exact=True).fill("测试说明书 第 2 页")
        page.get_by_role("button", name="保存知识资料", exact=True).click()
        page.get_by_role("heading", name="通勤咖啡知识", exact=True).wait_for()
        page.get_by_label("知识与热点关键词", exact=True).fill("咖啡")
        page.get_by_role("button", name="检索知识库", exact=True).click()
        page.get_by_text("命中 1 条", exact=True).wait_for()
        assert page.get_by_role("button", name="获取小红书实时热点", exact=True).is_disabled()
        if os.getenv("CONTENTPILOT_SCREENSHOTS"):
            page.screenshot(path=str(out / "knowledge.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(out / "knowledge-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1280, "height": 900})
        page.get_by_role("button", name="成员管理", exact=True).click()
        member = page.locator("#users-list .job-row").filter(has_text="member")
        member.get_by_role("button", name="启用审稿资格", exact=True).click()
        member.get_by_role("button", name="撤销审稿资格", exact=True).wait_for()
        page.get_by_role("button", name="商品资料", exact=True).click()
        page.get_by_role("button", name="添加商品").click()
        page.get_by_label("商品名称", exact=True).fill("通勤咖啡")
        page.get_by_label("商品事实", exact=True).fill("便携，低糖")
        page.get_by_label("资料来源", exact=True).fill("测试商品说明书")
        page.get_by_role("button", name="保存资料", exact=True).click()
        page.get_by_role("button", name="任务工作台", exact=True).click()
        page.get_by_role("button", name="新建任务", exact=False).click()
        page.get_by_label("目标用户", exact=True).fill("通勤上班族")
        page.get_by_label("总内容条数", exact=True).fill("1")
        page.get_by_role("button", name="提交后台生成", exact=True).click()
        page.locator("#detail-dialog").get_by_text("排队中", exact=True).wait_for()
        execute_job(db.url, *db.claim())
        page.locator("#detail-dialog").get_by_text("待人工审核", exact=True).wait_for(timeout=15000)
        page.get_by_label("选择审稿人", exact=True).select_option(label="member")
        page.get_by_role("button", name="保存审核分配", exact=True).click()
        page.get_by_text("审核分配已保存", exact=True).wait_for()
        assert page.get_by_role("button", name="审核通过", exact=True).is_disabled()
        reviewer_context = browser.new_context()
        reviewer = reviewer_context.new_page()
        reviewer.on("pageerror", lambda error: errors.append(str(error)))
        browser_login(reviewer, url, "member", MEMBER_PASSWORD)
        assert reviewer.locator("#services-nav").is_hidden()
        reviewer.get_by_role("button", name="审核中心", exact=True).click()
        reviewer.get_by_role("button", name="打开分配任务", exact=True).click()
        reviewer.get_by_label("审核说明（必填）", exact=True).fill("独立核对商品说明和知识来源")
        reviewer.get_by_role("button", name="审核通过", exact=True).click()
        reviewer.locator("#detail-dialog").get_by_text("已通过", exact=True).wait_for()
        assert reviewer.get_by_role("link", name="下载已审核内容（JSON）", exact=True).count() == 0
        reviewer_context.close()
        page.locator("#detail-dialog .close-dialog").click()
        page.get_by_role("button", name="查看任务", exact=True).click()
        page.locator("#detail-dialog").get_by_text("已通过", exact=True).wait_for()
        page.get_by_role("button", name="登记 / 发布内容", exact=True).click()
        page.get_by_label("平台发布链接", exact=True).fill(
            "https://www.xiaohongshu.com/explore/697c0eee000000000a03c308"
        )
        page.get_by_role("button", name="提交发布记录", exact=True).click()
        page.get_by_role("button", name="自动采集", exact=True).click()
        dialog = page.locator("#collection-dialog")
        dialog.get_by_text("采集服务未配置", exact=False).wait_for()
        assert dialog.get_by_role("button", name="立即采集", exact=True).is_disabled()
        dialog.get_by_role("button", name="关闭", exact=True).click()
        page.locator("#detail-dialog .close-dialog").click()
        page.get_by_role("button", name="退出登录", exact=True).click()
        page.locator("#login-screen").wait_for(state="visible")
        assert not page.locator("#view-services form").count()
        assert not errors, errors
    finally:
        server.should_exit = True
        thread.join(10)
        browser.close()
        manager.stop()
        db.engine.dispose()
