"""视频模块设置的读取、校验与写回。

契约 §3.5 / §5.5：
    · ``GET /api/video/settings`` 返回脱敏结构（Cookie 只给 ``configured/masked/updated_at``）；
    · ``PUT`` 支持部分字段，Cookie 空字符串表示清除；
    · 标量设置写回 ``config.toml`` 的 ``[video]`` 段：逐行替换 + 追加，**保留注释**；
      不使用 toml 序列化重写整文件（会丢失用户注释与排版）。

配置单例是 ``lru_cache`` 对象，因此写回后**就地修改** ``get_settings().video`` 的字段，
进程内立即生效。这里刻意不调用 ``get_settings.cache_clear()``：那会连同鉴权、引擎路径等
其它配置一起重置，用户刚保存的设置可能被意外覆盖，且会重置已缓存的引擎探测结果。
"""

from __future__ import annotations

import logging
from typing import Any, Final

from app.core.config import CONFIG_FILE, PlatformToggle, get_settings
from app.core.errors import ValidationError
from app.modules.video import credentials

logger = logging.getLogger(__name__)

_KNOWN_PLATFORMS: Final[tuple[str, ...]] = ("douyin", "bilibili", "kuaishou")
_VALID_QUALITIES: Final[frozenset[str]] = frozenset({"4K", "1080P", "720P", "480P"})
_VALID_FORMATS: Final[frozenset[str]] = frozenset({"mp4", "mp3", "m4a"})


def _validate_int(name: str, value: Any, low: int, high: int) -> int:
    """校验整数范围。

    Args:
        name: 字段名（错误文案用）。
        value: 待校验值。
        low: 下限（含）。
        high: 上限（含）。

    Returns:
        校验后的整数。

    Raises:
        ValidationError: 不是整数或越界。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"设置项 {name} 必须是整数")
    if not low <= value <= high:
        raise ValidationError(f"设置项 {name} 必须在 {low}–{high} 之间")
    return value


def _validate_bool(name: str, value: Any) -> bool:
    """校验布尔值。

    Args:
        name: 字段名。
        value: 待校验值。

    Returns:
        布尔值。

    Raises:
        ValidationError: 类型不符。
    """
    if not isinstance(value, bool):
        raise ValidationError(f"设置项 {name} 必须是布尔值")
    return value


def read_video_settings() -> dict[str, Any]:
    """读取视频模块设置（Cookie 已脱敏）。

    Returns:
        契约 §3.5 结构；额外附带上限/超时等排障字段（只追加，不改名）。
    """
    video = get_settings().video
    status = credentials.cookie_status()
    platforms: dict[str, Any] = {}
    for name in _KNOWN_PLATFORMS:
        cookie = status.get(name) or {"configured": False, "masked": None, "updated_at": None}
        platforms[name] = {
            "enabled": bool(video.platform_enabled(name)),
            "cookie": {
                "configured": bool(cookie.get("configured")),
                "masked": cookie.get("masked"),
                "updated_at": cookie.get("updated_at"),
            },
        }

    return {
        "enabled": bool(video.enabled),
        "prefer_quality": video.prefer_quality,
        "prefer_format": video.prefer_format,
        "max_concurrent": int(video.max_concurrent),
        "download_segments": int(video.download_segments),
        "max_file_mb": int(video.max_file_mb),
        "platforms": platforms,
        # 以下为追加字段（前端设置弹层/排障可读，非契约强制）
        "segment_size_mb": int(video.segment_size_mb),
        "task_timeout_sec_video": int(video.task_timeout_sec_video),
        "dedupe_window_hours": int(video.dedupe_window_hours),
        "min_free_gb": int(video.min_free_gb),
        "max_urls": int(video.max_urls),
    }


def update_video_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """更新视频模块设置（部分字段）。

    Args:
        patch: 请求体字段字典。

    Returns:
        更新后的完整设置（同 :func:`read_video_settings`）。

    Raises:
        ValidationError: 任一字段取值非法。
    """
    if not isinstance(patch, dict):
        raise ValidationError("设置内容必须是一个对象")

    video = get_settings().video
    updates: dict[str, Any] = {}
    cookie_updates: dict[str, str] = {}
    platform_updates: dict[str, bool] = {}

    if "enabled" in patch:
        enabled = _validate_bool("enabled", patch["enabled"])
        video.enabled = enabled
        updates["enabled"] = enabled

    if "prefer_quality" in patch:
        quality = patch["prefer_quality"]
        if quality not in _VALID_QUALITIES:
            raise ValidationError("默认清晰度只能是 4K / 1080P / 720P / 480P")
        video.prefer_quality = quality
        updates["prefer_quality"] = quality

    if "prefer_format" in patch:
        output_format = patch["prefer_format"]
        if output_format not in _VALID_FORMATS:
            raise ValidationError("默认输出格式只能是 mp4 / mp3 / m4a")
        video.prefer_format = output_format
        updates["prefer_format"] = output_format

    if "max_concurrent" in patch:
        value = _validate_int("max_concurrent", patch["max_concurrent"], 1, 4)
        video.max_concurrent = value
        updates["max_concurrent"] = value

    if "download_segments" in patch:
        value = _validate_int("download_segments", patch["download_segments"], 1, 8)
        video.download_segments = value
        updates["download_segments"] = value

    if "max_file_mb" in patch:
        value = _validate_int("max_file_mb", patch["max_file_mb"], 1, 10240)
        video.max_file_mb = value
        updates["max_file_mb"] = value

    if "segment_size_mb" in patch:
        value = _validate_int("segment_size_mb", patch["segment_size_mb"], 1, 32)
        video.segment_size_mb = value
        updates["segment_size_mb"] = value

    if "platforms" in patch:
        raw_platforms = patch["platforms"]
        if not isinstance(raw_platforms, dict):
            raise ValidationError("platforms 必须是对象")
        for name, value in raw_platforms.items():
            if name not in _KNOWN_PLATFORMS:
                raise ValidationError(f"未知平台：{name}")
            if isinstance(value, dict):
                enabled = _validate_bool(f"platforms.{name}.enabled", value.get("enabled", True))
            else:
                enabled = _validate_bool(f"platforms.{name}", value)
            platform_updates[name] = enabled

    if "cookies" in patch:
        raw_cookies = patch["cookies"]
        if not isinstance(raw_cookies, dict):
            raise ValidationError("cookies 必须是对象")
        for name, value in raw_cookies.items():
            if name not in _KNOWN_PLATFORMS:
                raise ValidationError(f"未知平台：{name}")
            if not isinstance(value, str):
                raise ValidationError(f"平台 {name} 的 Cookie 必须是字符串")
            cookie_updates[name] = value

    # 先落 Cookie（独立文件），再写 TOML，最后才视为成功
    for name, cookie in cookie_updates.items():
        credentials.save_cookie(name, cookie)

    for name, enabled in platform_updates.items():
        video.platforms[name] = PlatformToggle(enabled=enabled)
        updates[f"platforms.{name}"] = {"enabled": enabled}

    if updates:
        persist_to_toml(updates)

    return read_video_settings()


def persist_to_toml(updates: dict[str, Any]) -> None:
    """把设置写回 ``config.toml`` 的 ``[video]`` 段（保留注释）。

    逐行策略：
        1. 找到 ``[video]`` 段（含其后的子段边界）；
        2. 已有键 → 整行替换为新值；缺失 → 追加到段尾（保留段尾空行）；
        3. 文件只读时只记 warning，内存设置已经生效，不阻断用户操作。

    Args:
        updates: ``键 -> 值``；键支持 ``platforms.douyin`` 形式的点号路径。
    """
    if not updates:
        return

    if not CONFIG_FILE.exists():
        lines = ["[video]"]
        for key, value in updates.items():
            lines.append(_format_line(key, value))
        try:
            CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("视频设置写回 config.toml 失败（内存已生效）：%s", exc)
        return

    try:
        lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("读取 config.toml 失败（内存已生效）：%s", exc)
        return

    has_platform_section = any(line.strip() == "[video.platforms]" for line in lines)
    for key, value in updates.items():
        if key.startswith("platforms.") and has_platform_section:
            section = "[video.platforms]"
            section_key = key.split(".", 1)[1]
        else:
            section = "[video]"
            section_key = key
        lines = _upsert_in_section(lines, section, section_key, value)

    try:
        CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("视频设置写回 config.toml 失败（内存已生效）：%s", exc)


def _format_line(key: str, value: Any) -> str:
    """把键值格式化为一行 TOML。

    Args:
        key: 键（可含点号）。
        value: 值（bool / int / str / dict）。

    Returns:
        形如 ``max_concurrent = 2`` 的文本。
    """
    return f"{key} = {_format_value(value)}"


def _format_value(value: Any) -> str:
    """把 Python 值格式化为 TOML 字面量。

    Args:
        value: 待格式化值。

    Returns:
        TOML 字面量字符串。

    Raises:
        TypeError: 不支持的类型（调用方只传内部值，正常不会触发）。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, dict):
        inner = ", ".join(f"{k} = {_format_value(v)}" for k, v in value.items())
        return f"{{ {inner} }}"
    raise TypeError(f"不支持的配置值类型：{type(value).__name__}")


def _upsert_in_section(lines: list[str], section: str, key: str, value: Any) -> list[str]:
    """在指定 TOML 段内替换或追加一行。

    Args:
        lines: 文件行列表（会被就地修改并返回）。
        section: 段头（含方括号）。
        key: 段内键名。
        value: 新值。

    Returns:
        处理后的行列表。
    """
    new_line = _format_line(key, value)
    start: int | None = None
    for index, raw in enumerate(lines):
        if raw.strip() == section:
            start = index
            break

    if start is None:
        # 段不存在：追加整个段（前面留一个空行，保持可读性）
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(section)
        lines.append(new_line)
        return lines

    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break

    prefix = f"{key} "
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 精确匹配键名，避免 ``max_concurrent`` 误命中 ``max_concurrent_xxx``
        if stripped == key or stripped.startswith(prefix) or stripped.startswith(f"{key}="):
            lines[index] = new_line
            return lines

    insert_at = end
    # 跳过段尾空行，把新行插在最后一个有效行之后
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, new_line)
    return lines


__all__ = ["persist_to_toml", "read_video_settings", "update_video_settings"]
