"""TikHub's documented Xiaohongshu App V2 endpoints; bounded, no retries."""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import httpx

BASE_URL = "https://api.tikhub.io"
SEARCH_PATH = "/api/v1/xiaohongshu/app_v2/search_notes"
DETAIL_PATH = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
NOTE_ID = re.compile(r"^[0-9a-fA-F]{24}$")


class SearchProviderError(ValueError):
    """Only fixed, safe messages may cross the API boundary."""


def _get_json(client, path, params, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SearchProviderError("小红书搜索超时，请稍后重试；不会自动重发")
    try:
        with client.stream("GET", path, params=params, timeout=min(7, remaining)) as response:
            if response.status_code in {401, 403}:
                raise SearchProviderError("TikHub 密钥无效或没有接口权限，请管理员检查配置")
            if response.status_code == 402:
                raise SearchProviderError("TikHub 额度不足，请管理员检查服务余额")
            if response.status_code == 429:
                raise SearchProviderError("TikHub 请求限流，请稍后再试；不会自动重发")
            response.raise_for_status()
            payload = bytearray()
            for chunk in response.iter_bytes():
                payload.extend(chunk)
                if len(payload) > 2_000_000:
                    raise SearchProviderError("小红书搜索服务返回的数据超过大小限制")
                if time.monotonic() > deadline:
                    raise SearchProviderError("小红书搜索超时，请稍后重试；不会自动重发")
        result = json.loads(payload)
    except httpx.TimeoutException:
        raise SearchProviderError("小红书搜索服务超时，请稍后重试；不会自动重发") from None
    except httpx.HTTPError:
        raise SearchProviderError("TikHub 小红书接口请求失败，请管理员检查服务状态") from None
    except (ValueError, TypeError) as exc:
        if isinstance(exc, SearchProviderError):
            raise
        raise SearchProviderError("TikHub 返回格式无效，未导入结果") from None
    if not isinstance(result, dict) or result.get("code") != 200 or "data" not in result:
        raise SearchProviderError("TikHub 未返回成功数据，请管理员核对密钥、额度和接口状态")
    data = result["data"]
    for _ in range(5):
        if not isinstance(data, dict):
            break
        if data.get("success") is False or ("code" in data and data["code"] not in (0, 200)):
            raise SearchProviderError("小红书上游数据不可用，未导入结果；服务请求可能已计费")
        if "data" not in data:
            break
        data = data["data"]
    if not isinstance(data, (dict, list)):
        raise SearchProviderError("TikHub 未返回可读取的小红书数据，未导入结果")
    return data


def _items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("notes", "items", "note_list"):
            if key in data:
                if not isinstance(data[key], list):
                    raise SearchProviderError("小红书笔记列表格式无效，未导入结果")
                return data[key]
    raise SearchProviderError("TikHub 返回的笔记结构尚不支持，请管理员检查适配")


def _card(item):
    if not isinstance(item, dict):
        return {}
    for key in ("note_card", "note"):
        if isinstance(item.get(key), dict):
            return {**item, **item[key]}
    return item


def _id(card):
    value = card.get("note_id") or card.get("id") or ""
    return value if isinstance(value, str) and NOTE_ID.fullmatch(value) else ""


def _read_detail(client, note_id, deadline):
    data = _get_json(client, DETAIL_PATH, {"note_id": note_id}, deadline)
    candidates = (
        [_card(item) for item in _items(data)]
        if (isinstance(data, list) or any(key in data for key in ("notes", "items", "note_list")))
        else [_card(data)]
    )
    candidates = [card for card in candidates if card]
    matches = [card for card in candidates if _id(card) == note_id]
    if (
        not matches
        and len(candidates) == 1
        and not (candidates[0].get("id") or candidates[0].get("note_id"))
    ):
        matches = candidates
    if len(matches) != 1:
        raise SearchProviderError("小红书正文与搜索笔记编号不匹配，未导入结果")
    note = matches[0]
    body = note.get("desc") or note.get("content") or note.get("body")
    title = note.get("title") or note.get("display_title") or "无标题笔记"
    if not isinstance(body, str) or not body.strip() or not isinstance(title, str):
        raise SearchProviderError("TikHub 未返回可用的笔记正文，未用搜索摘要代替正文")
    user = note.get("user") or note.get("author") or {}
    author = (
        user.get("nickname") or user.get("nick_name") or user.get("name") or ""
        if isinstance(user, dict)
        else user
    )
    if not isinstance(author, str):
        raise SearchProviderError("小红书作者字段格式无效，未导入结果")
    url = "https://www.xiaohongshu.com/explore/" + note_id
    token = note.get("xsec_token")
    if isinstance(token, str) and 0 < len(token) <= 1024:
        url += "?" + urlencode({"xsec_token": token, "xsec_source": "pc_search"})
    return {"title": title, "body": body, "author": author, "url": url}


def search_tikhub(data, api_key):
    if not api_key or any(char.isspace() for char in api_key):
        raise SearchProviderError("请管理员配置有效的 TIKHUB_API_KEY 后启用小红书自动搜索")
    deadline = time.monotonic() + 22
    with httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": "Bearer " + api_key},
        timeout=7,
        follow_redirects=False,
    ) as client:
        result = _get_json(
            client,
            SEARCH_PATH,
            {
                "keyword": data.keyword,
                "page": 1,
                "sort_type": data.sort_type,
                "note_type": data.note_type,
                "ai_mode": 0,
            },
            deadline,
        )
        entries = _items(result)
        ids = []
        for entry in entries[:100]:
            note_id = _id(_card(entry))
            if note_id and note_id not in ids:
                ids.append(note_id)
            if len(ids) >= min(data.limit, 5):
                break
        if entries and not ids:
            raise SearchProviderError("搜索结果未包含可读取的小红书笔记编号，未导入结果")
        # HTTPX Client is thread-safe. At most one search + five detail requests.
        with ThreadPoolExecutor(max_workers=3) as pool:
            return list(pool.map(lambda note_id: _read_detail(client, note_id, deadline), ids))
