import pytest


@pytest.fixture(autouse=True)
def isolate_search_credentials(monkeypatch):
    # Never consume the user's real search credentials in automated tests.
    monkeypatch.setenv("TIKHUB_API_KEY", "")
    monkeypatch.setenv("CONTENTPILOT_SEARCH_XIAOHONGSHU_PROVIDER", "auto")
    monkeypatch.setenv("CONTENTPILOT_SEARCH_XIAOHONGSHU_URL", "")
    monkeypatch.setenv("CONTENTPILOT_SEARCH_XIAOHONGSHU_TOKEN", "")
    from cryptography.fernet import Fernet

    monkeypatch.setenv("CONTENTPILOT_CONFIG_KEY", Fernet.generate_key().decode())
    for prefix in (
        "CONTENTPILOT_HOTSPOTS",
        "CONTENTPILOT_METRICS_XIAOHONGSHU",
        "CONTENTPILOT_METRICS_DOUYIN",
        "CONTENTPILOT_METRICS_WECHAT",
    ):
        monkeypatch.setenv(prefix + "_URL", "")
        monkeypatch.setenv(prefix + "_TOKEN", "")
