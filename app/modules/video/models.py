"""视频下载模块的领域数据模型。

契约（``docs/视频下载模块-实现契约.md`` §3.2 / §5.1）要求 ``to_api()`` 输出固定字段：
``formats[]`` 的每个字段都必须存在（缺省用 ``None`` / ``false``），``images`` 与
``warnings`` 恒为数组。前端直接把这些字段绑定到 DOM，缺键会渲染成 ``undefined``。

设计取舍：
    1. 用 ``dataclass(slots=True)`` 而非 Pydantic 模型——这些对象只用于模块内部流转与
       JSON 输出，不需要入参校验；slots 还能避免拼错字段名时静默新增属性。
    2. ``VideoFormat.url`` / ``audio_url`` 是**内部下载字段**，故意不出现在 ``to_api()``：
       媒体直链有时效且可能携带签名，契约明确要求响应里不暴露（历史记录同理）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Author:
    """作品作者信息。

    Attributes:
        name: 昵称（前端主展示字段）。
        id: 平台内作者 ID（B站 mid / 抖音 sec_uid / 快手 userId）。
        avatar_url: 头像地址；平台未返回时为 None。
    """

    name: str = "未知作者"
    id: str | None = None
    avatar_url: str | None = None

    def to_api(self) -> dict[str, Any]:
        """转换为前端结构。

        Returns:
            含 ``name`` / ``id`` / ``avatar_url`` 的字典。
        """
        return {"name": self.name, "id": self.id, "avatar_url": self.avatar_url}


@dataclass(slots=True)
class Image:
    """图文作品中的单张图片。

    Attributes:
        url: 图片直链（有时效，仅用于本次下载）。
        width: 像素宽。
        height: 像素高。
    """

    url: str
    width: int | None = None
    height: int | None = None

    def to_api(self) -> dict[str, Any]:
        """转换为前端结构。

        Returns:
            含 ``url`` / ``width`` / ``height`` 的字典。
        """
        return {"url": self.url, "width": self.width, "height": self.height}


@dataclass(slots=True)
class VideoFormat:
    """单个清晰度（媒体流）的描述。

    Attributes:
        format_id: 平台原始格式标识，下载时原样回传。
        quality: 归一化等级（4K/2K/1080P60/1080P/720P/540P/480P/360P/audio）。
        width: 像素宽。
        height: 像素高。
        fps: 帧率。
        bitrate: 码率（bps）。
        container: 容器扩展名（mp4/m4a/webm/flv…）。
        size_bytes: 预估体积；未知为 None。
        need_login: 该清晰度是否需要登录态。
        need_vip: 该清晰度是否会员专属（只展示、不下载）。
        expire_at: 媒体地址过期时间（ISO 8601）。
        note: 平台侧画质备注（如「与 1080P 等宽高、码率更高」）。
        url: **内部字段**：视频流地址（不对外输出）。
        audio_url: **内部字段**：DASH 分离音频流地址（不对外输出）。
    """

    format_id: str
    quality: str = "unknown"
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    bitrate: int | None = None
    container: str = "mp4"
    size_bytes: int | None = None
    need_login: bool = False
    need_vip: bool = False
    expire_at: str | None = None
    note: str | None = None
    url: str | None = None
    audio_url: str | None = None

    def to_api(self) -> dict[str, Any]:
        """转换为前端结构（严格只输出契约字段）。

        ``available`` 是「该清晰度是否真的拿到了媒体地址」的布尔信号：平台
        只给出档位、未返回流地址的占位项为 ``False``，前端据此置灰且不默认选中。

        Returns:
            含 §3.2 约定全部字段（外加 ``available``）的字典。
        """
        return {
            "format_id": self.format_id,
            "quality": self.quality,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "bitrate": self.bitrate,
            "container": self.container,
            "size_bytes": self.size_bytes,
            "need_login": self.need_login,
            "need_vip": self.need_vip,
            "expire_at": self.expire_at,
            "note": self.note,
            "available": bool(self.url),
        }


@dataclass(slots=True)
class VideoItem:
    """一个作品（视频 / 图文 / 混合）的完整元数据。

    Attributes:
        platform: 平台标识（douyin/bilibili/kuaishou）。
        item_id: 平台内唯一 ID（去重键）。
        url: 归一化后的长链。
        media_type: ``video`` / ``image`` / ``mix``。
        title: 标题或文案首行（≤200 字）。
        description: 完整文案（≤2000 字）。
        author: 作者信息。
        cover_url: 封面地址。
        duration_sec: 时长（秒，图文为 0）。
        published_at: 发布时间（ISO 8601）。
        stats: 互动数据 ``{like, comment, share, play}``。
        formats: 清晰度列表（图文作品可为空）。
        images: 图文作品图片列表。
        piece_count: 多 P / 合集集数。
        warnings: 降级提示码（如 ``LOGIN_REQUIRED``）。
        parse_source: ``api`` / ``html`` / ``ytdlp``（排障用，前端不展示）。
    """

    platform: str
    item_id: str
    url: str
    media_type: str = "video"
    title: str = ""
    description: str = ""
    author: Author = field(default_factory=Author)
    cover_url: str | None = None
    duration_sec: int = 0
    published_at: str | None = None
    stats: dict[str, int] | None = None
    formats: list[VideoFormat] = field(default_factory=list)
    images: list[Image] = field(default_factory=list)
    piece_count: int = 1
    warnings: list[str] = field(default_factory=list)
    parse_source: str = "html"

    def to_api(self) -> dict[str, Any]:
        """转换为 §3.2 约定的 item 结构。

        Returns:
            字段齐全的字典；``images`` / ``warnings`` 恒为数组。
        """
        return {
            "platform": self.platform,
            "item_id": self.item_id,
            "url": self.url,
            "media_type": self.media_type,
            "title": self.title,
            "description": self.description,
            "author": self.author.to_api(),
            "cover_url": self.cover_url,
            "duration_sec": self.duration_sec,
            "published_at": self.published_at,
            "stats": self.stats,
            "formats": [fmt.to_api() for fmt in self.formats],
            "images": [image.to_api() for image in self.images],
            "piece_count": self.piece_count,
            "warnings": list(self.warnings),
            "parse_source": self.parse_source,
        }

    def format_by_id(self, format_id: str | None) -> VideoFormat | None:
        """按 ``format_id`` 查找清晰度。

        同一 ``format_id`` 可能对应多条记录（平台重复给流、或占位项与真实流历史
        遗留的同名 id）。此时优先返回**可播放**项（有媒体地址且非会员专属），
        只有全都不可播放时才退回第一条——否则下载器会拿到 ``url=None`` 的占位项。

        Args:
            format_id: 平台格式标识；None 表示未指定。

        Returns:
            命中的 VideoFormat；未命中返回 None。
        """
        if not format_id:
            return None
        matched = [fmt for fmt in self.formats if fmt.format_id == format_id]
        if not matched:
            return None
        for fmt in matched:
            if fmt.url and not fmt.need_vip:
                return fmt
        return matched[0]

    def has_playable_video(self) -> bool:
        """是否存在可直接下载的视频流。

        Returns:
            任一视频清晰度带有媒体地址且不是会员专属时返回 True。
        """
        return any(fmt.url and not fmt.need_vip for fmt in self.formats)


__all__ = ["Author", "Image", "VideoFormat", "VideoItem"]
