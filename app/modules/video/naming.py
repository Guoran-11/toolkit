"""产物命名：模板拼接、字符清洗、长度截断与重名去重。

契约 §4.5：``{platform}-{author}-{title}-{quality}.{ext}``，清洗 Windows 非法字符与
控制字符、压缩空白、总长 ≤120 字符；空作者 ``未知作者``、空标题 ``未命名作品``；
同名追加 ``-1`` / ``-2``。

重名判断依据 ``core.storage.registry`` 中**尚未过期**的产物名：产物 TTL 到期后
registry 会自行清理，此时同名文件可以复用原名称，不需要无意义地追加序号。
"""

from __future__ import annotations

import re
import threading
from typing import Final

from app.core.storage import registry

#: 产物名最大长度（含扩展名），契约 §4.5
MAX_NAME_LEN: Final[int] = 120

# Windows 保留字符 + C0 控制字符 + DEL
_ILLEGAL_RE: Final[re.Pattern[str]] = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
# emoji / 变体选择符 / 零宽连接符：部分播放器与压缩工具处理异常，统一剔除
_EMOJI_RE: Final[re.Pattern[str]] = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f\u200d\u20e3]"
)
_WS_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

_lock = threading.RLock()


def _clean(part: str | None, fallback: str) -> str:
    """清洗单个命名片段。

    Args:
        part: 原始片段（作者、标题等）。
        fallback: 清洗后为空时的兜底文案。

    Returns:
        不含非法字符、空白已压缩的片段。
    """
    text = _ILLEGAL_RE.sub("", str(part or ""))
    text = _EMOJI_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip(" .")
    return text or fallback


def build_name(
    *,
    platform: str,
    author: str | None,
    title: str | None,
    quality: str | None,
    ext: str,
) -> str:
    """按模板构造产物名。

    截断策略：先压缩作者与平台，再按剩余预算截断标题——标题是三者中最长、信息量
    最低的部分，优先保作者与清晰度能让用户一眼看出文件来源。

    Args:
        platform: 平台标识（douyin/bilibili/kuaishou）。
        author: 作者名，可空。
        title: 作品标题，可空。
        quality: 归一化清晰度，可空。
        ext: 扩展名（可带点，如 ``.mp4``）。

    Returns:
        长度 ≤120 且不含非法字符的文件名。
    """
    clean_platform = _clean(platform, "video")[:20]
    clean_author = _clean(author, "未知作者")
    clean_title = _clean(title, "未命名作品")
    clean_quality = _clean(quality, "unknown")[:16]
    clean_ext = _clean(ext, "bin").lstrip(".").lower()[:8] or "bin"

    suffix = f"-{clean_quality}.{clean_ext}"
    # 三个分隔符：platform-author-title-quality.ext
    fixed = len(clean_platform) + len(suffix) + 3
    budget = MAX_NAME_LEN - fixed
    if budget < 4:
        # 极端场景（作者/清晰度都异常长）：优先保住标题与扩展名
        clean_author = clean_author[:12]
        clean_quality = clean_quality[:8]
        suffix = f"-{clean_quality}.{clean_ext}"
        fixed = len(clean_platform) + len(suffix) + 3
        budget = max(1, MAX_NAME_LEN - fixed)

    if len(clean_author) > budget // 2 and len(clean_author) > 12:
        clean_author = clean_author[: max(12, budget // 2)]
        fixed = len(clean_platform) + len(clean_author) + len(suffix) + 3
        budget = MAX_NAME_LEN - fixed

    clean_title = clean_title[: max(1, budget)]
    name = f"{clean_platform}-{clean_author}-{clean_title}{suffix}"
    if len(name) > MAX_NAME_LEN:
        name = name[:MAX_NAME_LEN]
    return name


def dedupe_name(name: str) -> str:
    """返回不与现有产物重名的新文件名（同名追加 ``-1`` / ``-2``）。

    Args:
        name: 已清洗的文件名（含扩展名）。

    Returns:
        可安全落盘的文件名。
    """
    with _lock:
        existing = {record.name.lower() for record in registry.list_all()}
        if name.lower() not in existing:
            return name

        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        index = 1
        while True:
            candidate = f"{stem}-{index}.{ext}" if ext else f"{stem}-{index}"
            if candidate.lower() not in existing:
                return candidate
            index += 1


def mime_for_ext(ext: str) -> str:
    """取扩展名对应的 MIME 类型。

    Args:
        ext: 扩展名（可带点）。

    Returns:
        产物 MIME 字符串。
    """
    clean = ext.lstrip(".").lower()
    return {
        "mp4": "video/mp4",
        "webm": "video/webm",
        "mkv": "video/x-matroska",
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "zip": "application/zip",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(clean, "application/octet-stream")


__all__ = ["MAX_NAME_LEN", "build_name", "dedupe_name", "mime_for_ext"]
