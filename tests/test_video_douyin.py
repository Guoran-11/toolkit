"""抖音真实链接解析（匿名访客 Cookie ``ttwid`` 重试链路）的离线测试。

背景（2026-10-21 生产实测）：``https://www.iesdouyin.com/share/video/{id}/`` 对
普通 UA（含 ``net.UA_MOBILE``）已不再内嵌作品 JSON——``window._ROUTER_DATA`` 的
``loaderData["video_(id)/page"]`` 里只剩 ``ua`` / ``isSpider`` / ``itemId``；同一
URL 只要带上匿名访客 Cookie ``ttwid``，该 page 里就会出现
``videoInfoRes.item_list``。本文件锁定这条降级链的行为：

    1. 夹具（``share_page_video_info_res.html``）里的 ``videoInfoRes.item_list``
       能被 ``parse_fixture`` 解析；
    2. ``ADAPTER.parse`` 在无数据页上带 ``ttwid`` 重试并解析成功；
    3. ``ttwid`` 获取失败时不额外抓取页面、仍抛 ``PARSE_FAILED``；
    4. ``ItemNotFoundError`` 不被重试掩盖；
    5. 重试仍失败时错误文案提到「访客 Cookie」且不泄漏 Cookie 值；
    6. 已配置的用户 Cookie 优先于自动申请的 ``ttwid``；
    7. ``ttwid`` 进程内缓存生效；
    8. ``net.http_post_json`` 自身：POST + JSON 体 + 逐跳校验 + 暴露 ``Set-Cookie``。

**完全离线**：所有用例都把 ``net.http_get_text`` / ``net.http_post_json`` 换成
桩函数，同时把 ``urllib.request.urlopen``、``net._opener``、``socket.getaddrinfo``
换成「被调用即失败」的探针，并在用例末尾断言探针记录为空。
"""

from __future__ import annotations

import socket
import urllib.request
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "video" / "douyin"

#: 被测作品长链（www.douyin.com 不是短链域，``normalize`` 不会触网）
SHARE_URL = "https://www.douyin.com/video/7412345678901234567"
AWEME_ID = "7412345678901234567"
FIXTURE_NAME = "share_page_video_info_res.html"

#: 无 ``ttwid`` 时的真实分享页形态：只有 ua/isSpider/itemId，没有任何作品数据。
_NO_DATA_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>抖音</title></head>
<body><div id="root"></div>
<script>
window._ROUTER_DATA = {
  "loaderData": {
    "video_(id)/page": {
      "isSpider": false,
      "itemId": "7412345678901234567",
      "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"
    }
  },
  "errors": null
};
</script>
</body>
</html>
"""

#: 失效作品的真实形态：``item_list`` 为空 + ``filter_list`` 给 SYSTEM_ITEM_NOT_EXIST。
_ITEM_NOT_EXIST_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>作品不存在 - 抖音</title></head>
<body><div id="root"></div>
<script>
window._ROUTER_DATA = {
  "loaderData": {
    "video_(id)/page": {
      "videoInfoRes": {
        "filter_list": [
          {
            "aweme_id": "7400000000000000000",
            "filter_reason": "SYSTEM_ITEM_NOT_EXIST",
            "detail_msg": "作品不存在或已删除"
          }
        ],
        "item_list": [],
        "status_code": 0
      },
      "isSpider": false,
      "itemId": "7400000000000000000"
    }
  },
  "errors": null
};
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 通用工具


def _no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把全部出站入口换成「被调用即失败」的探针（思路同 ``tests/test_video.py``）。

    覆盖三层：``urllib.request.urlopen``（标准库直连）、``net._opener``（net.py
    手动跟随重定向，绕开了 urlopen）、``socket.getaddrinfo``（DNS 也属出站行为）。

    Args:
        monkeypatch: pytest 的补丁夹具。

    Returns:
        调用记录列表；用例末尾断言其为空即证明未触网。
    """
    calls: list[str] = []

    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003 - 测试探针
        calls.append(str(args[0] if args else kwargs))
        raise AssertionError(f"测试不应发起网络请求：{args!r}")

    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)

    from app.modules.video import net

    class _NoopOpener:
        """替身 Opener：任何 open 调用都记为一次违规网络请求。"""

        def open(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return _forbidden(*args, **kwargs)

    monkeypatch.setattr(net, "_opener", _NoopOpener())
    return calls


def _fixture_html(name: str = FIXTURE_NAME) -> str:
    """读取抖音夹具页面。

    Args:
        name: ``tests/fixtures/video/douyin/`` 下的文件名。

    Returns:
        页面文本。
    """
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse_ctx(cookie: str | None = None):
    """构造离线解析上下文。

    Args:
        cookie: 用户配置的登录 Cookie（默认无）。

    Returns:
        ``ParseContext`` 实例。
    """
    from app.modules.video.adapters.base import ParseContext

    return ParseContext(platform="douyin", cookie=cookie, prefer_quality="1080P", timeout=1.0)


def _stub_pages(monkeypatch: pytest.MonkeyPatch, pages: list[str]) -> list[dict]:
    """替换 ``net.http_get_text``：按调用次序依次返回 ``pages`` 中的页面。

    调用次数超过 ``pages`` 长度时重复最后一页，便于暴露「意外的额外抓取」。

    Args:
        monkeypatch: pytest 的补丁夹具。
        pages: 依次返回的页面文本。

    Returns:
        调用记录（每项含 ``url`` 与本次请求头副本）。
    """
    from app.modules.video import net

    calls: list[dict] = []

    def _fake_get(url: str, **kwargs: object) -> tuple[str, str, int]:
        calls.append({"url": url, "headers": dict(kwargs.get("headers") or {})})
        index = min(len(calls) - 1, len(pages) - 1)
        return url, pages[index], 200

    monkeypatch.setattr(net, "http_get_text", _fake_get)
    return calls


def _stub_ttwid(
    monkeypatch: pytest.MonkeyPatch,
    token: str = "test-token",
    *,
    calls: list[dict] | None = None,
) -> list[dict]:
    """替换 ``net.http_post_json``：模拟访客注册接口返回带 ``Set-Cookie`` 的响应。

    Args:
        monkeypatch: pytest 的补丁夹具。
        token: 期望出现在 ``ttwid=`` 后面的值。
        calls: 可选的调用记录列表（由调用方断言次数）。

    Returns:
        调用记录列表。
    """
    from app.modules.video import net

    recorded: list[dict] = calls if calls is not None else []

    def _fake_post(url: str, **kwargs: object) -> tuple[str, object, int, dict[str, str]]:
        recorded.append({"url": url, "kwargs": kwargs})
        set_cookie = (
            f"ttwid={token}; Path=/; Domain=bytedance.com; "
            "Max-Age=31536000; HttpOnly; Secure"
        )
        return url, {"status_code": 0}, 200, {"set-cookie": set_cookie}

    monkeypatch.setattr(net, "http_post_json", _fake_post)
    return recorded


def _stub_ytdlp(monkeypatch: pytest.MonkeyPatch, result=None) -> list[str]:
    """替换 ``ytdlp.parse``，避免测试机上真的拉起外部进程（也避免联网）。

    Args:
        monkeypatch: pytest 的补丁夹具。
        result: 桩函数返回值（默认 None 表示「兜底也不可用」）。

    Returns:
        调用记录（每次兜底的 URL）。
    """
    from app.modules.video.adapters import ytdlp

    calls: list[str] = []

    def _fake_parse(url, _platform, _ctx):
        calls.append(url)
        return result

    monkeypatch.setattr(ytdlp, "parse", _fake_parse)
    return calls


@pytest.fixture(autouse=True)
def _clear_ttwid_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空模块级 ttwid 缓存，保证用例之间互不影响（结束后自动还原）。"""
    from app.modules.video.adapters import douyin

    monkeypatch.setattr(douyin, "_ttwid_cache", None)


# ---------------------------------------------------------------- 1. 夹具解析


class TestFixtureParse:
    """``videoInfoRes.item_list`` 的离线解析（带 ttwid 时的新页面结构）。"""

    def test_parse_fixture_from_video_info_res(self) -> None:
        """``parse_fixture`` 能从 ``videoInfoRes.item_list`` 解析出完整作品信息。"""
        from app.modules.video.adapters.douyin import parse_fixture

        item = parse_fixture(_fixture_html(), SHARE_URL)

        assert item.platform == "douyin"
        assert item.item_id == AWEME_ID
        assert item.url == f"https://www.douyin.com/video/{AWEME_ID}"
        assert item.media_type == "video"
        assert item.parse_source == "html"

        assert item.title == "晚风里的一百种蓝 #风景 #自造样本"
        assert item.author.name == "示例作者"
        assert item.author.id == "MS4wLjABAAAAexamplesecuid"
        assert item.author.avatar_url is not None
        assert item.author.avatar_url.startswith("https://")
        assert item.cover_url is not None and item.cover_url.startswith("https://")
        assert item.duration_sec == 151  # 151000ms -> 151s
        assert item.published_at is not None and item.published_at.startswith("2025")
        assert item.stats == {"like": 1200, "comment": 34, "share": 12, "play": 56000}
        assert item.images == []

        assert len(item.formats) == 2
        assert {fmt.quality for fmt in item.formats} == {"1080P", "720P"}
        assert all(fmt.url and fmt.url.startswith("https://") for fmt in item.formats)
        assert item.formats[0].quality == "1080P"  # 按高度降序
        assert item.formats[0].height == 1080
        assert item.formats[0].size_bytes == 47180000
        assert item.formats[0].format_id == "1080p"


# ---------------------------------------------------------------- 2. ttwid 重试


class TestVisitorCookieRetry:
    """``ADAPTER.parse`` 的 ttwid 重试链路。"""

    def test_retry_with_ttwid_cookie_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无数据页 → 申请 ttwid → 带 Cookie 重抓 → 解析成功。"""
        from app.modules.video.adapters.douyin import ADAPTER

        calls = _no_network(monkeypatch)
        page_calls = _stub_pages(monkeypatch, [_NO_DATA_PAGE, _fixture_html()])
        ttwid_calls: list[dict] = []
        _stub_ttwid(monkeypatch, calls=ttwid_calls)
        ytdlp_calls = _stub_ytdlp(monkeypatch)

        item = ADAPTER.parse(SHARE_URL, _parse_ctx())

        assert item.item_id == AWEME_ID
        assert item.title == "晚风里的一百种蓝 #风景 #自造样本"
        assert item.author.name == "示例作者"
        assert {fmt.quality for fmt in item.formats} == {"1080P", "720P"}
        assert all(fmt.url for fmt in item.formats)

        # 两次抓取：第一次不带 Cookie，第二次带 ttwid
        assert len(page_calls) == 2
        assert "Cookie" not in page_calls[0]["headers"]
        assert "ttwid=test-token" in page_calls[1]["headers"].get("Cookie", "")

        # 只申请一次访客 ID；成功解析后不再走 yt-dlp 兜底
        assert len(ttwid_calls) == 1
        assert ytdlp_calls == []
        assert calls == [], f"解析过程不应发起真实网络请求：{calls}"

    def test_ttwid_failure_keeps_parse_error_and_fetches_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """访客 ID 获取失败：仍抛 ``PARSE_FAILED``，且页面只抓取一次。"""
        from app.core.errors import VideoParseError
        from app.modules.video import net
        from app.modules.video.adapters.douyin import ADAPTER

        calls = _no_network(monkeypatch)
        page_calls = _stub_pages(monkeypatch, [_NO_DATA_PAGE])
        post_calls: list[str] = []

        def _failing_post(url: str, **_kwargs: object):
            post_calls.append(url)
            raise net.NetError("模拟访客注册接口不可用")

        monkeypatch.setattr(net, "http_post_json", _failing_post)
        ytdlp_calls = _stub_ytdlp(monkeypatch)

        with pytest.raises(VideoParseError) as excinfo:
            ADAPTER.parse(SHARE_URL, _parse_ctx())

        assert excinfo.value.code == "PARSE_FAILED"
        assert len(page_calls) == 1, "拿不到 ttwid 时不应再抓一次页面"
        assert len(post_calls) == 1
        assert ytdlp_calls == [SHARE_URL]  # 仍走原有兜底链
        assert calls == []

    def test_item_not_found_not_masked_by_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``filter_list: SYSTEM_ITEM_NOT_EXIST`` → 直接 ``ITEM_NOT_FOUND``，不重试。"""
        from app.core.errors import ItemNotFoundError
        from app.modules.video.adapters.douyin import ADAPTER

        calls = _no_network(monkeypatch)
        page_calls = _stub_pages(monkeypatch, [_ITEM_NOT_EXIST_PAGE])
        ttwid_calls: list[dict] = []
        _stub_ttwid(monkeypatch, calls=ttwid_calls)
        ytdlp_calls = _stub_ytdlp(monkeypatch)

        with pytest.raises(ItemNotFoundError) as excinfo:
            ADAPTER.parse(SHARE_URL, _parse_ctx())

        assert excinfo.value.code == "ITEM_NOT_FOUND"
        assert len(page_calls) == 1
        assert ttwid_calls == [], "真失效作品不应触发 ttwid 重试"
        assert ytdlp_calls == []
        assert calls == []

    def test_retry_still_failing_mentions_visitor_cookie_without_leaking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """带 Cookie 重试仍无数据：错误文案提示「访客 Cookie」，但不泄漏 Cookie 值。"""
        from app.core.errors import VideoParseError
        from app.modules.video.adapters.douyin import ADAPTER

        calls = _no_network(monkeypatch)
        page_calls = _stub_pages(monkeypatch, [_NO_DATA_PAGE, _NO_DATA_PAGE])
        _stub_ttwid(monkeypatch, token="secret-visitor-token")
        _stub_ytdlp(monkeypatch)

        with pytest.raises(VideoParseError) as excinfo:
            ADAPTER.parse(SHARE_URL, _parse_ctx())

        detail = excinfo.value.detail
        assert excinfo.value.code == "PARSE_FAILED"
        assert "访客 Cookie" in detail
        assert "页面结构已变化" in detail
        assert "secret-visitor-token" not in detail, "异常文案不得包含 Cookie 值"
        assert len(page_calls) == 2
        assert calls == []

    def test_configured_cookie_is_used_without_ttwid_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """已配置用户 Cookie 时：两次抓取都带它，且不再申请 ttwid。"""
        from app.modules.video import net
        from app.modules.video.adapters.douyin import ADAPTER

        calls = _no_network(monkeypatch)
        page_calls = _stub_pages(monkeypatch, [_NO_DATA_PAGE, _fixture_html()])
        post_calls: list[str] = []

        def _unexpected_post(url: str, **_kwargs: object):
            post_calls.append(url)
            raise AssertionError("已配置用户 Cookie 时不应申请访客 ID")

        monkeypatch.setattr(net, "http_post_json", _unexpected_post)
        _stub_ytdlp(monkeypatch)

        item = ADAPTER.parse(SHARE_URL, _parse_ctx(cookie="sessionid=user-configured"))

        assert item.formats
        assert page_calls[0]["headers"].get("Cookie") == "sessionid=user-configured"
        assert page_calls[1]["headers"].get("Cookie") == "sessionid=user-configured"
        assert post_calls == []
        assert calls == []

    def test_ttwid_cached_across_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ttwid 进程内缓存命中：第二次解析不再打注册接口。"""
        from app.modules.video.adapters.douyin import ADAPTER

        calls = _no_network(monkeypatch)
        page_calls = _stub_pages(
            monkeypatch,
            [_NO_DATA_PAGE, _fixture_html(), _NO_DATA_PAGE, _fixture_html()],
        )
        ttwid_calls: list[dict] = []
        _stub_ttwid(monkeypatch, calls=ttwid_calls)
        _stub_ytdlp(monkeypatch)

        first = ADAPTER.parse(SHARE_URL, _parse_ctx())
        second = ADAPTER.parse(SHARE_URL, _parse_ctx())

        assert first.item_id == second.item_id == AWEME_ID
        assert len(page_calls) == 4
        assert len(ttwid_calls) == 1, "6 小时缓存内不应重复申请访客 ID"
        assert calls == []


# ---------------------------------------------------------------- 3. Cookie 解析


class TestTtwidCookieParsing:
    """``Set-Cookie`` → ``ttwid=...`` 的解析（到分号为止）。"""

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (
                "ttwid=1%7CabcDEF123; Path=/; Domain=bytedance.com; "
                "Max-Age=31536000; HttpOnly; Secure",
                "ttwid=1%7CabcDEF123",
            ),
            ("ttwid=plain-token", "ttwid=plain-token"),
            ("other=1; ttwid=after-semicolon; Path=/", "ttwid=after-semicolon"),
            ("ttwid=; Path=/", None),
            ("other=1; Path=/", None),
            ("", None),
        ],
    )
    def test_parse_ttwid_cookie(self, header: str, expected: str | None) -> None:
        """各种 ``Set-Cookie`` 形态都只取 ``ttwid=值``，不把属性混进 Cookie。"""
        from app.modules.video.adapters.douyin import _parse_ttwid_cookie

        assert _parse_ttwid_cookie(header) == expected


# ---------------------------------------------------------------- 4. net 层 POST


def _fake_response(status: int, headers: dict[str, str], body: bytes):
    """构造一个满足 ``net._run_request`` 需要的响应替身。

    Args:
        status: 状态码。
        headers: 响应头。
        body: 响应体（一次性返回，之后返回空字节表示结束）。

    Returns:
        支持上下文管理与 ``read()`` 的对象。
    """

    class _Response:
        def __init__(self) -> None:
            self.status = status
            self.headers = headers
            self._buffer = body

        def read(self, _size: int = -1) -> bytes:
            chunk, self._buffer = self._buffer, b""
            return chunk

        def getcode(self) -> int:
            return status

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    return _Response()


class TestHttpPostJson:
    """``net.http_post_json``：POST + 逐跳校验 + 暴露 ``Set-Cookie``。"""

    def _install_opener(self, monkeypatch: pytest.MonkeyPatch, response) -> dict:
        """把 ``net._opener`` 换成记录请求并返回固定响应的替身。

        Args:
            monkeypatch: pytest 的补丁夹具。
            response: 替身响应对象。

        Returns:
            记录字典（url / method / data / headers）。
        """
        from app.modules.video import net

        seen: dict = {}

        class _Opener:
            def open(self, request, timeout=None):
                seen["url"] = request.full_url
                seen["method"] = request.get_method()
                seen["data"] = request.data
                seen["headers"] = dict(request.header_items())
                return response

        monkeypatch.setattr(net, "_opener", _Opener())
        return seen

    def test_post_json_returns_set_cookie_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """POST 发送 JSON 体、逐跳校验，并返回小写键响应头（含 ``set-cookie``）。"""
        from app.modules.video import net

        calls = _no_network(monkeypatch)
        seen = self._install_opener(
            monkeypatch,
            _fake_response(
                200,
                {
                    "Content-Type": "application/json; charset=utf-8",
                    "Set-Cookie": "ttwid=1%7Cabc; Path=/; Domain=bytedance.com",
                },
                b'{"status_code": 0, "message": "success"}',
            ),
        )
        validated: list[str] = []

        def _validator(url: str) -> str:
            validated.append(url)
            return url

        url = "https://ttwid.bytedance.com/ttwid/union/register/"
        final_url, payload, status, headers = net.http_post_json(
            url, json_body={"aid": 1768, "union": True}, timeout=1.0, validator=_validator
        )

        assert status == 200
        assert final_url == url
        assert payload == {"status_code": 0, "message": "success"}
        assert headers["set-cookie"].startswith("ttwid=1%7Cabc")
        assert seen["method"] == "POST"
        assert seen["data"] == b'{"aid":1768,"union":true}'
        assert seen["headers"].get("Content-type") == "application/json"
        assert validated == [url], "每一跳都必须经过校验器"
        assert calls == [], f"不应真实触网：{calls}"

    def test_post_json_non_2xx_returns_none_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非 2xx：JSON 为 None，但状态码与响应头仍返回（供调用方判断）。"""
        from app.modules.video import net

        calls = _no_network(monkeypatch)
        self._install_opener(
            monkeypatch,
            _fake_response(403, {"Set-Cookie": "ttwid=blocked"}, b"forbidden"),
        )

        _final, payload, status, headers = net.http_post_json(
            "https://ttwid.bytedance.com/ttwid/union/register/",
            json_body={"aid": 1768},
            timeout=1.0,
            validator=lambda url: url,
        )

        assert status == 403
        assert payload is None
        assert headers["set-cookie"] == "ttwid=blocked"
        assert calls == []
