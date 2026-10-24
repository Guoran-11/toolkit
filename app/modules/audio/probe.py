"""音频格式嗅探。

判定顺序（按可靠性从高到低）：
    1. **magic bytes / 文件头特征** —— 不信任扩展名（FR-AU-01 明确要求）
    2. 扩展名 —— 仅在文件头无法判定时作为兜底
    3. 无判定结果 —— 如实返回 ``unknown``，绝不猜测

平台专有格式（.ncm / .qmc* / .kgm / .kwm ...）没有标准 magic，
但它们的**头部结构**有稳定特征（如 NCM 的 ``CTENFDAM``），
因此本项目为每种专有格式实现了头部指纹校验，而不是只看扩展名 ——
否则一个改名的 MP3 会被误判为加密文件，白白走一遍解密流程。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProprietaryFormat:
    """平台专有格式的描述。

    Attributes:
        suffix: 扩展名（小写，不含点）。
        platform: 平台展示名。
        magic: 头部特征字节；空表示仅按扩展名匹配。
        magic_offset: 头部特征的偏移量。
        container_hint: 解密后可能的容器（用于结果提示）。
        supported: 本版本是否实现了该变体的解封装。
        note: 补充说明。
    """

    suffix: str
    platform: str
    magic: bytes = b""
    magic_offset: int = 0
    container_hint: str = ""
    supported: bool = False
    note: str = ""


# 专有格式注册表（与前端 audio.js 的 PROPRIETARY 表保持一致）
PROPRIETARY_FORMATS: Final[dict[str, ProprietaryFormat]] = {
    "ncm": ProprietaryFormat(
        suffix="ncm",
        platform="网易云音乐",
        magic=b"CTENFDAM",  # 文件头 magic，稳定可靠
        container_hint="mp3/flac",
        supported=True,
        note="检测到网易云音乐加密文件，将在服务端解封装为原始音频格式。",
    ),
    "qmc0": ProprietaryFormat(
        suffix="qmc0",
        platform="QQ 音乐",
        container_hint="mp3",
        supported=False,
        note="QMCv2 系列需要平台下发的密钥材料，本版本不解封装该变体。",
    ),
    "qmc3": ProprietaryFormat(
        suffix="qmc3",
        platform="QQ 音乐",
        container_hint="mp3",
        supported=False,
        note="QMCv2 系列需要平台下发的密钥材料，本版本不解封装该变体。",
    ),
    "qmcflac": ProprietaryFormat(
        suffix="qmcflac",
        platform="QQ 音乐",
        container_hint="flac",
        supported=False,
        note="QMCv2 系列需要平台下发的密钥材料，本版本不解封装该变体。",
    ),
    "qmcogg": ProprietaryFormat(
        suffix="qmcogg",
        platform="QQ 音乐",
        container_hint="ogg",
        supported=False,
        note="QMCv2 系列需要平台下发的密钥材料，本版本不解封装该变体。",
    ),
    "mgg": ProprietaryFormat(
        suffix="mgg",
        platform="QQ 音乐",
        container_hint="ogg",
        supported=False,
        note="QQ 音乐新版加密变体，本版本不解封装。",
    ),
    "mflac": ProprietaryFormat(
        suffix="mflac",
        platform="QQ 音乐",
        container_hint="flac",
        supported=False,
        note="QQ 音乐新版加密变体，本版本不解封装。",
    ),
    "kgm": ProprietaryFormat(
        suffix="kgm",
        platform="酷狗音乐",
        container_hint="flac/mp3",
        supported=False,
        note="酷狗加密变体，本版本不解封装。",
    ),
    "vpr": ProprietaryFormat(
        suffix="vpr",
        platform="酷狗音乐",
        container_hint="mp3",
        supported=False,
        note="酷狗加密变体，本版本不解封装。",
    ),
    "kwm": ProprietaryFormat(
        suffix="kwm",
        platform="酷我音乐",
        container_hint="flac/mp3",
        supported=False,
        note="酷我加密变体，本版本不解封装。",
    ),
    "mg3d": ProprietaryFormat(
        suffix="mg3d",
        platform="咪咕音乐",
        container_hint="mp3",
        supported=False,
        note="咪咕加密变体，本版本不解封装。",
    ),
}

# 通用音频容器的 magic 特征表： (偏移, 字节, 类型 key, 展示名)
AUDIO_SIGNATURES: Final[tuple[tuple[int, bytes, str, str], ...]] = (
    (0, b"fLaC", "flac", "FLAC 无损音频"),
    (0, b"OggS", "ogg", "Ogg 容器（Vorbis / Opus）"),
    (0, b"RIFF", "wav", "WAV 波形音频"),
    (0, b"ID3", "mp3", "MP3（含 ID3 标签）"),
    (0, b"\xff\xfb", "mp3", "MP3 音频"),
    (0, b"\xff\xf3", "mp3", "MP3 音频"),
    (0, b"\xff\xf2", "mp3", "MP3 音频"),
    (0, b"\xff\xfa", "mp3", "MP3 音频"),
    (0, b"\xff\xf1", "aac", "AAC 音频（ADTS）"),
    (0, b"\xff\xf9", "aac", "AAC 音频（ADTS）"),
    (4, b"ftyp", "m4a", "MP4 / M4A 容器"),
    (0, b"\x1aE\xdf\xa3", "webm", "Matroska / WebM 容器"),
    (0, b"MAC ", "ape", "Monkey's Audio 无损"),
    (0, b"wvpk", "wv", "WavPack 无损"),
    (0, b"TTA1", "tta", "TTA 无损"),
    (0, b"DSD ", "dsf", "DSD 音频"),
)

# 通用音频扩展名白名单（与矩阵 §5.3.2 的"通用音频"行一致）
COMMON_AUDIO_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "mp3",
        "flac",
        "wav",
        "m4a",
        "mp4",
        "aac",
        "ogg",
        "oga",
        "opus",
        "wma",
        "ape",
        "wv",
        "tta",
        "aiff",
        "aif",
        "dsf",
        "amr",
    }
)

# 全部可上传的扩展名 = 通用音频 + 专有格式
ALL_AUDIO_SUFFIXES: Final[frozenset[str]] = COMMON_AUDIO_SUFFIXES | frozenset(PROPRIETARY_FORMATS)


@dataclass(slots=True)
class ProbeResult:
    """单个文件的嗅探结果。

    字段与前端 ``audio.js`` 消费的 ``probe`` 对象逐一对齐
    （``name`` / ``detected_type`` / ``detected_label`` / ``decryptable`` /
    ``container`` / ``duration_sec`` / ``note``），字段名保持 snake_case。

    Attributes:
        name: 原始文件名。
        size: 字节数。
        detected_type: 类型标识（flac / mp3 / ncm / unknown）。
        detected_label: 面向用户的类型描述。
        decryptable: 是否可被本工具处理。
        container: 实际容器标识。
        duration_sec: 时长（秒），未知为 None。
        note: 说明文案。
        platform: 专有格式所属平台（通用格式为 None）。
        proprietary: 是否平台专有格式。
    """

    name: str
    size: int
    detected_type: str
    detected_label: str
    decryptable: bool
    container: str | None = None
    duration_sec: float | None = None
    note: str = ""
    platform: str | None = None
    proprietary: bool = False
    extra: dict[str, object] = field(default_factory=dict)

    def to_api(self) -> dict[str, object]:
        """转换为前端期望的字典。

        Returns:
            含全部约定字段的字典。
        """
        return {
            "name": self.name,
            "size": self.size,
            "detected_type": self.detected_type,
            "detected_label": self.detected_label,
            "decryptable": self.decryptable,
            "container": self.container,
            "duration_sec": self.duration_sec,
            "note": self.note,
            "platform": self.platform,
            "proprietary": self.proprietary,
        }


def read_head(path: Path, size: int = 64) -> bytes:
    """读取文件头。

    Args:
        path: 文件路径。
        size: 读取字节数。

    Returns:
        文件头字节；读取失败返回空字节串。
    """
    try:
        with path.open("rb") as fp:
            return fp.read(size)
    except OSError:
        return b""


def detect_proprietary(path: Path) -> ProprietaryFormat | None:
    """判定文件是否为平台专有格式。

    判定规则：扩展名必须命中注册表，且若注册表定义了头部特征，
    则文件头必须匹配 —— 避免改名的普通 MP3 被误送入解密流程。

    Args:
        path: 文件路径。

    Returns:
        匹配的 ProprietaryFormat；不是专有格式返回 None。
    """
    suffix = path.suffix.lower().lstrip(".")
    spec = PROPRIETARY_FORMATS.get(suffix)
    if spec is None:
        return None
    if not spec.magic:
        return spec

    head = read_head(path, 64)
    segment = head[spec.magic_offset : spec.magic_offset + len(spec.magic)]
    if segment == spec.magic:
        return spec

    # 头部特征不符：可能是被改名的普通音频，交由通用嗅探处理
    logger.info("文件 %s 扩展名为 %s 但头部特征不符，按通用音频处理", path.name, suffix)
    return None


def sniff_container(head: bytes) -> tuple[str, str] | None:
    """按文件头判定音频容器。

    Args:
        head: 文件头字节。

    Returns:
        ``(类型标识, 展示名)``；无法判定返回 None。
    """
    for offset, magic, kind, label in AUDIO_SIGNATURES:
        if head[offset : offset + len(magic)] == magic:
            return kind, label
    return None


def probe_file(path: Path, *, display_name: str | None = None) -> ProbeResult:
    """嗅探单个音频文件。

    Args:
        path: 已落盘的文件路径。
        display_name: 展示用的文件名（默认取磁盘文件名）。

    Returns:
        ProbeResult 实例。
    """
    name = display_name or path.name
    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    head = read_head(path, 64)
    suffix = path.suffix.lower().lstrip(".")

    # 1) 平台专有格式优先（它们的扩展名是明确的）
    proprietary = detect_proprietary(path)
    if proprietary is not None:
        # 头部特征确认过的专有格式：supported 决定是否可解密
        decryptable = proprietary.supported
        note = proprietary.note
        if not proprietary.supported:
            note += " 本工具不会尝试破解受授权保护的载荷。"
        return ProbeResult(
            name=name,
            size=size,
            detected_type=proprietary.suffix,
            detected_label=f"{proprietary.platform} · .{proprietary.suffix}",
            decryptable=decryptable,
            container=proprietary.container_hint or proprietary.suffix,
            duration_sec=None,
            note=note,
            platform=proprietary.platform,
            proprietary=True,
        )

    # 2) 通用音频容器
    sniffed = sniff_container(head)
    if sniffed:
        kind, label = sniffed
        return ProbeResult(
            name=name,
            size=size,
            detected_type=kind,
            detected_label=label,
            decryptable=True,
            container=kind,
            duration_sec=probe_duration(path),
            note="通用音频格式，可直接转码。",
        )

    # 3) 按扩展名兜底（部分容器如 wma/amr 头部特征不稳定）
    if suffix in COMMON_AUDIO_SUFFIXES:
        note = f"按扩展名 .{suffix} 识别，未在文件头找到标准特征。"
        duration = probe_duration(path)
        if duration is None:
            # 连时长都读不出来的"音频"，大概率是损坏文件或伪装扩展名
            return ProbeResult(
                name=name,
                size=size,
                detected_type="unknown",
                detected_label=f".{suffix} 文件（内容无法识别）",
                decryptable=False,
                container=None,
                duration_sec=None,
                note=note + " 文件可能已损坏，或并非真正的音频文件。",
            )
        return ProbeResult(
            name=name,
            size=size,
            detected_type=suffix,
            detected_label=f".{suffix} 音频",
            decryptable=True,
            container=suffix,
            duration_sec=duration,
            note=note,
        )

    # 4) 无法识别
    return ProbeResult(
        name=name,
        size=size,
        detected_type="unknown",
        detected_label="无法识别的格式",
        decryptable=False,
        container=None,
        duration_sec=None,
        note=(
            "未识别到已知的音频容器特征。若这是音乐平台的加密文件，"
            "请确认其扩展名（如 .ncm / .qmcflac）未被修改。"
        ),
    )


def probe_duration(path: Path) -> float | None:
    """尝试读取音频时长。

    优先使用 mutagen（纯 Python，无外部进程开销），失败时回退到 ffprobe。

    Args:
        path: 音频文件路径。

    Returns:
        时长（秒）；无法获取返回 None。
    """
    try:
        import mutagen  # type: ignore[import-not-found]

        audio = mutagen.File(str(path))
        if audio is not None and getattr(audio, "info", None) is not None:
            length = getattr(audio.info, "length", None)
            if length:
                return round(float(length), 3)
    except ImportError:
        logger.debug("未安装 mutagen，跳过时长读取")
    except Exception as exc:  # noqa: BLE001 - 损坏文件的解析异常很多
        logger.debug("mutagen 读取时长失败：%s", exc)

    return _duration_via_ffprobe(path)


def _duration_via_ffprobe(path: Path) -> float | None:
    """通过 ffprobe 读取时长（mutagen 不可用时的兜底）。

    Args:
        path: 音频文件路径。

    Returns:
        时长（秒）；不可用返回 None。
    """
    from app.core.engines import ffmpeg_path, run_process

    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        return None

    # ffprobe 通常与 ffmpeg 同目录
    probe = ffmpeg.parent / ("ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe")
    if not probe.exists():
        return None

    code, out, _ = run_process(
        [
            str(probe),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=15,
    )
    if code != 0:
        return None
    try:
        return round(float(out.strip()), 3)
    except ValueError:
        return None


def format_from_suffix(filename: str) -> str:
    """取文件名扩展名（小写）。

    Args:
        filename: 文件名。

    Returns:
        扩展名；无扩展名返回空串。
    """
    _, _, ext = (filename or "").rpartition(".")
    return ext.lower() if ext else ""


__all__ = [
    "ALL_AUDIO_SUFFIXES",
    "AUDIO_SIGNATURES",
    "COMMON_AUDIO_SUFFIXES",
    "PROPRIETARY_FORMATS",
    "ProbeResult",
    "ProprietaryFormat",
    "detect_proprietary",
    "format_from_suffix",
    "probe_duration",
    "probe_file",
    "sniff_container",
]
