"""Real-browser smoke test; skips cleanly if the Playwright browser is not installed."""

import os
import socket
import time
from pathlib import Path
from threading import Thread

import httpx
import pytest

playwright = pytest.importorskip("playwright.sync_api")
import uvicorn
from test_tikhub import NOTE_ID, mock_provider

from contentpilot_agent.api import create_app
from contentpilot_agent.config import Settings
from contentpilot_agent.database import Database, User
from contentpilot_agent.security import password_hash
from contentpilot_agent.tikhub import DETAIL_PATH, SEARCH_PATH
from contentpilot_agent.worker import run_once


@pytest.mark.parametrize("automatic_search", [False, True])
def test_operator_can_login_save_product_and_queue_job(tmp_path, monkeypatch, automatic_search):
    calls = []
    if automatic_search:

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == SEARCH_PATH:
                assert request.url.params["keyword"] == "通勤咖啡"
                assert request.url.params["sort_type"] == "collect_descending"
                assert request.url.params["note_type"] == "普通笔记"
                return httpx.Response(200, json={"code": 200, "data": {"items": [{"id": NOTE_ID}]}})
            assert request.url.path == DETAIL_PATH
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "id": NOTE_ID,
                        "title": "早晨的咖啡选择思路",
                        "desc": "根据出门时间和个人需求安排早餐，了解容量和携带方式再做选择。",
                        "user": {"nickname": "测试参考作者"},
                    },
                },
            )

        mock_provider(monkeypatch, handler)
    try:
        manager = playwright.sync_playwright().start()
        browser = manager.chromium.launch(headless=True, channel="chromium")
    except playwright.Error as exc:
        manager.stop()
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Run python -m playwright install chromium first")
        raise

    password = "browser-test-password-123"
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'browser.db'}", cookie_secure=False, provider="mock"
    )
    db = Database(settings.database_url)
    db.initialize()
    with db.session() as session:
        session.add(User(username="browser", password=password_hash(password), admin=True))
    app = create_app(settings, db)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    page_errors = []
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        page = browser.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{port}/")
        if os.getenv("CONTENTPILOT_SCREENSHOTS"):
            out = Path(os.environ["CONTENTPILOT_SCREENSHOTS"])
            if automatic_search:
                out = out / "tikhub"
            out.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out / "login.png"), full_page=True)
        page.locator("#login-form").get_by_label("用户名", exact=True).fill("browser")
        page.locator("#login-form").get_by_label("密码", exact=True).fill(password)
        page.get_by_role("button", name="进入工作台").click()
        page.get_by_role("heading", name="任务工作台").wait_for()
        page.get_by_role("button", name="商品资料", exact=True).click()
        page.get_by_role("button", name="添加商品").click()
        page.get_by_label("商品名称").fill("浏览器试用咖啡")
        page.get_by_label("商品事实").fill("低糖，方便携带")
        page.get_by_label("资料来源").fill("说明书第 1 页")
        page.get_by_role("button", name="保存资料").click()
        page.get_by_text("浏览器试用咖啡").wait_for()
        page.get_by_role("button", name="参考帖二创", exact=True).click()
        if not automatic_search:
            page.get_by_text("自动搜索未接通。", exact=False).wait_for()
            assert page.get_by_role("button", name="搜索并导入", exact=True).is_disabled()
        page.get_by_label("搜索关键词", exact=True).fill("通勤咖啡")
        search_link = page.get_by_role("link", name="去小红书搜索")
        assert "%E9%80%9A%E5%8B%A4" in search_link.get_attribute("href")
        if automatic_search:
            page.locator("#reference-search-form [name=limit]").select_option("1")
            page.locator("#reference-search-form [name=sort_type]").select_option(
                "collect_descending"
            )
            page.locator("#reference-search-form [name=note_type]").select_option("普通笔记")
            page.get_by_text("本次最多 1 次搜索 + 1 次正文读取", exact=False).wait_for()
            page.get_by_role("button", name="搜索并导入", exact=True).click()
            page.get_by_text("小红书数据服务 · TikHub", exact=False).wait_for()
            assert calls == [SEARCH_PATH, DETAIL_PATH]
        else:
            page.get_by_role("button", name="手动录入参考帖", exact=True).click()
            page.get_by_label("笔记链接", exact=True).fill(
                "https://www.xiaohongshu.com/explore/browser-reference"
            )
            page.get_by_label("参考标题", exact=True).fill("早晨的咖啡选择思路")
            page.get_by_label("作者（可选）", exact=True).fill("测试参考作者")
            page.get_by_label("参考正文", exact=True).fill(
                "根据出门时间和个人需求安排早餐，了解容量和携带方式再做选择。"
            )
            page.get_by_role("button", name="保存参考帖", exact=True).click()
        page.get_by_label("选择参考帖：早晨的咖啡选择思路", exact=True).check()
        page.get_by_text("已选 1 / 5 条", exact=True).wait_for()
        if os.getenv("CONTENTPILOT_SCREENSHOTS"):
            page.locator("#toast").evaluate("node => node.hidden = true")
            page.screenshot(path=str(out / "references.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(out / "references-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.set_viewport_size({"width": 1280, "height": 720})
        page.get_by_role("button", name="用所选参考帖创建二创", exact=True).click()
        page.locator("#job-dialog").wait_for(state="visible")
        assert page.locator("#job-reference-list input:checked").count() == 1
        page.get_by_label("二创要求（可选）", exact=True).fill(
            "重新组织为新手选购指南，保留真实商品事实"
        )
        page.get_by_label("目标用户").fill("上班族")
        page.get_by_role("button", name="提交后台生成").click()
        page.locator("#detail-dialog").get_by_text("排队中", exact=True).wait_for()
        assert run_once(db, settings)
        page.locator("#detail-dialog").get_by_text("待人工审核", exact=True).wait_for(timeout=15000)
        page.locator(".content-editor").first.locator("[name=title]").fill("通勤咖啡的日常选择")
        tabs = page.get_by_role("tab")
        tabs.nth(1).click()
        page.locator(".content-editor").nth(1).locator("[name=title]").fill("通勤咖啡的使用指南")
        tabs.nth(1).press("ArrowLeft")
        assert tabs.first.get_attribute("aria-selected") == "true"
        assert (
            page.locator(".content-editor").first.locator("[name=title]").input_value()
            == "通勤咖啡的日常选择"
        )
        page.get_by_label("审核说明（必填）").fill("已核对商品来源和编辑稿")
        if os.getenv("CONTENTPILOT_SCREENSHOTS"):
            page.locator("#detail-dialog").evaluate("(node) => node.scrollTop = 0")
            page.screenshot(path=str(out / "editing.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#detail-dialog").evaluate("(node) => node.scrollTop = 0")
            page.screenshot(path=str(out / "editing-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.set_viewport_size({"width": 1280, "height": 720})
        page.get_by_role("button", name="审核通过", exact=True).click()
        page.locator("#detail-dialog").get_by_text("已通过", exact=True).wait_for()
        with page.expect_download() as download:
            page.get_by_role("link", name="下载已审核内容（JSON）").click()
        assert download.value.suggested_filename.endswith(".json")
        import json

        exported = json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
        assert exported["contents"][0]["title"] == "通勤咖啡的日常选择"
        assert exported["contents"][1]["title"] == "通勤咖啡的使用指南"
        assert exported["mode"] == "mock"
        assert exported["references"][0]["title"] == "早晨的咖啡选择思路"
        assert exported["references"][0]["source_type"] == (
            "tikhub" if automatic_search else "manual"
        )
        assert exported["rewrite_guidance"] == "重新组织为新手选购指南，保留真实商品事实"
        page.get_by_role("button", name="人工质量标注", exact=True).click()
        page.get_by_label("标注依据", exact=True).fill("已核对测试说明书")
        page.get_by_role("button", name="保存人工评测", exact=True).click()
        page.locator("#detail-dialog").get_by_text("人工评测 通过", exact=False).wait_for()
        page.get_by_role("button", name="登记 / 发布内容", exact=True).click()
        page.get_by_label("平台发布链接", exact=True).fill(
            "https://www.xiaohongshu.com/explore/test"
        )
        page.get_by_label("对比组名称", exact=True).fill("通勤标题")
        page.get_by_role("button", name="提交发布记录", exact=True).click()
        page.get_by_role("button", name="录入效果", exact=True).click()
        for label, value in [
            ("累计曝光数", "1000"),
            ("累计点击数", "100"),
            ("累计成交数", "10"),
            ("累计营收（元）", "790"),
            ("累计总成本（元）", "100"),
            ("数据依据", "浏览器测试报表"),
        ]:
            page.get_by_label(label, exact=True).fill(value)
        page.get_by_role("button", name="保存效果快照", exact=True).click()
        page.locator("#detail-dialog").get_by_text("CTR 10.00%", exact=False).wait_for()
        page.locator("#detail-dialog .close-dialog").click()
        page.get_by_role("button", name="发布与效果", exact=True).click()
        page.locator("#growth-publications").get_by_text(
            "通勤咖啡的日常选择", exact=True
        ).wait_for()
        page.locator("#growth-evaluation").get_by_text("通过率 100.00%", exact=False).wait_for()
        with page.expect_download() as labels_download:
            page.get_by_role("link", name="导出人工标注集（JSONL）").click()
        labels = Path(labels_download.value.path()).read_text(encoding="utf-8")
        assert "已核对测试说明书" in labels
        if os.getenv("CONTENTPILOT_SCREENSHOTS"):
            out = Path(os.environ["CONTENTPILOT_SCREENSHOTS"])
            out.mkdir(parents=True, exist_ok=True)
            page.evaluate("window.scrollTo(0,0)")
            page.locator("#toast").evaluate("node => node.hidden = true")
            page.screenshot(path=str(out / "growth.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("window.scrollTo(0,0)")
            layout = page.evaluate("""() => ({
                asideTop: document.querySelector('aside').getBoundingClientRect().top,
                workspaceTop: document.querySelector('#workspace').getBoundingClientRect().top,
                bodyMargin: getComputedStyle(document.body).margin,
                sidebarMargin: getComputedStyle(document.querySelector('.sidebar-bottom')).margin,
                navMargin: getComputedStyle(document.querySelector('nav')).margin
            })""")
            assert layout["asideTop"] == 0, layout
            page.screenshot(path=str(out / "growth-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.set_viewport_size({"width": 1280, "height": 720})
        page.get_by_role("button", name="任务工作台", exact=True).click()
        page.get_by_label("搜索本页任务", exact=True).fill("没有这个商品")
        page.get_by_text("没有匹配的任务", exact=True).wait_for()
        page.get_by_label("搜索本页任务", exact=True).fill("")
        page.get_by_label("筛选本页任务状态", exact=True).select_option("pending_review")
        page.get_by_text("没有匹配的任务", exact=True).wait_for()
        page.get_by_label("筛选本页任务状态", exact=True).select_option("approved")
        page.get_by_role("button", name="查看任务", exact=True).click()
        if os.getenv("CONTENTPILOT_SCREENSHOTS"):
            out = Path(os.environ["CONTENTPILOT_SCREENSHOTS"])
            out.mkdir(parents=True, exist_ok=True)
            page.locator("#detail-dialog").evaluate("(node) => node.scrollTop = 0")
            page.screenshot(path=str(out / "review.png"), full_page=True)
            page.locator("#detail-dialog .close-dialog").click()
            page.evaluate("window.scrollTo(0,0)")
            page.screenshot(path=str(out / "dashboard.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("window.scrollTo(0,0)")
            page.screenshot(path=str(out / "mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not page_errors, page_errors
        page.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        browser.close()
        manager.stop()
        db.engine.dispose()
