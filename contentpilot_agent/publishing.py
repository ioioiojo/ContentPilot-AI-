"""Explicit opt-in publishing gateway; no guessed social platform API endpoints."""

from urllib.parse import urlsplit

import httpx

from .services import service_env


def valid_https(url):
    if not isinstance(url, str) or any(c.isspace() for c in url):
        return False
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        return False


def gateway_config(platform):
    prefix = "CONTENTPILOT_PUBLISH_" + platform.upper()
    return service_env(prefix + "_URL", ""), service_env(prefix + "_TOKEN", "")


def publish(publication, attachments=None):
    url, token = gateway_config(publication.platform)
    if not valid_https(url) or not token:
        raise ValueError("该平台未配置 HTTPS 发布网关和授权令牌")
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": publication.id}
    payload = {
        "idempotency_key": publication.id,
        "platform": publication.platform,
        "content": publication.content,
        "source": publication.source,
        "revision": publication.revision,
    }
    if attachments:
        payload["assets"] = attachments
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    # An accepted/queued request is NOT proof that a platform post exists.
    if data.get("status") != "published" or not valid_https(data.get("url", "")):
        raise ValueError("网关未返回已发布状态及有效链接，请人工核对")
    return data["url"]
