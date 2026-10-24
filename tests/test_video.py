"""视频下载模块离线测试。

覆盖范围（对应 ``docs/视频下载模块-实现契约.md`` §7 ↔ PRD §13.1 的 TC-01..TC-24）：

    1. 链接提取/去重/上限（中文分享文案、多行、20 条、http→https、尾部中文标点）；
    2. 清晰度归一化与默认选择策略（不选 4K、≤1080P 取最高、偏好命中）；
    3. 产物命名清洗（Windows 保留字符、控制字符、emoji、120 截断、空值兜底、重名 -1）；
    4. SSRF 防护（内网/回环/链路本地/云元数据、``file://``、跳转到内网）；
    5. Cookie 加解密往返、脱敏（``SESSDATA=****9f2a``）、落盘无明文、清除；
    6. 历史记录（记录/分页/筛选/搜索/删除/清空/200 上限/24h 去重/原子写）；
    7. 三平台夹具解析（``tests/fixtures/video/**``，自造脱敏样本）；
    8. 契约端点（platforms/parse/history/settings、503、401、429）；
    9. 任务模型（``TaskInput.source``、旧任务兼容、``module=video`` 过滤）。

**完全离线**：任何用例都不发起真实网络请求；涉及网络的位置一律 monkeypatch
``urllib.request.urlopen`` 并在用例内断言调用计数为 0 或抛出断言错误。
外部引擎（FFmpeg / yt-dlp / LibreOffice）缺失不影响本文件运行。
"""

from __future__ import annotations

import contextlib
import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "video"


# ---------------------------------------------------------------- 通用工具


def _no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把全部出站入口换成「被调用即失败」的探针。

    覆盖三层，缺一不可：
        1. ``urllib.request.urlopen``（标准库直连）；
        2. ``app.modules.video.net._opener``（net.py 用手动重定向，绕开了 urlopen）；
        3. ``socket.getaddrinfo``（DNS 解析同样属于出站行为）。

    Args:
        monkeypatch: pytest 的补丁夹具。

    Returns:
        调用记录列表；用例结束后断言其为空即可证明未触网。
    """
    import socket

    calls: list[str] = []

    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003 - 测试探针
        calls.append(str(args[0] if args else kwargs))
        raise AssertionError(f"测试不应发起网络请求：{args!r}")

    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)

    # net.py 用自建的 OpenerDirector，不经过 urllib.request.urlopen
    try:
        from app.modules.video import net
    except ImportError:  # pragma: no cover - 模块缺失时其它用例会先失败
        return calls

    class _NoopOpener:
        """替身 Opener：任何 open 调用都记为一次违规网络请求。"""

        def open(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return _forbidden(*args, **kwargs)

    monkeypatch.setattr(net, "_opener", _NoopOpener())
    return calls


def _dns_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 DNS 解析确定性地失败，并清空 registry 的 DNS 缓存。

    作用：让「白名单域名在解析失败时放行 / 非白名单域名拒绝」的用例在
    联网与离线机器上表现一致，同时确保测试**不做真实 DNS 查询**。

    Args:
        monkeypatch: pytest 的补丁夹具。
    """
    import socket

    from app.modules.video.adapters import registry

    def _fail(*args, **kwargs):  # noqa: ANN002, ANN003 - 测试探针
        raise socket.gaierror("offline test")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    registry.reset_for_tests()


def _video_format(*, fmt_id: str, quality: str, height: int | None, fps: int = 30,
                  bitrate: int = 1000, need_login: bool = False,
                  need_vip: bool = False, url: str | None = None):
    """构造一个 ``VideoFormat``（避免每个用例重复写 12 个字段）。

    Returns:
        ``VideoFormat`` 实例。
    """
    from app.modules.video.models import VideoFormat

    return VideoFormat(
        format_id=fmt_id,
        quality=quality,
        height=height,
        fps=fps,
        bitrate=bitrate,
        need_login=need_login,
        need_vip=need_vip,
        url=url or f"https://example-cdn.test/{fmt_id}.mp4",
    )


# ================================================================ 1. 链接提取


class TestExtractUrls:
    """分享文案 → 链接列表（契约 §3.2「从每条原始文本里提取链接」）。"""

    def test_extract_urls_from_chinese_share_text(self) -> None:
        """中文口令 + 表情包裹的分享文案要能提取出唯一链接。"""
        from app.modules.video.adapters.registry import extract_links

        text = (
            "7.43 复制打开抖音，看看【示例作者的作品】晚风里的一百种蓝 😆 "
            "https://v.douyin.com/AbCdEfG/ 复制此链接，打开Dou音搜索，直接观看视频！"
        )
        urls = extract_links(text)
        assert urls == ["https://v.douyin.com/AbCdEfG/"]

    def test_extract_urls_multiline_and_dedup(self) -> None:
        """多行输入逐行提取，同一批内按 URL 去重且保持首次出现顺序。"""
        from app.modules.video.adapters.registry import extract_links

        text = "\n".join(
            [
                "https://www.bilibili.com/video/BV1xx411c7mD",
                "看看这个 https://v.douyin.com/AbCdEfG/ 不错",
                "https://www.bilibili.com/video/BV1xx411c7mD",
                "https://www.kuaishou.com/short-video/3xabcdEfGh",
            ]
        )
        urls = extract_links(text)
        assert urls == [
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://v.douyin.com/AbCdEfG/",
            "https://www.kuaishou.com/short-video/3xabcdEfGh",
        ]

    def test_extract_urls_normalizes_http_and_trailing_punct(self) -> None:
        """``http://`` 归一化为 ``https://``，并剥掉尾部中文标点与括号。"""
        from app.modules.video.adapters.registry import extract_links

        text = "链接：http://b23.tv/AbCdEf，还有（https://v.douyin.com/XyZ/）。"
        urls = extract_links(text)
        assert "https://b23.tv/AbCdEf" in urls
        assert "https://v.douyin.com/XyZ/" in urls
        assert all(url.startswith("https://") for url in urls)
        assert all(not url.endswith(("，", "。", "）", ")", "、")) for url in urls)

    def test_extract_urls_respects_max_urls(self) -> None:
        """超过 20 条时只取前 20 条（上限来自 ``settings.video.max_urls``）。"""
        from app.modules.video.adapters.registry import extract_links

        text = "\n".join(
            f"https://www.bilibili.com/video/BV1xx411c7m{i}" for i in range(30)
        )
        urls = extract_links(text)
        assert len(urls) == 20
        assert urls[0].endswith("BV1xx411c7m0")

    def test_extract_urls_empty_and_garbage(self) -> None:
        """无链接文本返回空列表，不抛异常。"""
        from app.modules.video.adapters.registry import extract_links

        assert extract_links("") == []
        assert extract_links("这只是一段没有链接的文字 😀") == []


# ================================================================ 2. 清晰度


class TestQuality:
    """清晰度归一化与默认选择（契约 §4.2）。"""

    @pytest.mark.parametrize(
        ("height", "fps", "expected"),
        [
            (2160, 30, "4K"),
            (1600, 30, "2K"),
            (1440, 30, "2K"),
            (1080, 60, "1080P60"),
            (1080, 30, "1080P"),
            (720, 30, "720P"),
            (540, 30, "540P"),
            (480, 30, "480P"),
            (360, 30, "360P"),
            (240, 30, "360P"),
            (None, None, "360P"),
        ],
    )
    def test_normalize_quality_levels(self, height, fps, expected) -> None:
        """按高度（与帧率）落到统一等级。"""
        from app.modules.video.adapters.base import normalize_quality

        assert normalize_quality(height, fps) == expected

    def test_normalize_quality_audio_when_no_video_track(self) -> None:
        """无视频轨时归一化为 ``audio``（仅音频下载路径）。"""
        from app.modules.video.adapters.base import normalize_quality

        assert normalize_quality(1080, 30, has_video=False) == "audio"
        assert normalize_quality(None, None, has_video=False) == "audio"

    def test_default_quality_never_4k(self) -> None:
        """默认策略绝不选中 4K（即便 4K 是最优清晰度）。"""
        from app.modules.video.adapters.base import pick_default_format

        formats = [
            _video_format(fmt_id="v-4k", quality="4K", height=2160, bitrate=9000),
            _video_format(fmt_id="v-1080", quality="1080P", height=1080, bitrate=4000),
            _video_format(fmt_id="v-720", quality="720P", height=720, bitrate=2000),
        ]
        picked = pick_default_format(formats, None)
        assert picked is not None
        assert picked["quality"] != "4K"
        assert picked["format_id"] == "v-1080"

    def test_default_quality_picks_highest_under_1080p(self) -> None:
        """无偏好时取 ≤1080P 的最高可用（1080P > 720P > 480P）。"""
        from app.modules.video.adapters.base import pick_default_format

        formats = [
            _video_format(fmt_id="v-720", quality="720P", height=720),
            _video_format(fmt_id="v-480", quality="480P", height=480),
            _video_format(fmt_id="v-1080", quality="1080P", height=1080),
        ]
        picked = pick_default_format(formats, "")
        assert picked is not None
        assert picked["format_id"] == "v-1080"

    def test_default_quality_honours_preference(self) -> None:
        """用户偏好命中时优先使用（含 1080P 偏好命中 1080P60）。"""
        from app.modules.video.adapters.base import pick_default_format

        formats = [
            _video_format(fmt_id="v-1080", quality="1080P", height=1080, bitrate=4000),
            _video_format(fmt_id="v-720", quality="720P", height=720, bitrate=2000),
            _video_format(fmt_id="v-1080p60", quality="1080P60", height=1080, fps=60, bitrate=6000),
        ]
        picked = pick_default_format(formats, "720P")
        assert picked is not None and picked["format_id"] == "v-720"

        picked_1080 = pick_default_format(formats, "1080P")
        assert picked_1080 is not None
        assert picked_1080["quality"] in {"1080P", "1080P60"}

    def test_default_quality_skips_vip_and_login_by_default(self) -> None:
        """会员专属与需登录清晰度默认不选；``allow_login=True`` 才放行登录项。"""
        from app.modules.video.adapters.base import pick_default_format

        formats = [
            _video_format(fmt_id="v-vip", quality="1080P", height=1080, need_vip=True),
            _video_format(fmt_id="v-login", quality="720P", height=720, need_login=True),
            _video_format(fmt_id="v-480", quality="480P", height=480),
        ]
        picked = pick_default_format(formats, None)
        assert picked is not None and picked["format_id"] == "v-480"

        picked_login = pick_default_format(formats, "720P", allow_login=True)
        assert picked_login is not None and picked_login["format_id"] == "v-login"

    def test_default_quality_audio_only_and_empty(self) -> None:
        """纯音频列表返回 audio；空列表返回 None。"""
        from app.modules.video.adapters.base import pick_default_format

        audio = _video_format(fmt_id="a-128", quality="audio", height=None, bitrate=128000)
        picked = pick_default_format([audio], None)
        assert picked is not None and picked["quality"] == "audio"
        assert pick_default_format([], None) is None

    def test_default_quality_keeps_some_track_when_only_4k_available(self) -> None:
        """只有 4K/2K 可选时退而取其中最低一档，而不是返回 None。"""
        from app.modules.video.adapters.base import pick_default_format

        formats = [
            _video_format(fmt_id="v-4k", quality="4K", height=2160),
            _video_format(fmt_id="v-2k", quality="2K", height=1440),
        ]
        picked = pick_default_format(formats, None)
        assert picked is not None
        assert picked["quality"] == "2K"


# ================================================================ 3. 命名


class TestNaming:
    """产物命名（契约 §4.5）。"""

    def test_build_name_strips_illegal_chars(self) -> None:
        """``\\ / : * ? " < > |`` 全部从文件名中清除。"""
        from app.modules.video.naming import build_name

        name = build_name(
            platform="douyin",
            author="示例作者",
            title='非法字符\\/:*?"<>|测试',
            quality="1080P",
            ext="mp4",
        )
        for char in '\\/:*?"<>|':
            assert char not in name, f"文件名仍含非法字符 {char!r}：{name}"
        assert name.endswith(".mp4")

    def test_build_name_removes_emoji_and_control_chars(self) -> None:
        """emoji 与控制字符被剔除，正常中文保留。"""
        from app.modules.video.naming import build_name

        name = build_name(
            platform="bilibili",
            author="UP主",
            title="夏日😀vlog🔥\x00\x1f",
            quality="720P",
            ext="mp4",
        )
        assert "😀" not in name and "🔥" not in name
        assert "\x00" not in name and "\x1f" not in name
        assert "夏日" in name and "vlog" in name

    def test_build_name_truncates_to_120(self) -> None:
        """超长作者 + 超长标题截断到 120 字符以内，且保留扩展名。"""
        from app.modules.video.naming import MAX_NAME_LEN, build_name

        name = build_name(
            platform="kuaishou",
            author="作" * 400,
            title="长" * 400,
            quality="1080P",
            ext="mp4",
        )
        assert len(name) <= MAX_NAME_LEN
        assert name.endswith(".mp4")

    def test_build_name_fallbacks_for_empty_fields(self) -> None:
        """空作者 → 未知作者；空标题 → 未命名作品。"""
        from app.modules.video.naming import build_name

        name = build_name(
            platform="douyin", author="", title="   ", quality="1080P", ext="mp4"
        )
        assert "未知作者" in name
        assert "未命名作品" in name

    def test_build_name_uses_contract_template(self) -> None:
        """命名模板为 ``{platform}-{author}-{title}-{quality}.{ext}``。"""
        from app.modules.video.naming import build_name

        name = build_name(
            platform="douyin", author="某某", title="晚风", quality="1080P", ext="mp4"
        )
        assert name == "douyin-某某-晚风-1080P.mp4"

    def test_dedupe_name_appends_numeric_suffix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同名产物依次追加 ``-1``、``-2``（依据 registry 未过期产物判断）。"""
        from app.modules.video import naming

        existing = [SimpleNamespace(name="douyin-某某-晚风-1080P.mp4")]
        monkeypatch.setattr(
            naming, "registry", SimpleNamespace(list_all=lambda: existing), raising=True
        )

        assert naming.dedupe_name("douyin-某某-晚风-1080P.mp4") == "douyin-某某-晚风-1080P-1.mp4"

        existing.append(SimpleNamespace(name="douyin-某某-晚风-1080P-1.mp4"))
        assert naming.dedupe_name("douyin-某某-晚风-1080P.mp4") == "douyin-某某-晚风-1080P-2.mp4"

        assert naming.dedupe_name("other-作品.mp4") == "other-作品.mp4"

    def test_naming_and_quality_do_not_touch_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """纯函数（命名/清晰度）不得发起任何网络请求（契约 §7.9）。"""
        from app.modules.video.adapters.base import normalize_quality, pick_default_format
        from app.modules.video.naming import build_name, mime_for_ext

        calls = _no_network(monkeypatch)
        build_name(platform="douyin", author="作者", title="标题", quality="1080P", ext="mp4")
        normalize_quality(1080, 30)
        pick_default_format([_video_format(fmt_id="v", quality="1080P", height=1080)], None)
        mime_for_ext("mp4")
        assert calls == [], f"纯函数不应触网：{calls}"


# ================================================================ 4. SSRF


class TestSsrf:
    """SSRF 防护（契约 §5.2 / TC-09）。"""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/admin",
            "https://127.0.0.1/admin",
            "http://10.0.0.1/secret",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/local",
            "file:///etc/passwd",
            "ftp://example.com/a.mp4",
            "data:text/html,<h1>x</h1>",
        ],
    )
    def test_validate_public_url_rejects_internal_hosts(self, url: str) -> None:
        """内网/回环/链路本地/元数据地址与 file/ftp/data scheme 一律拒绝。"""
        from app.core.errors import ToolkitError
        from app.modules.video.adapters.registry import validate_public_url

        with pytest.raises(ToolkitError):
            validate_public_url(url)

    def test_validate_public_url_accepts_whitelisted_platform_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """白名单平台域名内的正常 https 地址放行（DNS 失败时 fail-open 仅限白名单）。"""
        from app.modules.video.adapters.registry import validate_public_url

        _dns_offline(monkeypatch)
        for url in (
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://v.douyin.com/AbCdEfG/",
            "https://www.kuaishou.com/short-video/3xabcdEfGh",
        ):
            assert validate_public_url(url).startswith("https://")

    def test_validate_public_url_rejects_non_whitelist_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非白名单域名不能被当成媒体地址放行（DNS 失败时 fail-closed）。"""
        from app.core.errors import ToolkitError
        from app.modules.video.adapters.registry import validate_public_url

        _dns_offline(monkeypatch)
        with pytest.raises(ToolkitError):
            validate_public_url("https://internal.example.invalid/video.mp4")

    def test_ssrf_check_makes_no_network_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SSRF 判定必须在发起请求之前完成：urlopen 调用计数为 0。"""
        from app.core.errors import ToolkitError
        from app.modules.video.adapters.registry import validate_public_url

        calls = _no_network(monkeypatch)
        for url in ("http://127.0.0.1/x", "https://10.0.0.1/x", "file:///etc/passwd"):
            with pytest.raises(ToolkitError):
                validate_public_url(url)
        assert calls == [], f"SSRF 校验不应发起网络请求：{calls}"


class TestShortlink:
    """短链展开的逐跳校验（契约 §5.2；离线用注入 fetcher 模拟每一跳）。"""

    def test_expand_shortlink_follows_whitelisted_hops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """合法短链逐跳展开到平台长链。"""
        from app.modules.video.adapters.registry import expand_shortlink

        _dns_offline(monkeypatch)
        final = "https://www.iesdouyin.com/share/video/7412345678901234567/"
        hops = {"https://v.douyin.com/XyZ/": final}
        result = expand_shortlink("https://v.douyin.com/XyZ/", fetcher=lambda url: hops.get(url))
        assert result == final

    def test_expand_shortlink_rejects_internal_redirect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """短链跳转到内网地址必须拒绝（TC-09）。"""
        from app.core.errors import ShortlinkError
        from app.modules.video.adapters.registry import expand_shortlink

        _dns_offline(monkeypatch)
        with pytest.raises(ShortlinkError):
            expand_shortlink(
                "https://v.douyin.com/XyZ/", fetcher=lambda _url: "http://127.0.0.1/admin"
            )

    def test_expand_shortlink_rejects_non_whitelist_redirect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """跳转到非白名单域名同样拒绝。"""
        from app.core.errors import ShortlinkError
        from app.modules.video.adapters.registry import expand_shortlink

        _dns_offline(monkeypatch)
        with pytest.raises(ShortlinkError):
            expand_shortlink(
                "https://b23.tv/XyZ", fetcher=lambda _url: "https://internal.example.invalid/x"
            )

    def test_expand_shortlink_rejects_too_many_hops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """超过 5 跳返回 ``SHORTLINK_EXPAND_FAILED``，不无限跟随。"""
        from app.core.errors import ShortlinkError
        from app.modules.video.adapters.registry import expand_shortlink

        _dns_offline(monkeypatch)

        counter = {"n": 0}

        def _endless(_url: str) -> str:
            counter["n"] += 1
            return f"https://v.douyin.com/hop{counter['n']}/"

        with pytest.raises(ShortlinkError):
            expand_shortlink("https://v.douyin.com/start/", fetcher=_endless)


# ================================================================ 5. Cookie


@pytest.fixture()
def credentials_clean():
    """清理凭据文件与内存缓存，保证 Cookie 用例互不影响。"""
    from app.core.config import STORAGE_DIR
    from app.modules.video import credentials

    path = STORAGE_DIR / "credentials.json"
    credentials.reset_for_tests()
    path.unlink(missing_ok=True)
    yield path
    credentials.reset_for_tests()
    path.unlink(missing_ok=True)


class TestCredentials:
    """Cookie 加解密、脱敏与落盘安全（契约 §5.4）。"""

    def test_cookie_roundtrip(self, credentials_clean: Path) -> None:
        """保存后能读回明文；状态为已配置且带脱敏值。"""
        from app.modules.video import credentials

        cookie = "SESSDATA=abcd1234efgh9f2a; bili_jct=deadbeef; DedeUserID=10001"
        status = credentials.save_cookie("bilibili", cookie)

        assert status["configured"] is True
        assert status["masked"] == "SESSDATA=****9f2a"
        assert credentials.get_cookie("bilibili") == cookie

    def test_mask_cookie_bilibili(self) -> None:
        """B站脱敏固定为 ``SESSDATA=****末4位``。"""
        from app.modules.video import credentials

        assert (
            credentials.mask_cookie("bilibili", "buvid3=x; SESSDATA=abcdef9f2a; bili_jct=y")
            == "SESSDATA=****9f2a"
        )
        assert credentials.mask_cookie("bilibili", "") == ""

    def test_mask_cookie_douyin_and_kuaishou(self) -> None:
        """其它平台取前 4 个键名 + 末 4 位；无 ``=`` 时只给末 4 位。"""
        from app.modules.video import credentials

        masked = credentials.mask_cookie(
            "douyin",
            "sessionid=abcdef1234; uid=99; passport_csrf_token=zzzz; sid_guard=4242",
        )
        assert masked == "sessionid,uid,passport_csrf_token,sid_guard=****4242"

        assert credentials.mask_cookie("kuaishou", "did=12345678") == "did=****5678"
        assert credentials.mask_cookie("douyin", "opaque-token-abcd") == "****abcd"

    def test_cookie_file_contains_no_plaintext(self, credentials_clean: Path) -> None:
        """落盘的 ``credentials.json`` 不得出现 Cookie 明文片段。"""
        from app.modules.video import credentials

        cookie = "SESSDATA=PLAINTEXT-SECRET-9f2a; bili_jct=PLAINTEXT-JCT"
        credentials.save_cookie("bilibili", cookie)

        text = credentials_clean.read_text(encoding="utf-8")
        assert "PLAINTEXT" not in text
        assert "SESSDATA=PLAINTEXT" not in text
        assert "bili_jct=PLAINTEXT" not in text
        payload = json.loads(text)
        assert payload["version"] == 1
        assert "bilibili" in payload["items"]
        for key in ("nonce", "ciphertext", "tag", "updated_at"):
            assert key in payload["items"]["bilibili"]

    def test_clear_cookie(self, credentials_clean: Path) -> None:
        """空字符串表示清除；清除后读回 None。"""
        from app.modules.video import credentials

        credentials.save_cookie("douyin", "sessionid=abcdef")
        assert credentials.get_cookie("douyin") == "sessionid=abcdef"

        assert credentials.clear_cookie("douyin") is True
        assert credentials.get_cookie("douyin") is None

        status = credentials.save_cookie("douyin", "")
        assert status["configured"] is False and status["masked"] is None

    def test_cookie_status_lists_three_platforms(self, credentials_clean: Path) -> None:
        """状态接口对三个平台都给占位，前端不必判空。"""
        from app.modules.video import credentials

        status = credentials.cookie_status()
        assert set(status) == {"douyin", "bilibili", "kuaishou"}
        assert all(item["configured"] is False for item in status.values())

# ================================================================ 6. 历史记录


def _history_entry(
    *,
    platform: str = "douyin",
    item_id: str = "7412345678901234567",
    title: str = "晚风里的一百种蓝",
    author: str = "示例作者",
    status: str = "success",
    file_id: str | None = "f_8a3c2e",
    created_at: str | None = None,
    expire_at: str | None = None,
    url: str | None = "https://www.douyin.com/video/7412345678901234567",
) -> dict:
    """构造一条历史记录原始字典（用于预写文件、模拟历史数据）。

    Returns:
        含内部字段（``url`` 等）的记录字典。
    """
    from app.core.tasks import utc_now_iso

    return {
        "platform": platform,
        "item_id": item_id,
        "title": title,
        "author": author,
        "cover_url": None,
        "quality": "1080P",
        "size_bytes": 1024,
        "status": status,
        "task_id": "task-1",
        "file_id": file_id,
        "url": url,
        "owner_id": None,
        "created_at": created_at or utc_now_iso(),
        "expire_at": expire_at,
    }


def _write_history_file(path: Path, items: list[dict]) -> None:
    """预写历史文件（模拟进程重启后载入旧数据）。"""
    path.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")


class TestHistory:
    """历史记录：原子写、分页、筛选、上限、去重（契约 §3.4 / §5.3）。"""

    def test_record_and_pagination(self, tmp_path: Path) -> None:
        """记录按时间倒序分页，``total`` / ``page`` / ``page_size`` 正确。"""
        from app.modules.video.history import VideoHistory

        store = VideoHistory(path=tmp_path / "h.json")
        for index in range(3):
            store.record(
                platform="douyin",
                item_id=f"item-{index}",
                title=f"标题{index}",
                author="作者",
                file_id=f"f{index}",
            )

        page1 = store.list_items(page=1, page_size=2)
        assert page1["total"] == 3
        assert page1["page"] == 1 and page1["page_size"] == 2
        assert len(page1["items"]) == 2
        assert page1["items"][0]["title"] == "标题2"  # 最新在前

        page2 = store.list_items(page=2, page_size=2)
        assert len(page2["items"]) == 1
        assert page2["items"][0]["title"] == "标题0"

    def test_platform_filter_and_keyword_search(self, tmp_path: Path) -> None:
        """平台过滤 + 标题/作者关键词搜索（大小写不敏感）。"""
        from app.modules.video.history import VideoHistory

        store = VideoHistory(path=tmp_path / "h.json")
        store.record(platform="douyin", item_id="d1", title="Vlog 夏日", author="小张")
        store.record(platform="bilibili", item_id="b1", title="示例稿件", author="UP主")
        store.record(platform="kuaishou", item_id="k1", title="vlog 快手", author="老李")

        only_bili = store.list_items(platform="bilibili")
        assert only_bili["total"] == 1
        assert only_bili["items"][0]["item_id"] == "b1"

        all_platforms = store.list_items(platform="all")
        assert all_platforms["total"] == 3

        by_title = store.list_items(keyword="vlog")
        assert by_title["total"] == 2

        by_author = store.list_items(keyword="up主")
        assert by_author["total"] == 1

    def test_remove_and_clear(self, tmp_path: Path) -> None:
        """单条删除返回删除条数；清空返回清空条数。"""
        from app.modules.video.history import VideoHistory

        store = VideoHistory(path=tmp_path / "h.json")
        store.record(platform="douyin", item_id="d1", file_id="f1")
        store.record(platform="douyin", item_id="d2", file_id="f2")
        store.record(platform="bilibili", item_id="d1", file_id="f3")

        assert store.remove("d1", platform="douyin") == 1
        assert store.count() == 2
        assert store.remove("not-exist") == 0
        assert store.clear() == 2
        assert store.count() == 0

    def test_limit_is_fifo(self, tmp_path: Path) -> None:
        """超过 ``history_limit`` 时按 FIFO 淘汰最旧记录（默认 200，此处用 3 验证逻辑）。"""
        from app.modules.video.history import VideoHistory

        store = VideoHistory(path=tmp_path / "h.json", limit=3)
        for index in range(5):
            store.record(platform="douyin", item_id=f"item-{index}", title=f"标题{index}")

        assert store.count() == 3
        titles = [item["title"] for item in store.list_items()["items"]]
        assert titles == ["标题4", "标题3", "标题2"]

    def test_dedupe_hit_within_window(self, tmp_path: Path) -> None:
        """24 小时窗口内同 platform+item_id 的成功记录可复用。"""
        from app.modules.video.history import VideoHistory

        store = VideoHistory(path=tmp_path / "h.json")
        store.record(
            platform="douyin",
            item_id="item-1",
            file_id="f_1",
            expire_at="2999-01-01T00:00:00.000Z",
        )
        hit = store.find_reusable("douyin", "item-1", window_hours=24)
        assert hit is not None and hit["file_id"] == "f_1"

        # 窗口为 0 表示禁用复用
        assert store.find_reusable("douyin", "item-1", window_hours=0) is None
        # 不同作品不复用
        assert store.find_reusable("douyin", "item-2") is None

    def test_dedupe_ignores_expired_artifact(self, tmp_path: Path) -> None:
        """产物已过期的记录不可复用（即使 created_at 在窗口内）。"""
        from app.modules.video.history import VideoHistory

        path = tmp_path / "h.json"
        _write_history_file(
            path,
            [
                _history_entry(
                    item_id="expired",
                    file_id="f_old",
                    expire_at="2000-01-01T00:00:00.000Z",
                )
            ],
        )
        store = VideoHistory(path=path)
        assert store.find_reusable("douyin", "expired") is None

    def test_dedupe_ignores_old_and_failed_records(self, tmp_path: Path) -> None:
        """超过 24 小时的记录、失败记录都不参与复用。"""
        from app.modules.video.history import VideoHistory

        path = tmp_path / "h.json"
        _write_history_file(
            path,
            [
                _history_entry(
                    item_id="old",
                    file_id="f_old",
                    created_at="2000-01-01T00:00:00.000Z",
                    expire_at="2999-01-01T00:00:00.000Z",
                ),
                _history_entry(item_id="failed", status="failed", file_id=None),
            ],
        )
        store = VideoHistory(path=path)
        assert store.find_reusable("douyin", "old") is None
        assert store.find_reusable("douyin", "failed") is None

    def test_upsert_keeps_single_entry(self, tmp_path: Path) -> None:
        """同一作品重复记录只保留最新一条（不刷屏）。"""
        from app.modules.video.history import VideoHistory

        store = VideoHistory(path=tmp_path / "h.json")
        store.record(platform="douyin", item_id="item-1", file_id="f_1", quality="720P")
        store.record(platform="douyin", item_id="item-1", file_id="f_2", quality="1080P")
        assert store.count() == 1
        assert store.list_items()["items"][0]["quality"] == "1080P"

    def test_atomic_write_is_valid_json(self, tmp_path: Path) -> None:
        """写后文件必须是合法 JSON，且没有残留 ``.tmp`` 中间文件。"""
        from app.modules.video.history import VideoHistory

        path = tmp_path / "h.json"
        store = VideoHistory(path=path)
        store.record(platform="douyin", item_id="item-1", file_id="f_1")

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict) and isinstance(payload["items"], list)
        assert len(payload["items"]) == 1
        assert not path.with_suffix(".tmp").exists()

    def test_api_fields_never_expose_media_url(self, tmp_path: Path) -> None:
        """历史 API 结构不得出现媒体直链字段 ``url``（只落盘不对外）。"""
        from app.modules.video.history import VideoHistory

        store = VideoHistory(path=tmp_path / "h.json")
        store.record(
            platform="douyin",
            item_id="item-1",
            file_id="f_1",
            url="https://example-cdn.test/media.mp4?sign=secret",
        )
        item = store.list_items()["items"][0]
        assert "url" not in item
        assert "sign=secret" not in json.dumps(item)


# ================================================================ 7. 设置读写


@pytest.fixture()
def video_settings():
    """快照并恢复 ``settings.video``，避免设置用例污染其它用例。"""
    from app.core.config import PlatformToggle, get_settings

    video = get_settings().video
    snapshot = video.model_dump()
    yield video
    for key, value in snapshot.items():
        if key == "platforms":
            video.platforms = {
                name: PlatformToggle(**cfg) if isinstance(cfg, dict) else cfg
                for name, cfg in value.items()
            }
        else:
            setattr(video, key, value)


class TestSettingsStore:
    """设置读写（契约 §3.5 / §5.5）。"""

    def test_update_roundtrip(self, tmp_path: Path, video_settings, monkeypatch) -> None:
        """部分更新立即反映到读取结果；标量写回受控的临时 TOML（不碰仓库 config.toml）。"""
        from app.modules.video import settings_store

        config_file = tmp_path / "config.toml"
        config_file.write_text("[video]\n# 保留注释\nprefer_quality = \"1080P\"\n", encoding="utf-8")
        monkeypatch.setattr(settings_store, "CONFIG_FILE", config_file)

        result = settings_store.update_video_settings(
            {"prefer_quality": "720P", "max_concurrent": 3}
        )
        assert result["prefer_quality"] == "720P"
        assert result["max_concurrent"] == 3

        text = config_file.read_text(encoding="utf-8")
        assert "prefer_quality = \"720P\"" in text
        assert "max_concurrent = 3" in text
        assert "# 保留注释" in text  # 注释未被 toml 重写冲掉

    @pytest.mark.parametrize(
        "patch",
        [
            {"prefer_quality": "8K"},
            {"max_concurrent": 9},
            {"download_segments": 0},
            {"max_file_mb": 0},
            {"prefer_format": "avi"},
            {"platforms": {"unknown": {"enabled": True}}},
        ],
    )
    def test_invalid_patch_rejected(self, patch: dict, video_settings) -> None:
        """非法取值返回 ``ValidationError``（400），不静默写入。"""
        from app.core.errors import ValidationError
        from app.modules.video import settings_store

        with pytest.raises(ValidationError):
            settings_store.update_video_settings(patch)

    def test_cookie_patch_masks_value(self, tmp_path: Path, video_settings, monkeypatch,
                                      credentials_clean: Path) -> None:
        """Cookie 更新后读取结果只含脱敏值，不回显明文。"""
        from app.modules.video import settings_store

        monkeypatch.setattr(settings_store, "CONFIG_FILE", tmp_path / "config.toml")
        result = settings_store.update_video_settings(
            {"cookies": {"bilibili": "SESSDATA=abcdefgh9f2a; bili_jct=secret"}}
        )
        cookie = result["platforms"]["bilibili"]["cookie"]
        assert cookie["configured"] is True
        assert cookie["masked"] == "SESSDATA=****9f2a"
        assert "bili_jct=secret" not in json.dumps(result)


# ================================================================ 8. 限流 / 熔断


class TestLimiter:
    """限流与平台熔断（契约 §4.6 / TC-20）。"""

    def test_parse_limiter_rejects_21st_request(self) -> None:
        """解析限流 20 次/分钟：第 21 次抛 ``RateLimitedError`` 且带 retry_after。"""
        from app.core.errors import RateLimitedError
        from app.modules.video import limiter

        limiter.reset()
        try:
            for _ in range(20):
                limiter.check_parse("127.0.0.1", "tester")
            with pytest.raises(RateLimitedError) as excinfo:
                limiter.check_parse("127.0.0.1", "tester")
            assert excinfo.value.code == "RATE_LIMITED"
            assert int(excinfo.value.extra.get("retry_after", 0)) >= 1
        finally:
            limiter.reset()

    def test_platform_breaker_opens_after_five_failures(self) -> None:
        """同平台连续 5 次失败后熔断，冷却期内立即 ``RiskControlError``。"""
        from app.core.errors import RiskControlError
        from app.modules.video import limiter

        limiter.reset()
        try:
            for _ in range(5):
                limiter.record_platform_failure("douyin")

            with pytest.raises(RiskControlError) as excinfo:
                limiter.assert_platform_available("douyin")
            assert excinfo.value.code == "RISK_CONTROL"
            assert int(excinfo.value.extra.get("retry_after", 0)) >= 1

            # 其它平台不受影响
            limiter.assert_platform_available("bilibili")

            # 成功一次即解除
            limiter.record_platform_success("douyin")
            limiter.assert_platform_available("douyin")
        finally:
            limiter.reset()


# ================================================================ 9. 任务模型


class TestTaskModel:
    """``TaskInput.source`` 与 ``module=video`` 任务可见性（契约 §2.1 / §3.3）。"""

    def test_task_input_source_serialized(self) -> None:
        """``source`` 非空时出现在 ``to_api()``，为空时键不存在（老前端结构不变）。"""
        from app.core.tasks import TaskInput

        with_source = TaskInput(
            name="晚风里的一百种蓝",
            size=0,
            sha256="",
            source="https://www.douyin.com/video/7412345678901234567",
        )
        assert with_source.to_api()["source"].startswith("https://www.douyin.com/video/")

        without_source = TaskInput(name="报告.pdf", size=1024, sha256="abcd1234")
        assert "source" not in without_source.to_api()

    def test_legacy_task_record_without_source(self) -> None:
        """旧 ``tasks.json`` 中缺少 ``source`` 的记录能正常反序列化/输出。"""
        from app.core.tasks import TaskRecord

        legacy = TaskRecord(
            task_id="legacy-1",
            module="converter",
            operation="pdf2docx",
            status="success",
            inputs=[{"name": "a.pdf", "size": 10, "sha256": "deadbeef"}],
        )
        api = legacy.to_api()
        assert "source" not in api["inputs"][0]
        assert api["module"] == "converter"

    def test_module_video_task_visible_via_api(self, client, auth_off) -> None:
        """``module="video"`` 的任务可通过 ``/api/tasks?module=video`` 查到（不触发真实下载）。"""
        from app.core.tasks import TaskInput, get_task_manager

        manager = get_task_manager()

        class _FakeOutput:
            expire_at = None

            def to_api(self) -> dict:
                return {
                    "file_id": "f_test_video",
                    "name": "douyin-示例作者-晚风-1080P.mp4",
                    "size": 1024,
                    "mime": "video/mp4",
                    "url": "/api/download/f_test_video",
                }

        record = manager.register_completed(
            module="video",
            operation="download",
            inputs=[
                TaskInput(
                    name="晚风里的一百种蓝",
                    size=0,
                    source="https://www.douyin.com/video/7412345678901234567",
                )
            ],
            output=_FakeOutput(),  # type: ignore[arg-type]
        )
        try:
            res = client.get("/api/tasks", params={"module": "video", "pageSize": 100})
            assert res.status_code == 200, res.text
            data = res.json()
            assert data["total"] >= 1
            hit = next(item for item in data["items"] if item["task_id"] == record.task_id)
            assert hit["module"] == "video"
            assert hit["operation"] == "download"
            assert hit["inputs"][0]["source"] == "https://www.douyin.com/video/7412345678901234567"
        finally:
            manager.delete(record.task_id, purge_output=False)

# ================================================================ 10. 三平台夹具解析


def _load_json_fixture(name: str) -> dict:
    """载入 JSON 夹具。

    Args:
        name: 相对 ``tests/fixtures/video/`` 的路径。

    Returns:
        解析后的字典。
    """
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _load_html_fixture(name: str) -> str:
    """载入 HTML 夹具文本。

    Args:
        name: 相对 ``tests/fixtures/video/`` 的路径。

    Returns:
        页面文本。
    """
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse_ctx(cookie: str | None = None):
    """构造 ``ParseContext``（离线解析上下文）。

    Returns:
        ``ParseContext`` 实例。
    """
    from app.modules.video.adapters.base import ParseContext

    return ParseContext(platform="", cookie=cookie, prefer_quality="1080P", timeout=1.0)


class TestFixtures:
    """三平台夹具解析（自造脱敏样本，全部离线）。"""

    # ------------------------------------------------------------ B站

    def test_bilibili_view_and_playurl_parse(self) -> None:
        """view + playurl 组合解析：字段完整、清晰度按高度归一、封面 https。"""
        from app.modules.video.adapters.bilibili import parse_fixture

        view = _load_json_fixture("bilibili/view_ok.json")
        playurl = _load_json_fixture("bilibili/playurl_ok.json")
        item = parse_fixture(
            {"view": view, "playurl": playurl},
            "https://www.bilibili.com/video/BV1xx411c7mD",
        )

        assert item.platform == "bilibili"
        assert item.item_id == "BV1xx411c7mD"
        assert "示例稿件" in item.title
        assert item.author.name == "示例UP主"
        assert item.author.id == "1000001"
        assert item.duration_sec == 187
        assert item.cover_url is not None and item.cover_url.startswith("https://")
        assert len(item.formats) >= 3
        qualities = {fmt.quality for fmt in item.formats}
        assert {"1080P", "720P", "480P"} <= qualities
        assert "4K" not in qualities
        assert all(fmt.url for fmt in item.formats if not fmt.need_vip)
        assert item.warnings == []

    def test_bilibili_login_required_fixture(self) -> None:
        """未配置 Cookie 时 1080P 标记 need_login + warnings 含 LOGIN_REQUIRED。"""
        from app.modules.video.adapters.bilibili import parse_fixture

        view = _load_json_fixture("bilibili/view_login_1080p.json")
        playurl = _load_json_fixture("bilibili/playurl_login_1080p.json")
        item = parse_fixture(
            {"view": view, "playurl": playurl},
            "https://www.bilibili.com/video/BV1Lg4y1x7mD",
        )

        assert "LOGIN_REQUIRED" in item.warnings
        login_formats = [fmt for fmt in item.formats if fmt.need_login]
        assert login_formats, "1080P 应以 need_login 占位项出现"
        assert all(fmt.url is None for fmt in login_formats)
        # 仍可降级下载 720P
        downloadable = [fmt for fmt in item.formats if fmt.url and not fmt.need_login]
        assert any(fmt.quality == "720P" for fmt in downloadable)

        # 携带 Cookie 时 1080P 不再是「需登录」占位项
        logged_in = parse_fixture(
            {"view": view, "playurl": playurl},
            "https://www.bilibili.com/video/BV1Lg4y1x7mD",
            logged_in=True,
        )
        assert "LOGIN_REQUIRED" not in logged_in.warnings

    def test_bilibili_multipart_fixture(self) -> None:
        """多分P稿件：piece_count 与 ``?p=2`` 长链正确。"""
        from app.modules.video.adapters.bilibili import parse_fixture

        view = _load_json_fixture("bilibili/view_multipart.json")
        item = parse_fixture(
            view, "https://www.bilibili.com/video/BV1mp411c7mD?p=2"
        )
        assert item.piece_count == 2
        assert item.url.endswith("?p=2")

    def test_bilibili_invalid_error_mapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """接口 ``code=-404`` 映射为 ``ITEM_NOT_FOUND``（TC-07）。"""
        from app.core.errors import ItemNotFoundError
        from app.modules.video import net
        from app.modules.video.adapters.bilibili import ADAPTER

        calls = _no_network(monkeypatch)
        view_error = {
            "code": -404,
            "message": "啥都木有",
            "data": None,
        }
        monkeypatch.setattr(
            net,
            "http_get_json",
            lambda *_a, **_k: ("https://api.bilibili.com/x/web-interface/view", view_error, 200),
        )
        with pytest.raises(ItemNotFoundError):
            ADAPTER.parse("https://www.bilibili.com/video/BV1xx411c7mD", _parse_ctx())
        # 完全离线：只经由被替换的 net 层，urlopen 未被调用
        assert calls == []

    # ------------------------------------------------------------ 抖音

    def test_douyin_router_data_video(self) -> None:
        """分享页 ``_ROUTER_DATA`` 解析：标题/作者/清晰度/时长均正确。"""
        from app.modules.video.adapters.douyin import parse_fixture

        html = _load_html_fixture("douyin/router_data_video.html")
        item = parse_fixture(html, "https://www.douyin.com/video/7412345678901234567")

        assert item.platform == "douyin"
        assert item.item_id == "7412345678901234567"
        assert item.title == "晚风里的一百种蓝 #风景 #自造样本"
        assert item.author.name == "示例作者"
        assert item.author.avatar_url and item.author.avatar_url.startswith("https://")
        assert item.duration_sec == 151  # 毫秒夹具换算为秒
        assert len(item.formats) == 2
        assert {fmt.quality for fmt in item.formats} == {"1080P", "720P"}
        assert item.images == []
        assert item.stats == {"like": 1, "comment": 2, "share": 3, "play": 4}

    def test_douyin_render_data_fallback(self) -> None:
        """``window.RENDER_DATA``（URL 编码 JSON）降级解析可用（契约 §4.1）。"""
        from app.modules.video.adapters.douyin import parse_fixture

        html = _load_html_fixture("douyin/render_data.html")
        item = parse_fixture(html, "https://www.douyin.com/video/7412345678901234999")
        assert item.item_id == "7412345678901234999"
        assert item.title.startswith("示例渲染数据样本")
        assert item.formats and item.formats[0].quality == "1080P"

    def test_douyin_images_fixture(self) -> None:
        """图文作品：``images`` 非空、``media_type=image``。"""
        from app.modules.video.adapters.douyin import parse_fixture

        html = _load_html_fixture("douyin/router_data_images.html")
        item = parse_fixture(html, "https://www.douyin.com/note/7412345678901234568")

        assert item.media_type == "image"
        assert len(item.images) == 3
        assert all(image.url.startswith("https://") for image in item.images)
        assert item.formats == []
        assert item.author.name == "示例图文作者"

    def test_douyin_login_required_fixture(self) -> None:
        """需登录/私密作品 → ``LOGIN_REQUIRED``，而不是笼统的解析失败。"""
        from app.core.errors import LoginRequiredError
        from app.modules.video.adapters.douyin import parse_fixture

        html = _load_html_fixture("douyin/router_data_login.html")
        with pytest.raises(LoginRequiredError) as excinfo:
            parse_fixture(html, "https://www.douyin.com/video/7412345678901234569")
        assert excinfo.value.code == "LOGIN_REQUIRED"

    def test_douyin_missing_title_uses_desc(self) -> None:
        """缺标题字段时用 ``desc`` 兜底；空作者名 → 未知作者。"""
        from app.modules.video.adapters.douyin import parse_fixture

        html = _load_html_fixture("douyin/router_data_no_title.html")
        item = parse_fixture(html, "https://www.douyin.com/video/7412345678901234570")
        assert item.title == "只有 desc 字段的作品标题"
        assert item.author.name == "未知作者"
        assert item.formats  # 单流回退

    def test_douyin_empty_item_list_raises_parse_error(self) -> None:
        """``item_list: []``（失效作品）给出明确错误码，而不是空 Item。"""
        from app.core.errors import VideoError
        from app.modules.video.adapters.douyin import parse_fixture

        html = _load_html_fixture("douyin/router_data_empty.html")
        with pytest.raises(VideoError) as excinfo:
            parse_fixture(html, "https://www.douyin.com/video/7400000000000000000")
        assert excinfo.value.code in {"ITEM_NOT_FOUND", "PARSE_FAILED"}

    def test_douyin_og_only_fallback(self) -> None:
        """只有 og:* 的页面走降级链，仍能给出标题/封面/单一格式。"""
        from app.modules.video.adapters.douyin import parse_fixture

        html = _load_html_fixture("douyin/og_only.html")
        item = parse_fixture(html, "https://www.douyin.com/video/7412345678901234571")
        assert item.title == "仅 og 标签的降级样本"
        assert item.cover_url is not None
        assert len(item.formats) == 1
        assert item.parse_source == "html"

    def test_douyin_parse_offline_with_stubbed_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """适配器 ``parse`` 全链路（替换 net 层）离线可跑，且不发真实请求。"""
        from app.modules.video import net
        from app.modules.video.adapters.douyin import ADAPTER

        html = _load_html_fixture("douyin/router_data_video.html")
        calls = _no_network(monkeypatch)
        monkeypatch.setattr(net, "http_get_text", lambda *_a, **_k: ("", html, 200))

        item = ADAPTER.parse("https://www.douyin.com/video/7412345678901234567", _parse_ctx())
        assert item.item_id == "7412345678901234567"
        assert calls == []

    def test_douyin_status_404_maps_to_item_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """分享页 404 → ``ITEM_NOT_FOUND``。"""
        from app.core.errors import ItemNotFoundError
        from app.modules.video import net
        from app.modules.video.adapters.douyin import ADAPTER

        monkeypatch.setattr(net, "http_get_text", lambda *_a, **_k: ("", "", 404))
        with pytest.raises(ItemNotFoundError):
            ADAPTER.parse("https://www.douyin.com/video/7412345678901234567", _parse_ctx())

    # ------------------------------------------------------------ 快手

    def test_kuaishou_apollo_video(self) -> None:
        """``__APOLLO_STATE__`` 解析：ID/标题/作者/清晰度。"""
        from app.modules.video.adapters.kuaishou import parse_fixture

        html = _load_html_fixture("kuaishou/apollo_video.html")
        item = parse_fixture(html, "https://www.kuaishou.com/short-video/3xabcdEfGh")

        assert item.platform == "kuaishou"
        assert item.item_id == "3xabcdEfGh"
        assert item.title == "示例快手作品：从链接到本机 #自造样本"
        assert item.author.name == "示例快手作者"
        assert item.duration_sec == 15  # 15100ms -> 15s
        assert item.formats
        assert item.cover_url is not None

    def test_kuaishou_images_fixture(self) -> None:
        """图集作品：``imageUrls`` 映射为 images。"""
        from app.modules.video.adapters.kuaishou import parse_fixture

        html = _load_html_fixture("kuaishou/apollo_images.html")
        item = parse_fixture(html, "https://www.kuaishou.com/short-video/3ximgAbCdE")
        assert item.media_type == "image"
        assert len(item.images) == 3
        assert item.formats == []

    def test_kuaishou_initial_state_fallback(self) -> None:
        """``__INITIAL_STATE__`` 降级解析可用。"""
        from app.modules.video.adapters.kuaishou import parse_fixture

        html = _load_html_fixture("kuaishou/initial_state.html")
        item = parse_fixture(html, "https://www.kuaishou.com/short-video/3xinitState1")
        assert item.item_id == "3xinitState1"
        assert item.title.startswith("示例快手作品：初始状态降级")
        assert item.formats

    def test_kuaishou_og_only_fallback(self) -> None:
        """只有 og:* 时仍能解析（降级链最后一级）。"""
        from app.modules.video.adapters.kuaishou import parse_fixture

        html = _load_html_fixture("kuaishou/og_only.html")
        item = parse_fixture(html, "https://www.kuaishou.com/short-video/3xog000000")
        assert item.title == "仅 og 标签的快手降级样本"
        assert item.formats

    def test_kuaishou_login_or_parse_error(self) -> None:
        """需登录/会员作品给出明确错误码（``LOGIN_REQUIRED`` 或 ``PARSE_FAILED``）。"""
        from app.core.errors import VideoError
        from app.modules.video.adapters.kuaishou import parse_fixture

        html = _load_html_fixture("kuaishou/apollo_login.html")
        with pytest.raises(VideoError) as excinfo:
            parse_fixture(html, "https://www.kuaishou.com/short-video/3xmemberOnly")
        assert excinfo.value.code in {"LOGIN_REQUIRED", "VIP_REQUIRED", "PARSE_FAILED"}

    def test_kuaishou_empty_apollo_raises(self) -> None:
        """``defaultClient: {}``（失效作品）抛明确错误码。"""
        from app.core.errors import VideoError
        from app.modules.video.adapters.kuaishou import parse_fixture

        html = _load_html_fixture("kuaishou/apollo_empty.html")
        with pytest.raises(VideoError):
            parse_fixture(html, "https://www.kuaishou.com/short-video/3xdeleted00")

    # ------------------------------------------------------------ 全体夹具

    def test_all_fixtures_parse_without_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """遍历全部夹具逐个解析：要么成功，要么抛业务错误；全程零网络请求。"""
        from app.core.errors import VideoError
        from app.modules.video.adapters.bilibili import parse_fixture as bili_parse
        from app.modules.video.adapters.douyin import parse_fixture as dy_parse
        from app.modules.video.adapters.kuaishou import parse_fixture as ks_parse

        calls = _no_network(monkeypatch)

        cases = [
            (bili_parse, _load_json_fixture("bilibili/view_ok.json"),
             "https://www.bilibili.com/video/BV1xx411c7mD"),
            (bili_parse, _load_json_fixture("bilibili/view_invalid.json"),
             "https://www.bilibili.com/video/BV1xx411c7mD"),
            (dy_parse, _load_html_fixture("douyin/router_data_video.html"),
             "https://www.douyin.com/video/7412345678901234567"),
            (dy_parse, _load_html_fixture("douyin/render_data.html"),
             "https://www.douyin.com/video/7412345678901234999"),
            (dy_parse, _load_html_fixture("douyin/og_only.html"),
             "https://www.douyin.com/video/7412345678901234571"),
            (dy_parse, _load_html_fixture("douyin/router_data_empty.html"),
             "https://www.douyin.com/video/7400000000000000000"),
            (ks_parse, _load_html_fixture("kuaishou/apollo_video.html"),
             "https://www.kuaishou.com/short-video/3xabcdEfGh"),
            (ks_parse, _load_html_fixture("kuaishou/initial_state.html"),
             "https://www.kuaishou.com/short-video/3xinitState1"),
            (ks_parse, _load_html_fixture("kuaishou/apollo_empty.html"),
             "https://www.kuaishou.com/short-video/3xdeleted00"),
        ]
        for parser, raw, url in cases:
            with contextlib.suppress(VideoError):
                parser(raw, url)

        assert calls == [], f"夹具解析不应联网：{calls}"

    def test_parse_fixtures_leave_no_temp_files(self) -> None:
        """解析路径不落盘：``storage/temp`` 不新增文件（TC-24 解析侧）。"""
        from app.core.config import STORAGE_DIR
        from app.modules.video.adapters.douyin import parse_fixture as dy_parse

        temp_dir = STORAGE_DIR / "temp"
        before = {p.name for p in temp_dir.iterdir()} if temp_dir.exists() else set()

        dy_parse(
            _load_html_fixture("douyin/router_data_video.html"),
            "https://www.douyin.com/video/7412345678901234567",
        )

        after = {p.name for p in temp_dir.iterdir()} if temp_dir.exists() else set()
        assert after == before, f"解析不应产生临时文件：新增 {after - before}"


# ================================================================ 10.1 B站清晰度回归
# 生产事故（2026-10-20）：前端默认选中的 720P/480P 没有媒体地址 → 下载报
# LOGIN_REQUIRED / DOWNLOAD_FAILED。根因是 DASH 流的 format_id 由「高度反查 qn」
# 生成，老视频高度 384 反查失败后退回 entry["id"]，与 support_formats 补出的
# 占位项撞车；同一 qn 的 3 种编码也共用同一个 id。以下用例锁住修复后的行为。

_DUPLICATE_CODECS_URL = "https://www.bilibili.com/video/BV1xx411c7mD"


def _bilibili_duplicate_codecs_item(*, logged_in: bool = False):
    """解析「同 qn 多编码 + 缺 720P/1080P 流」夹具。

    Args:
        logged_in: 是否按已登录处理。

    Returns:
        ``VideoItem``。
    """
    from app.modules.video.adapters.bilibili import parse_fixture

    return parse_fixture(
        {
            "view": _load_json_fixture("bilibili/view_ok.json"),
            "playurl": _load_json_fixture("bilibili/playurl_duplicate_codecs.json"),
        },
        _DUPLICATE_CODECS_URL,
        logged_in=logged_in,
    )


class TestBilibiliFormatRegression:
    """B站 format_id 冲突 / 不可用占位项 / default_format_id 回归。"""

    def test_dash_codecs_deduped_and_format_ids_unique(self) -> None:
        """同一 qn 的多编码只留 AVC 一条，format_id 全局唯一且跨解析稳定。"""
        item = _bilibili_duplicate_codecs_item()

        ids = [fmt.format_id for fmt in item.formats]
        assert len(ids) == len(set(ids)), f"format_id 必须全局唯一：{ids}"
        assert ids == [fmt.format_id for fmt in _bilibili_duplicate_codecs_item().formats], (
            "format_id 必须跨两次解析稳定（下载任务会重新解析后按 id 回查）"
        )

        # qn 32 给了 codecid 7/12/13 三条，只保留 AVC(7)
        hd = [fmt for fmt in item.formats if fmt.quality == "480P"]
        assert len(hd) == 1, f"480P 只应出现一次，实际 {len(hd)} 次"
        assert hd[0].format_id == "dash-32-7"
        assert hd[0].url and "dup-32-avc" in hd[0].url

        # 每个清晰度只出现一次（编码去重后不再有同名项）
        qualities = [fmt.quality for fmt in item.formats]
        assert len(qualities) == len(set(qualities)), qualities

        sd = [fmt for fmt in item.formats if fmt.quality == "360P"]
        assert len(sd) == 1 and sd[0].format_id == "dash-16-7"

    def test_placeholder_formats_marked_unavailable(self) -> None:
        """720P 占位项 need_login=True / url=None / available=False；真实流 available=True。"""
        item = _bilibili_duplicate_codecs_item()

        placeholders = {
            fmt.format_id: fmt for fmt in item.formats if fmt.quality in {"1080P", "720P"}
        }
        assert set(placeholders) == {"80", "64"}
        p720 = placeholders["64"]
        assert p720.url is None
        assert p720.need_login is True
        assert p720.need_vip is False
        assert p720.note and "登录" in p720.note
        assert p720.to_api()["available"] is False
        assert placeholders["80"].to_api()["available"] is False
        assert "LOGIN_REQUIRED" in item.warnings

        for fmt_id in ("dash-32-7", "dash-16-7"):
            real = item.format_by_id(fmt_id)
            assert real is not None
            assert real.need_login is False
            assert real.to_api()["available"] is True

        # 已登录时占位项不再标 need_login，但仍无地址（available 依旧 False）
        logged_in = _bilibili_duplicate_codecs_item(logged_in=True)
        p720_logged = logged_in.format_by_id("64")
        assert p720_logged is not None
        assert p720_logged.need_login is False
        assert p720_logged.url is None
        assert p720_logged.to_api()["available"] is False
        assert "LOGIN_REQUIRED" not in logged_in.warnings

    def test_pick_default_format_never_picks_unavailable(self) -> None:
        """默认清晰度绝不选中无媒体地址的项（任何偏好下都一样）。"""
        from app.modules.video.adapters.base import pick_default_format

        item = _bilibili_duplicate_codecs_item()
        for prefer in ("1080P", "720P", "480P", "360P", None, ""):
            picked = pick_default_format(item.formats, prefer)
            assert picked is not None, f"偏好 {prefer} 不应返回 None"
            assert picked["url"], f"偏好 {prefer} 选中了无地址项：{picked}"

        # 显式 url 为空 → 跳过；纯 to_api 字典没有 url 键 → 可用性未知，保留
        explicit = [
            {"format_id": "dead", "quality": "1080P", "height": 1080, "url": None},
            {"format_id": "ok", "quality": "480P", "height": 480},
        ]
        picked_explicit = pick_default_format(explicit, None)
        assert picked_explicit is not None and picked_explicit["format_id"] == "ok"

        # 纯 to_api 字典（无 url 键）但显式 available=False → 同样排除
        unavailable = [
            {"format_id": "dead", "quality": "1080P", "height": 1080, "available": False},
            {"format_id": "ok", "quality": "480P", "height": 480, "available": True},
        ]
        picked_unavailable = pick_default_format(unavailable, None)
        assert picked_unavailable is not None and picked_unavailable["format_id"] == "ok"

        plain = [{"format_id": "plain", "quality": "720P", "height": 720}]
        picked_plain = pick_default_format(plain, "720P")
        assert picked_plain is not None and picked_plain["format_id"] == "plain"

    def test_select_format_placeholder_raises_login_required(self) -> None:
        """占位项抛 LoginRequiredError；真实流可下载；旧 format_id 回退到同清晰度真实流。"""
        from dataclasses import replace

        from app.core.errors import DownloadFailedError, LoginRequiredError, VipRequiredError
        from app.modules.video.downloader import select_format
        from app.modules.video.models import VideoFormat

        item = _bilibili_duplicate_codecs_item()

        with pytest.raises(LoginRequiredError):
            select_format(item, "64")

        real = select_format(item, "dash-32-7")
        assert real.url and real.quality == "480P"

        # 旧 format_id "32"（占位项）在同清晰度有真实流时回退到可播放项
        legacy = replace(
            item,
            formats=[
                VideoFormat(
                    format_id="32",
                    quality="480P",
                    height=480,
                    fps=30,
                    need_login=True,
                    url=None,
                ),
                *item.formats,
            ],
        )
        fallback = select_format(legacy, "32")
        assert fallback.format_id == "dash-32-7"
        assert fallback.url

        # 完全没有可播放项、且该档位不需要登录 → DOWNLOAD_FAILED（不再误报登录）
        no_media = replace(
            item,
            formats=[VideoFormat(format_id="999", quality="720P", height=720, fps=30)],
        )
        with pytest.raises(DownloadFailedError) as excinfo:
            select_format(no_media, "999")
        assert "媒体地址" in str(excinfo.value)

        # 会员专属优先报 VIP_REQUIRED
        vip_only = replace(
            item,
            formats=[
                VideoFormat(
                    format_id="120",
                    quality="4K",
                    height=2160,
                    fps=30,
                    need_vip=True,
                    need_login=True,
                )
            ],
        )
        with pytest.raises(VipRequiredError):
            select_format(vip_only, "120")

    def test_legacy_nonstandard_height_does_not_collide(self) -> None:
        """老视频 DASH 高度 384（不在 qn 表）不再与 480P 占位项撞 id。"""
        from dataclasses import replace

        from app.modules.video.adapters.bilibili import parse_fixture
        from app.modules.video.downloader import select_format
        from app.modules.video.models import VideoFormat

        playurl = {
            "accept_quality": [80, 64, 32],
            "support_formats": [{"quality": 80}, {"quality": 64}, {"quality": 32}],
            "dash": {
                "video": [
                    {
                        "id": 32,
                        "height": 384,
                        "width": 640,
                        "bandwidth": 400000,
                        "codecid": 7,
                        "frameRate": "30",
                        "baseUrl": "https://upos-sz-mirrorcos.bilivideo.com/example/legacy-384.m4s",
                    },
                    {
                        "id": 16,
                        "height": 360,
                        "width": 640,
                        "bandwidth": 300000,
                        "codecid": 7,
                        "frameRate": "30",
                        "baseUrl": "https://upos-sz-mirrorcos.bilivideo.com/example/legacy-360.m4s",
                    },
                ],
                "audio": [
                    {
                        "id": 30280,
                        "bandwidth": 320000,
                        "baseUrl": "https://upos-sz-mirrorcos.bilivideo.com/example/legacy-30280.m4s",
                    }
                ],
            },
            "durl": [],
        }
        item = parse_fixture(
            {"view": _load_json_fixture("bilibili/view_ok.json"), "playurl": playurl},
            _DUPLICATE_CODECS_URL,
        )

        ids = [fmt.format_id for fmt in item.formats]
        assert len(ids) == len(set(ids)), ids
        # 老逻辑会生成 "32" 并与 480P 占位项撞车；新逻辑带 qn+codecid 前缀
        assert "32" not in ids
        assert "dash-32-7" in ids

        legacy_stream = item.format_by_id("dash-32-7")
        assert legacy_stream is not None
        assert legacy_stream.height == 384 and legacy_stream.url

        # 同 id 多条时优先返回有地址项（占位项即使排在前面也不影响）
        shadowed = replace(
            item,
            formats=[
                VideoFormat(
                    format_id="dash-32-7",
                    quality="480P",
                    height=480,
                    fps=30,
                    need_login=True,
                    url=None,
                ),
                *item.formats,
            ],
        )
        chosen = shadowed.format_by_id("dash-32-7")
        assert chosen is not None and chosen.url

        # 旧 format_id "32" 下载时回落到真实流，而不是 url=None 的占位项
        picked = select_format(item, "32")
        assert picked.url and picked.format_id == "dash-32-7"

    def test_parse_one_adds_default_format_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``service._parse_one`` 在成功与 LOGIN_REQUIRED 两条路径都给出 default_format_id。"""
        from app.modules.video import service
        from app.modules.video.cache import parse_cache
        from app.modules.video.models import Author, VideoFormat, VideoItem

        parse_cache.clear()
        url = "https://www.bilibili.com/video/BV1dup00000a"
        parsed = _bilibili_duplicate_codecs_item()
        parsed.item_id = "BV1dup00000a"
        parsed.url = url

        class _Adapter:
            def parse(self, _url: str, _ctx):
                return parsed

        monkeypatch.setattr(service.adapters, "get_adapter", lambda _platform: _Adapter())
        try:
            degraded = service._parse_one(url, "bilibili", "1080P")
            assert degraded["ok"] is False
            assert degraded["error"]["code"] == "LOGIN_REQUIRED"
            payload = degraded["item"]
            assert payload["default_format_id"] == "dash-32-7"
            by_id = {fmt["format_id"]: fmt for fmt in payload["formats"]}
            assert by_id[payload["default_format_id"]]["available"] is True
            assert by_id["64"]["available"] is False

            ok_url = "https://www.bilibili.com/video/BV1dup00000b"
            ok_item = VideoItem(
                platform="bilibili",
                item_id="BV1dup00000b",
                url=ok_url,
                title="示例作品",
                author=Author(name="示例作者"),
                formats=[
                    VideoFormat(
                        format_id="dash-80-7",
                        quality="1080P",
                        height=1080,
                        fps=30,
                        url="https://example-cdn.test/v-1080.m4s",
                    )
                ],
            )

            class _OkAdapter:
                def parse(self, _url: str, _ctx):
                    return ok_item

            monkeypatch.setattr(service.adapters, "get_adapter", lambda _platform: _OkAdapter())
            success = service._parse_one(ok_url, "bilibili", "1080P")
            assert success["ok"] is True
            assert success["item"]["default_format_id"] == "dash-80-7"
        finally:
            parse_cache.clear()

    def test_parse_api_item_includes_default_format_id(
        self, client, auth_off, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``POST /api/video/parse`` 的 item 含 default_format_id，且指向 available 的格式。"""
        from app.core.config import get_settings
        from app.modules.video import limiter, service
        from app.modules.video.cache import parse_cache

        calls = _no_network(monkeypatch)
        settings = get_settings()
        original_enabled = settings.video.enabled
        settings.video.enabled = True
        limiter.reset()
        parse_cache.clear()

        url = "https://www.bilibili.com/video/BV1dup0000ab"
        parsed = _bilibili_duplicate_codecs_item()
        parsed.item_id = "BV1dup0000ab"
        parsed.url = url

        class _Adapter:
            def parse(self, _url: str, _ctx):
                return parsed

        monkeypatch.setattr(service.adapters, "get_adapter", lambda _platform: _Adapter())
        try:
            res = client.post("/api/video/parse", json={"urls": [url]})
            assert res.status_code == 200, res.text
            result = res.json()["results"][0]
            payload = result["item"]
            assert payload["default_format_id"], "响应 item 缺少 default_format_id"
            by_id = {fmt["format_id"]: fmt for fmt in payload["formats"]}
            assert payload["default_format_id"] in by_id
            assert by_id[payload["default_format_id"]]["available"] is True
            assert by_id["64"]["available"] is False
        finally:
            limiter.reset()
            parse_cache.clear()
            settings.video.enabled = original_enabled

        assert calls == [], f"解析不应联网：{calls}"

# ================================================================ 11. 契约端点


def _fake_item(platform: str, item_id: str, url: str, *, warnings: list[str] | None = None):
    """构造一个用于替换网络解析结果的 ``VideoItem``（含可选媒体地址）。

    Returns:
        ``VideoItem`` 实例。
    """
    from app.modules.video.models import Author, VideoFormat, VideoItem

    return VideoItem(
        platform=platform,
        item_id=item_id,
        url=url,
        title="示例作品",
        author=Author(name="示例作者", id="1001", avatar_url="https://example-cdn.test/a.jpg"),
        cover_url="https://example-cdn.test/cover.jpg",
        duration_sec=10,
        formats=[
            VideoFormat(
                format_id="v-1080",
                quality="1080P",
                width=1920,
                height=1080,
                fps=30,
                bitrate=2000000,
                container="mp4",
                size_bytes=2500000,
                url="https://example-cdn.test/v-1080.mp4",
            )
        ],
        warnings=list(warnings or []),
        parse_source="api",
    )


class TestApiContract:
    """``/api/video/*`` 契约（TC-06/08/20/21/22/23）。"""

    @pytest.fixture(autouse=True)
    def _reset_video_state(self, client):
        """每个用例前清空限流/熔断与解析缓存，并确保模块开关为开启。

        依赖 ``client`` 保证在其 ``get_settings.cache_clear()`` 之后读取配置单例；
        ``settings.video.enabled`` 可能被本机 ``config.toml`` 关闭（公网部署建议关闭），
        而契约端点的默认断言需要模块处于开启态；快照后恢复，不污染其它用例。
        """
        from app.core.config import get_settings
        from app.modules.video import limiter
        from app.modules.video.cache import parse_cache

        settings = get_settings()
        original_enabled = settings.video.enabled
        settings.video.enabled = True
        limiter.reset()
        parse_cache.clear()
        yield
        limiter.reset()
        parse_cache.clear()
        settings.video.enabled = original_enabled

    def test_platforms_endpoint_shape(self, client, auth_off) -> None:
        """``GET /api/video/platforms``：三平台条目 + 能力矩阵 + Cookie 脱敏状态。"""
        res = client.get("/api/video/platforms")
        assert res.status_code == 200, res.text
        data = res.json()

        for key in ("enabled", "items", "max_urls", "prefer_quality", "prefer_format", "engine"):
            assert key in data, f"platforms 缺少字段 {key}"
        assert isinstance(data["enabled"], bool)
        assert data["max_urls"] >= 1
        assert {item["name"] for item in data["items"]} == {"douyin", "bilibili", "kuaishou"}
        for item in data["items"]:
            assert item["hosts"], f"{item['name']} 缺少 host 白名单"
            assert {"video", "image", "multi_part"} <= set(item["capabilities"])
            assert "configured" in item["cookie"]
            assert "masked" in item["cookie"]

    def test_parse_rejects_non_whitelist_domain(self, client, auth_off) -> None:
        """非白名单域名 → 400 ``PLATFORM_UNSUPPORTED``（TC-08）。"""
        res = client.post("/api/video/parse", json={"urls": ["https://example.com/v/1"]})
        assert res.status_code == 400, res.text
        body = res.json()
        assert body["code"] == "PLATFORM_UNSUPPORTED"
        assert "detail" in body

    def test_parse_rejects_empty_input(self, client, auth_off) -> None:
        """空输入 → 400；错误码为 ``LINK_UNRECOGNIZED``（或校验错误）。"""
        for payload in ({"urls": []}, {"urls": ["", "   "]}):
            res = client.post("/api/video/parse", json=payload)
            assert res.status_code == 400, res.text
            body = res.json()
            assert body["code"] in {"LINK_UNRECOGNIZED", "VALIDATION_ERROR"}

    def test_parse_rejects_too_many_urls(self, client, auth_off) -> None:
        """21 条链接 → 400，文案说明上限为 20。"""
        urls = [f"https://www.bilibili.com/video/BV1xx411c7m{i}" for i in range(21)]
        res = client.post("/api/video/parse", json={"urls": urls})
        assert res.status_code == 400, res.text
        body = res.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert "20" in body["detail"]

    def test_parse_endpoint_returns_per_item_results(
        self, client, auth_off, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """逐条返回：一条成功 + 一条失效 + 一条成功，单条失败不影响整批（TC-06/07）。"""
        from app.core.errors import ItemNotFoundError
        from app.modules.video import service

        douyin_url = "https://www.douyin.com/video/7412345678901234567"
        bilibili_url = "https://www.bilibili.com/video/BV1xx411c7mD"
        kuaishou_url = "https://www.kuaishou.com/short-video/3xabcdEfGh"
        outcomes: dict[str, object] = {
            douyin_url: _fake_item("douyin", "7412345678901234567", douyin_url),
            bilibili_url: ItemNotFoundError("作品不存在或已被作者删除"),
            kuaishou_url: _fake_item("kuaishou", "3xabcdEfGh", kuaishou_url),
        }

        class _Adapter:
            def parse(self, url: str, _ctx):
                outcome = outcomes[url]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        monkeypatch.setattr(service.adapters, "get_adapter", lambda _platform: _Adapter())

        res = client.post("/api/video/parse", json={"urls": [douyin_url, bilibili_url, kuaishou_url]})
        assert res.status_code == 200, res.text
        data = res.json()
        assert "elapsed_ms" in data and isinstance(data["elapsed_ms"], int)

        results = data["results"]
        assert len(results) == 3
        assert [item["ok"] for item in results] == [True, False, True]
        assert results[1]["error"]["code"] == "ITEM_NOT_FOUND"
        assert results[0]["item"]["platform"] == "douyin"
        assert results[0]["item"]["formats"][0]["quality"] == "1080P"
        assert results[2]["item"]["item_id"] == "3xabcdEfGh"

    def test_parse_login_required_keeps_item(
        self, client, auth_off, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``LOGIN_REQUIRED`` 时 ``ok=false`` 但仍返回 item（前端可降级下载）。"""
        from app.modules.video import service

        url = "https://www.bilibili.com/video/BV1Lg4y1x7mD"
        item = _fake_item("bilibili", "BV1Lg4y1x7mD", url, warnings=["LOGIN_REQUIRED"])

        class _Adapter:
            def parse(self, _url: str, _ctx):
                return item

        monkeypatch.setattr(service.adapters, "get_adapter", lambda _platform: _Adapter())

        res = client.post("/api/video/parse", json={"urls": [url]})
        assert res.status_code == 200, res.text
        result = res.json()["results"][0]
        assert result["ok"] is False
        assert result["error"]["code"] == "LOGIN_REQUIRED"
        assert result["item"]["item_id"] == "BV1Lg4y1x7mD"
        assert "LOGIN_REQUIRED" in result["item"]["warnings"]

    def test_history_endpoint_pagination(self, client, auth_off) -> None:
        """``GET /api/video/history`` 分页字段与筛选参数可用。"""
        from app.modules.video import history

        token = "api-history-测试"
        store = history.history_store
        for index in range(3):
            store.record(platform="douyin", item_id=f"api-{index}", title=f"{token}{index}", file_id=None)
        try:
            res = client.get(
                "/api/video/history",
                params={"keyword": token, "page": 1, "pageSize": 2},
            )
            assert res.status_code == 200, res.text
            data = res.json()
            for key in ("total", "page", "page_size", "items"):
                assert key in data
            assert data["total"] == 3
            assert data["page"] == 1 and data["page_size"] == 2
            assert len(data["items"]) == 2
            assert all(token in item["title"] for item in data["items"])
        finally:
            for index in range(3):
                store.remove(f"api-{index}", platform="douyin")

    def test_history_delete_endpoints(self, client, auth_off) -> None:
        """单条删除 / 清空返回 ``{"ok": true, "removed": n}``。"""
        from app.modules.video import history

        store = history.history_store
        store.record(platform="kuaishou", item_id="api-del-1", title="待删除", file_id=None)
        res = client.delete("/api/video/history/api-del-1", params={"platform": "kuaishou"})
        assert res.status_code == 200, res.text
        assert res.json()["removed"] == 1

        store.record(platform="kuaishou", item_id="api-del-2", title="待清空", file_id=None)
        res = client.delete("/api/video/history")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["ok"] is True and isinstance(body["removed"], int) and body["removed"] >= 1

    def test_settings_endpoint_masks_cookie(
        self, client, auth_off, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        video_settings, credentials_clean: Path,
    ) -> None:
        """``GET/PUT /api/video/settings`` 往返，Cookie 只回显脱敏值（契约 §3.5）。"""
        from app.modules.video import settings_store

        # 写回目标指向临时文件，避免污染仓库 config.toml
        monkeypatch.setattr(settings_store, "CONFIG_FILE", tmp_path / "config.toml")

        res = client.get("/api/video/settings")
        assert res.status_code == 200, res.text
        for key in (
            "enabled",
            "prefer_quality",
            "prefer_format",
            "max_concurrent",
            "download_segments",
            "max_file_mb",
            "platforms",
        ):
            assert key in res.json(), f"settings 缺少字段 {key}"

        put = client.put(
            "/api/video/settings",
            json={
                "prefer_quality": "720P",
                "max_concurrent": 2,
                "cookies": {"bilibili": "SESSDATA=abcdefgh9f2a; bili_jct=PLAINTEXTSECRET"},
            },
        )
        assert put.status_code == 200, put.text
        body = put.json()
        assert body["prefer_quality"] == "720P"
        cookie = body["platforms"]["bilibili"]["cookie"]
        assert cookie["configured"] is True
        assert cookie["masked"] == "SESSDATA=****9f2a"
        text = json.dumps(body, ensure_ascii=False)
        assert "PLAINTEXTSECRET" not in text
        assert "abcdefgh9f2a" not in text

        again = client.get("/api/video/settings")
        assert again.json()["platforms"]["bilibili"]["cookie"]["masked"] == "SESSDATA=****9f2a"

    def test_download_rejects_empty_and_bad_platform(self, client, auth_off) -> None:
        """下载端点先做请求校验：空列表 400、非白名单域名 400 ``PLATFORM_UNSUPPORTED``。"""
        empty = client.post("/api/video/download", json={"items": []})
        assert empty.status_code == 400, empty.text
        assert empty.json()["code"] == "VALIDATION_ERROR"

        bad = client.post(
            "/api/video/download",
            json={"items": [{"url": "https://example.com/v/1", "output": "mp4"}]},
        )
        assert bad.status_code == 400, bad.text
        assert bad.json()["code"] == "PLATFORM_UNSUPPORTED"

        # 平台与链接不匹配：链接是抖音，却声明 bilibili
        mismatch = client.post(
            "/api/video/download",
            json={
                "items": [
                    {
                        "url": "https://www.douyin.com/video/7412345678901234567",
                        "platform": "bilibili",
                        "output": "mp4",
                    }
                ]
            },
        )
        assert mismatch.status_code == 400, mismatch.text
        assert mismatch.json()["code"] == "PLATFORM_UNSUPPORTED"

    def test_module_disabled_returns_service_disabled(self, client) -> None:
        """``[video] enabled=false`` → 所有端点 503 ``SERVICE_DISABLED``（TC-21）。"""
        from app.core.config import get_settings

        settings = get_settings()
        original = settings.video.enabled
        settings.video.enabled = False
        try:
            for method, path, payload in (
                ("GET", "/api/video/platforms", None),
                ("GET", "/api/video/settings", None),
                ("POST", "/api/video/parse", {"urls": ["https://www.bilibili.com/video/BV1xx411c7mD"]}),
            ):
                res = client.request(method, path, json=payload)
                assert res.status_code == 503, f"{method} {path} -> {res.status_code}"
                assert res.json()["code"] == "SERVICE_DISABLED"
        finally:
            settings.video.enabled = original

    def test_video_endpoints_require_auth(self, client) -> None:
        """开启鉴权且未带 Token 时全部 401 ``UNAUTHORIZED``（TC-22）。"""
        from app.core.config import get_settings

        settings = get_settings()
        original = settings.server.auth_required
        settings.server.auth_required = True
        try:
            for method, path in (
                ("GET", "/api/video/platforms"),
                ("GET", "/api/video/settings"),
                ("GET", "/api/video/history"),
                ("POST", "/api/video/download"),
            ):
                res = client.request(method, path)
                assert res.status_code == 401, f"{method} {path} -> {res.status_code}"
                body = res.json()
                assert "detail" in body and "code" in body
        finally:
            settings.server.auth_required = original


class TestMediaHeaders:
    """媒体防盗链请求头（2026-10-20 生产验收发现的 B站 CDN 403 回归）。

    B站媒体 CDN 同时校验 Referer 与 UA：缺任一项都返回 403。这里的用例锁住
    「平台 → 媒体请求头」映射以及「下载器把请求头透传到 net 层」两条链路，
    避免以后重构时悄悄丢掉 Referer 导致线上下载全红。
    """

    def test_platform_media_headers_have_referer_and_ua(self) -> None:
        """三平台都必须带 Referer 与 UA，未知平台返回空字典。"""
        from app.modules.video.adapters import registry as registry_mod

        for name in ("bilibili", "douyin", "kuaishou"):
            headers = registry_mod.media_headers(name)
            assert headers.get("Referer", "").startswith("https://"), f"{name} 缺少 Referer"
            assert headers.get("User-Agent"), f"{name} 缺少 User-Agent"
        assert registry_mod.media_headers(None) == {}
        assert registry_mod.media_headers("unknown") == {}
        # 返回副本：调用方修改不能污染全局映射
        mutating = registry_mod.media_headers("bilibili")
        mutating["Referer"] = "https://evil.example/"
        assert registry_mod.media_headers("bilibili")["Referer"] == "https://www.bilibili.com/"

    def test_net_download_range_forwards_headers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``download_range`` 必须把平台请求头原样交给底层请求。"""
        from app.modules.video import net

        captured: dict[str, object] = {}

        class _FakeResponse:
            def read(self, _size: int = -1) -> bytes:
                return b""

            def close(self) -> None:
                return None

        def fake_open(url: str, *, range_start=None, range_end=None, timeout=30.0, headers=None):
            captured["headers"] = headers
            return _FakeResponse(), 206, {}

        monkeypatch.setattr(net, "open_stream", fake_open)
        media_headers = {"Referer": "https://www.bilibili.com/", "User-Agent": "UA/1.0"}
        net.download_range(
            "https://example.com/v.mp4", tmp_path / "a.bin", start=0, end=1, headers=media_headers
        )
        assert captured["headers"] == media_headers

    def test_probe_media_forwards_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``probe_media`` 的 HEAD/Range 探测同样要带平台请求头。"""
        from app.modules.video import net

        captured: dict[str, object] = {}

        def fake_head(url: str, *, timeout: float = 10.0, headers=None):
            captured["head"] = headers
            return 404, {}

        class _FakeResponse:
            def read(self, _size: int = -1) -> bytes:
                return b""

            def close(self) -> None:
                return None

        def fake_open(url: str, *, range_start=None, range_end=None, timeout=30.0, headers=None):
            captured["range"] = headers
            return _FakeResponse(), 206, {"content-range": "bytes 0-0/1024"}

        monkeypatch.setattr(net, "http_head", fake_head)
        monkeypatch.setattr(net, "open_stream", fake_open)
        media_headers = {"Referer": "https://www.bilibili.com/"}
        probe = net.probe_media("https://example.com/v.mp4", headers=media_headers)
        assert probe.total_size == 1024
        assert captured["head"] == media_headers
        assert captured["range"] == media_headers
