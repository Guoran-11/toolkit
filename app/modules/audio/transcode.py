"""音频转码（FFmpeg）。

流程：解封装还原出的中间音频 → FFmpeg 转码 → mutagen 回写标签与封面。

为什么元数据用 mutagen 而不是 FFmpeg 的 ``-metadata``：
    FFmpeg 的元数据参数走命令行，中文标题、特殊符号在 Windows 上极易因
    控制台代码页而乱码，且封面需要用 ``-map`` 精确指定流，参数组合脆弱。
    mutagen 直接操作容器内的标签结构，对 UTF-8 与封面（APIC / Picture / covr）
    的处理是明确且可控的。FFmpeg 只负责最擅长的事：解码与编码。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from app.core.engines import ffmpeg_path, run_process
from app.core.errors import ConversionFailedError, EngineUnavailableError
from app.core.tasks import JobContext

logger = logging.getLogger(__name__)

# 输出格式定义：扩展名 → (FFmpeg 编码器参数, 是否支持比特率)
OUTPUT_FORMATS: Final[dict[str, dict[str, Any]]] = {
    # 规格书 FR-AU-03：MP4 指「AAC 音频轨道容器」。
    # 行业惯例是该场景使用 .m4a 扩展名（相同容器，播放器识别率更高），
    # 因此 mp4 与 m4a 共用一套参数，仅扩展名不同。
    "mp4": {"suffix": "m4a", "args": ["-c:a", "aac"], "bitrate": True, "label": "MP4（AAC 音频）"},
    "m4a": {"suffix": "m4a", "args": ["-c:a", "aac"], "bitrate": True, "label": "M4A（AAC 音频）"},
    "mp3": {"suffix": "mp3", "args": ["-c:a", "libmp3lame"], "bitrate": True, "label": "MP3"},
    "flac": {
        "suffix": "flac",
        "args": ["-c:a", "flac", "-compression_level", "5"],
        "bitrate": False,
        "label": "FLAC 无损",
    },
    "wav": {"suffix": "wav", "args": ["-c:a", "pcm_s16le"], "bitrate": False, "label": "WAV 无压缩"},
    "ogg": {"suffix": "ogg", "args": ["-c:a", "libvorbis"], "bitrate": True, "label": "OGG Vorbis"},
}

# 比特率白名单（kbps），与前端选项一致
ALLOWED_BITRATES: Final[tuple[int, ...]] = (128, 192, 256, 320)

# 采样率白名单（Hz）
ALLOWED_SAMPLE_RATES: Final[tuple[int, ...]] = (22050, 32000, 44100, 48000, 96000)


@dataclass(slots=True)
class TranscodeOptions:
    """转码参数。

    Attributes:
        output_format: 目标格式 id（mp4 / mp3 / m4a / flac / wav / ogg）。
        bitrate: 比特率（kbps）；None 表示使用编码器默认值。
        sample_rate: 采样率（Hz）；None 表示保持源采样率。
        channels: 声道数；None 表示保持源声道。
        keep_metadata: 是否尝试保留标签与封面。
    """

    output_format: str = "mp3"
    bitrate: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    keep_metadata: bool = True

    @classmethod
    def from_form(cls, **raw: Any) -> TranscodeOptions:
        """从表单字段构造（容忍空串与非法值）。

        Args:
            **raw: 表单字段（output_format / bitrate / sample_rate / channels / keep_metadata）。

        Returns:
            TranscodeOptions 实例。

        Raises:
            ConversionFailedError: 输出格式不受支持。
        """
        fmt = str(raw.get("output_format") or "mp3").strip().lower()
        if fmt not in OUTPUT_FORMATS:
            raise ConversionFailedError(f"不支持的输出格式：{fmt}。可选：{', '.join(sorted(OUTPUT_FORMATS))}")

        def _optional_int(key: str, allowed: tuple[int, ...] | None = None) -> int | None:
            """解析可选整数（越界或非法值回退为 None）。

            Args:
                key: 字段名。
                allowed: 允许值白名单。

            Returns:
                解析结果或 None。
            """
            value = raw.get(key)
            if value in (None, "", "default", "auto"):
                return None
            try:
                parsed = int(float(str(value)))
            except (TypeError, ValueError):
                return None
            if allowed is not None and parsed not in allowed:
                return None
            return parsed

        keep = raw.get("keep_metadata", True)
        if isinstance(keep, bool):
            keep_metadata = keep
        else:
            keep_metadata = str(keep).strip().lower() in {"true", "1", "yes", "on"}

        return cls(
            output_format=fmt,
            bitrate=_optional_int("bitrate", ALLOWED_BITRATES),
            sample_rate=_optional_int("sample_rate", ALLOWED_SAMPLE_RATES),
            channels=_optional_int("channels", (1, 2)),
            keep_metadata=keep_metadata,
        )

    @property
    def suffix(self) -> str:
        """输出扩展名。

        Returns:
            扩展名（不含点）。
        """
        return OUTPUT_FORMATS[self.output_format]["suffix"]

    @property
    def label(self) -> str:
        """输出格式的展示名。

        Returns:
            中文展示名。
        """
        return OUTPUT_FORMATS[self.output_format]["label"]


def _require_ffmpeg() -> Path:
    """取 FFmpeg 路径。

    Returns:
        FFmpeg 可执行文件路径。

    Raises:
        EngineUnavailableError: FFmpeg 缺失。
    """
    path = ffmpeg_path()
    if path is None:
        from app.core.engines import engine_probe

        status = engine_probe.status_of("ffmpeg")
        raise EngineUnavailableError(
            "未检测到 FFmpeg 引擎，无法执行音频转码。"
            + (status.hint or "请将 FFmpeg 静态构建解压至 runtime/ffmpeg/bin/。")
        )
    return path


def build_ffmpeg_args(
    source: Path,
    output: Path,
    options: TranscodeOptions,
) -> list[str]:
    """构造 FFmpeg 命令行参数。

    Args:
        source: 输入音频。
        output: 输出路径。
        options: 转码参数。

    Returns:
        参数列表（不含可执行文件本身）。
    """
    spec = OUTPUT_FORMATS[options.output_format]
    args: list[str] = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",  # 覆盖输出（输出路径由我们唯一分配）
        "-i",
        str(source),
        "-vn",  # 丢弃视频流：转码目标是音频文件，残留的封面流会导致容器异常
        *spec["args"],
    ]

    if spec["bitrate"] and options.bitrate:
        args.extend(["-b:a", f"{options.bitrate}k"])
    if options.sample_rate:
        args.extend(["-ar", str(options.sample_rate)])
    if options.channels:
        args.extend(["-ac", str(options.channels)])

    # 输出容器由扩展名决定；加 -f 显式指定可避免 FFmpeg 在 m4a 上猜测失败
    if spec["suffix"] == "m4a":
        args.extend(["-f", "ipod"])
    elif spec["suffix"] == "ogg":
        args.extend(["-f", "ogg"])

    args.append(str(output))
    return args


def transcode(
    source: Path,
    output: Path,
    options: TranscodeOptions,
    *,
    ctx: JobContext | None = None,
    timeout_sec: int = 600,
) -> Path:
    """执行音频转码。

    Args:
        source: 输入音频（已解封装或原生通用格式）。
        output: 输出路径。
        options: 转码参数。
        ctx: 任务上下文。
        timeout_sec: 超时秒数。

    Returns:
        输出文件路径。

    Raises:
        EngineUnavailableError: FFmpeg 缺失。
        ConversionFailedError: 转码失败。
    """
    ffmpeg = _require_ffmpeg()
    output.parent.mkdir(parents=True, exist_ok=True)

    args = [str(ffmpeg), *build_ffmpeg_args(source, output, options)]
    if ctx:
        ctx.progress(45)

    code, _stdout, stderr = run_process(args, timeout=timeout_sec)

    if code != 0 or not output.exists() or output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        reason = (stderr or "").strip().splitlines()
        detail = reason[-1] if reason else f"退出码 {code}"
        raise ConversionFailedError(
            f"音频转码失败：{detail[:200]}。"
            "常见原因：源文件损坏、编码器不受当前 FFmpeg 构建支持（如需 libmp3lame / libvorbis）。"
        )

    if ctx:
        ctx.progress(80)
    return output


# ---------------------------------------------------------------- 元数据


def copy_metadata(
    audio_path: Path,
    *,
    metadata: dict[str, Any] | None = None,
    cover: bytes | None = None,
    ctx: JobContext | None = None,
) -> None:
    """把标签与封面写入转码后的文件。

    所有失败都被吞掉并记录日志：元数据是锦上添花，**绝不能**因为
    mutagen 不支持某个容器或某段标签而让整个转换任务失败。

    Args:
        audio_path: 转码产出的音频文件路径。
        metadata: 来源元数据（NCM 解出的 JSON 或 mutagen 读出的标签）。
        cover: 封面图片字节。
        ctx: 任务上下文。
    """
    try:
        import mutagen  # type: ignore[import-not-found]
    except ImportError:
        logger.info("未安装 mutagen，跳过标签与封面写入")
        return

    try:
        audio = mutagen.File(str(audio_path))
        if audio is None:
            logger.info("mutagen 无法识别输出容器，跳过标签写入")
            return

        title, artist, album = _extract_tags(metadata)
        if title:
            _set_tag(audio, "title", "TIT2", "\xa9nam", title)
        if artist:
            _set_tag(audio, "artist", "TPE1", "\xa9ART", artist)
        if album:
            _set_tag(audio, "album", "TALB", "\xa9alb", album)

        if cover:
            _attach_cover(audio, cover)

        audio.save()
        if ctx:
            ctx.progress(92)
    except Exception as exc:  # noqa: BLE001 - 标签写入失败不影响音频本身
        logger.info("标签写入失败（不影响音频内容）：%s", exc)


def _extract_tags(metadata: dict[str, Any] | None) -> tuple[str, str, str]:
    """从元数据字典中提取标题/艺术家/专辑。

    同时兼容 NCM 的原始字段名（``musicName`` / ``artist`` 为 ``[[名字, id], ...]``）
    与标准字段名（``title`` / ``artist`` / ``album``）。

    Args:
        metadata: 元数据字典。

    Returns:
        ``(标题, 艺术家, 专辑)``，缺失项为空串。
    """
    if not metadata:
        return "", "", ""

    title = str(metadata.get("musicName") or metadata.get("title") or "").strip()

    raw_artist = metadata.get("artist") or metadata.get("artists") or ""
    if isinstance(raw_artist, list):
        names: list[str] = []
        for item in raw_artist:
            if isinstance(item, (list, tuple)) and item:
                names.append(str(item[0]).strip())
            else:
                names.append(str(item).strip())
        artist = " / ".join(name for name in names if name)
    else:
        artist = str(raw_artist).strip()

    album = str(metadata.get("album") or metadata.get("albumName") or "").strip()
    return title, artist, album


def _set_tag(audio: Any, generic_key: str, id3_key: str, mp4_key: str, value: str) -> None:
    """按容器类型写入标签。

    Args:
        audio: mutagen 的文件对象。
        generic_key: 通用键名（Vorbis / FLAC 使用）。
        id3_key: ID3 帧名（MP3 使用）。
        mp4_key: MP4 atom 名（M4A 使用）。
        value: 标签值。
    """
    try:
        if audio.tags is None:
            audio.add_tags()
    except Exception:  # noqa: BLE001 - 部分容器已隐式创建 tags
        pass

    tags = audio.tags
    if tags is None:
        return

    # 不同容器用不同的键：ID3 用帧名，MP4 用 atom，Vorbis/FLAC 用通用小写键
    for key in (id3_key, mp4_key, generic_key):
        try:
            tags[key] = value
        except (KeyError, ValueError, TypeError):
            continue


def _guess_image_mime(data: bytes) -> str:
    """按 magic bytes 判定封面图片的 MIME。

    Args:
        data: 图片字节。

    Returns:
        MIME 字符串；无法判定时回退为 image/jpeg。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return "image/jpeg"


def _attach_cover(audio: Any, cover: bytes) -> None:
    """把封面写入容器。

    依次尝试 MP4（covr）、ID3（APIC）、FLAC/Ogg（Picture）三种承载方式，
    与容器类型不匹配的尝试会抛错并被忽略，因此顺序无关正确性。

    Args:
        audio: mutagen 的文件对象。
        cover: 图片字节。
    """
    mime = _guess_image_mime(cover)

    try:
        # MP4 / M4A：使用 MP4Cover
        from mutagen.mp4 import MP4Cover

        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(cover, imageformat=fmt)]
        return
    except Exception:  # noqa: BLE001
        pass

    try:
        # ID3（MP3）：APIC 帧
        from mutagen.id3 import APIC

        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))
        return
    except Exception:  # noqa: BLE001
        pass

    try:
        # FLAC / Ogg：Picture 块
        from mutagen.flac import Picture

        picture = Picture()
        picture.data = cover
        picture.type = 3
        picture.mime = mime
        picture.desc = "Cover"
        audio.add_picture(picture)
    except Exception as exc:  # noqa: BLE001
        logger.debug("封面写入失败（容器可能不支持）：%s", exc)


def probe_output_duration(path: Path) -> float | None:
    """读取输出文件的时长（用于 AC-07 的时长一致性校验）。

    Args:
        path: 音频文件路径。

    Returns:
        时长（秒）；无法读取返回 None。
    """
    from app.modules.audio.probe import probe_duration

    return probe_duration(path)


__all__ = [
    "ALLOWED_BITRATES",
    "ALLOWED_SAMPLE_RATES",
    "OUTPUT_FORMATS",
    "TranscodeOptions",
    "build_ffmpeg_args",
    "copy_metadata",
    "probe_output_duration",
    "transcode",
]
