"""平台专有音频格式的解封装。

**合规声明（§5.3.4，不可协商）**
    本模块只处理「用户已合法获得并保存在本机」的文件，全程在本地内存与磁盘完成，
    不上传任何第三方。本模块**不实现**任何绕过付费、破解授权、解除 DRM 的能力：
    对于需要平台密钥材料才能还原的变体（QMCv2 / KGM / KWM / MG3D 等），
    一律返回明确的「不支持」而不是尝试破解（NFR-AU-03）。

**架构（对应风险 R4 的应对措施）**
    所有变体通过 `register_decryptor` 注册到插件表，新增变体只需：
        1. 新建一个实现 ``Decryptor`` 协议的类；
        2. 调用 ``register_decryptor()`` 注册（或放进本模块末尾的自动注册区）。
    无需改动路由、任务或前端代码 —— 这就是规格书要求的「版本插件注册表」。

**已实现的变体**
    · NCM（网易云音乐）—— 完整实现。其解封装所需的密钥是**格式固有常量**
      （由格式本身固定，而非每个用户/每台设备的授权密钥），因此不涉及授权绕过。
    · 通用音频容器 —— 直通，不做任何处理。

**未实现的变体**
    见 ``probe.PROPRIETARY_FORMATS`` 中 ``supported=False`` 的条目，
    由 ``UnsupportedDecryptor`` 统一返回可读的「不支持」错误。
"""

from __future__ import annotations

import base64
import json
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from app.core.errors import DecryptError, EngineUnavailableError

logger = logging.getLogger(__name__)

# NCM 文件头 magic
NCM_MAGIC: Final[bytes] = b"CTENFDAM"

# NCM 的两个格式固有常量密钥（AES-128-ECB）
# core_key 用于还原 RC4 密钥材料与元数据；meta_key 用于还原元数据块。
# 这两个常量由 .ncm 容器格式本身固定，与用户账号/设备无关。
NCM_CORE_KEY: Final[bytes] = bytes.fromhex("687A4852416D736F356B496E62617857")  # "hzHRAmso5kInbaxW"
NCM_META_KEY: Final[bytes] = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")  # "#14ljk_!\]&0U<'("

# 分块处理的块大小（1 MiB）。逐字节 Python 循环对 30 MB 文件要数秒，
# 分块 + 大整数异或可以把它压到几十毫秒。
_XOR_CHUNK: Final[int] = 1024 * 1024


# ---------------------------------------------------------------- 数据模型


@dataclass(slots=True)
class DecryptResult:
    """解封装产出。

    Attributes:
        path: 还原出的音频文件路径。
        container: 容器标识（mp3 / flac / ogg / m4a）。
        metadata: 元数据（标题、艺术家、专辑等）。
        cover: 封面图片字节（可能为 None）。
        cover_mime: 封面 MIME。
        source_format: 来源专有格式扩展名。
    """

    path: Path
    container: str
    metadata: dict[str, Any] = field(default_factory=dict)
    cover: bytes | None = None
    cover_mime: str | None = None
    source_format: str = ""

    @property
    def title(self) -> str:
        """取标题（用于生成输出文件名）。

        Returns:
            标题字符串；无元数据时返回空串。
        """
        return str(self.metadata.get("musicName") or self.metadata.get("title") or "").strip()

    @property
    def artist(self) -> str:
        """取艺术家名。

        Returns:
            艺术家字符串；无元数据时返回空串。
        """
        raw = self.metadata.get("artist")
        if isinstance(raw, list) and raw:
            # NCM 的 artist 是 [[名字, id], ...] 形式
            first = raw[0]
            if isinstance(first, list) and first:
                return str(first[0]).strip()
            return str(first).strip()
        return str(self.metadata.get("artist") or "").strip()


# ---------------------------------------------------------------- 协议


@runtime_checkable
class Decryptor(Protocol):
    """解封装插件协议。"""

    #: 负责的扩展名（小写，不含点）
    suffix: str
    #: 是否为「真解密」实现（False 表示只负责返回不支持错误）
    implemented: bool

    def decrypt(self, source: Path, workdir: Path) -> DecryptResult:
        """执行解封装。

        Args:
            source: 加密文件路径。
            workdir: 输出目录。

        Returns:
            DecryptResult。

        Raises:
            DecryptError: 解封装失败或不支持。
        """
        ...


_REGISTRY: dict[str, Decryptor] = {}


def register_decryptor(decryptor: Decryptor) -> None:
    """注册一个解封装插件。

    Args:
        decryptor: 插件实例。
    """
    _REGISTRY[decryptor.suffix.lower()] = decryptor
    logger.debug("已注册音频解封装插件：%s（implemented=%s）", decryptor.suffix, decryptor.implemented)


def get_decryptor(suffix: str) -> Decryptor | None:
    """按扩展名取插件。

    Args:
        suffix: 扩展名（可含点）。

    Returns:
        插件实例；未注册返回 None。
    """
    return _REGISTRY.get(suffix.lower().lstrip("."))


def registered_suffixes() -> list[str]:
    """返回已注册的插件扩展名列表。

    Returns:
        扩展名列表（升序）。
    """
    return sorted(_REGISTRY)


# ---------------------------------------------------------------- 密码学工具


def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密（无填充处理）。

    Args:
        data: 密文（长度须为 16 的倍数）。
        key: 16 字节密钥。

    Returns:
        明文。

    Raises:
        EngineUnavailableError: 未安装 cryptography。
        DecryptError: 数据长度非法或解密失败。
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise EngineUnavailableError(
            "解封装需要 cryptography 库。请执行：pip install cryptography",
            extra={"missing_package": "cryptography"},
        ) from exc

    if len(data) == 0 or len(data) % 16 != 0:
        raise DecryptError("加密数据块长度非法，文件可能已损坏或并非该格式。")

    try:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return decryptor.update(data) + decryptor.finalize()
    except Exception as exc:
        raise DecryptError(f"解密失败：{str(exc)[:160]}") from exc


def _unpad_pkcs7(data: bytes) -> bytes:
    """去除 PKCS#7 填充。

    Args:
        data: 带填充的数据。

    Returns:
        去填充后的数据；填充非法时原样返回（部分文件填充不符合规范但仍可解析）。
    """
    if not data:
        return data
    padding = data[-1]
    if 1 <= padding <= 16 and padding <= len(data):
        return data[:-padding]
    return data


def _xor_bytes(data: bytes, key: int) -> bytes:
    """对字节串整体异或单字节密钥。

    用 ``bytes.translate`` 实现：比 Python 循环快一个数量级，
    因为转换表在 C 层应用。

    Args:
        data: 输入数据。
        key: 单字节密钥（0–255）。

    Returns:
        异或后的数据。
    """
    return data.translate(bytes.maketrans(bytes(range(256)), bytes(b ^ key for b in range(256))))


def _build_key_box(key: bytes) -> bytes:
    """构建 NCM 使用的 RC4 变体密钥盒。

    Args:
        key: 密钥材料（非空）。

    Returns:
        256 字节的密钥盒。
    """
    box = bytearray(range(256))
    key_length = len(key)
    last_byte = 0
    key_offset = 0

    for index in range(256):
        swap = box[index]
        cursor = (swap + last_byte + key[key_offset]) & 0xFF
        key_offset += 1
        if key_offset >= key_length:
            key_offset = 0
        box[index] = box[cursor]
        box[cursor] = swap
        last_byte = cursor

    return bytes(box)


def _build_keystream(box: bytes) -> bytes:
    """由密钥盒预计算 256 字节的周期密钥流。

    **关键优化**：该算法的密钥流仅依赖 ``(i + 1) % 256``，因此整体周期为 256。
    预计算 256 字节后即可用按块异或处理任意大小的文件，
    把原本 O(n) 的 Python 逐字节运算降到几乎零开销。

    Args:
        box: 256 字节密钥盒。

    Returns:
        256 字节的密钥流（索引 i 对应原始算法中位置 i 的密钥字节）。
    """
    table = bytearray(256)
    for cursor in range(256):
        table[cursor] = box[(box[cursor] + box[(box[cursor] + cursor) & 0xFF]) & 0xFF]
    # 原始算法用 j = (i + 1) & 0xff，因此位置 i 的密钥字节是 table[(i+1) % 256]
    return bytes(table[1:] + table[:1])


def _xor_with_keystream(data: bytes, keystream: bytes) -> bytes:
    """用周期密钥流对数据做异或。

    按块转成大整数异或：避免了 Python 层面的逐字节循环，
    同时把峰值内存控制在块大小量级（而不是整个文件的两倍）。

    Args:
        data: 待解密数据。
        keystream: 256 字节周期密钥流。

    Returns:
        解密后的数据。
    """
    if not data:
        return b""

    total = len(data)
    output = bytearray(total)
    period = len(keystream)

    for offset in range(0, total, _XOR_CHUNK):
        chunk = data[offset : offset + _XOR_CHUNK]
        size = len(chunk)
        # 构造与块等长的掩码：按周期铺开并裁剪
        repeats = size // period + 1
        mask = (keystream * repeats)[:size]
        # 大整数异或：O(n) 的 C 层运算
        mixed = int.from_bytes(chunk, "big") ^ int.from_bytes(mask, "big")
        output[offset : offset + size] = mixed.to_bytes(size, "big")

    return bytes(output)


# ---------------------------------------------------------------- NCM 插件


class NcmDecryptor:
    """网易云音乐 .ncm 解封装。

    文件结构：
        [0:8]    magic "CTENFDAM"
        [8:10]   2 字节保留
        [10:14]  key_length (u32 LE)
        [...]    key_data（每字节 ^0x64 → AES-ECB 解密 → 去填充 → 丢弃前 17 字节）
        [:]      meta_length (u32 LE) + meta_data（^0x63 → 去前 22 字节 → base64 → AES → 去前 6 字节 → JSON）
        [:]      5 字节保留 + crc32(u32) + image_size(u32) + 封面图
        [:]      剩余即为加密音频流（用上面的密钥盒解密）
    """

    suffix = "ncm"
    implemented = True

    def decrypt(self, source: Path, workdir: Path) -> DecryptResult:
        """执行 NCM 解封装。

        Args:
            source: .ncm 文件路径。
            workdir: 输出目录。

        Returns:
            DecryptResult。

        Raises:
            DecryptError: 文件结构非法。
        """
        workdir.mkdir(parents=True, exist_ok=True)

        with source.open("rb") as fp:
            magic = fp.read(8)
            if magic != NCM_MAGIC:
                raise DecryptError("该文件的头部标识与网易云音乐 .ncm 格式不符，可能已损坏或并非此格式。")

            fp.seek(2, 1)  # 2 字节保留

            raw_u32 = fp.read(4)
            if len(raw_u32) < 4:
                raise DecryptError("文件结构不完整：缺少密钥长度字段。")
            key_length = struct.unpack("<I", raw_u32)[0]
            if key_length <= 0 or key_length > 1024:
                raise DecryptError("密钥长度字段异常，文件可能已损坏。")

            key_data = fp.read(key_length)
            if len(key_data) < key_length:
                raise DecryptError("文件结构不完整：密钥数据被截断。")

        # 1) 还原 RC4 密钥材料
        key_data = _xor_bytes(key_data, 0x64)
        key_data = _unpad_pkcs7(_aes_ecb_decrypt(key_data, NCM_CORE_KEY))

        # 去除格式固有前缀 "neteasecloudmusic"（17 字节）
        if len(key_data) <= 17:
            raise DecryptError("还原后的密钥材料长度异常，文件可能已损坏。")
        key_box = _build_key_box(key_data[17:])
        keystream = _build_keystream(key_box)

        # 2) 还原元数据 + 封面
        # 元数据与封面的偏移依赖 key_length，必须重新按绝对位置读取
        with source.open("rb") as fp:
            fp.seek(8 + 2 + 4 + key_length)

            raw_meta_len = fp.read(4)
            metadata: dict[str, Any] = {}
            if len(raw_meta_len) == 4:
                meta_length = struct.unpack("<I", raw_meta_len)[0]
                if 0 < meta_length <= 8 * 1024 * 1024:
                    meta_blob = fp.read(meta_length)
                    metadata = _parse_ncm_metadata(meta_blob)

            fp.seek(5, 1)  # 5 字节保留
            raw_crc = fp.read(4)  # crc32，仅用于完整性校验，这里不使用
            cover: bytes | None = None
            if len(raw_crc) == 4:
                raw_image_size = fp.read(4)
                if len(raw_image_size) == 4:
                    image_size = struct.unpack("<I", raw_image_size)[0]
                    if 0 < image_size <= 32 * 1024 * 1024:
                        cover = fp.read(image_size) or None

            audio_offset = fp.tell()

        # 3) 探测容器（预解密头部若干字节后按 magic 判定）
        container = _peek_container(source, audio_offset, keystream, metadata)
        output = workdir / f"decrypted.{container}"

        self._decrypt_stream(source, audio_offset, output, keystream)

        if output.stat().st_size == 0:
            raise DecryptError(
                "解封装后未得到任何音频数据。该文件可能不包含可还原的音频载荷"
                "（本工具不会尝试破解受授权保护的载荷）。"
            )

        return DecryptResult(
            path=output,
            container=container,
            metadata=metadata,
            cover=cover,
            cover_mime=_guess_image_mime(cover),
            source_format="ncm",
        )

    @staticmethod
    def _decrypt_stream(source: Path, offset: int, output: Path, keystream: bytes) -> None:
        """分块解密音频流并写入目标文件。

        块大小刻意取 256 的整数倍，使每块的密钥流相位恒为 0 ——
        这样就能直接复用从位置 0 开始的 ``_xor_with_keystream``，
        无需维护跨块的相位状态。

        Args:
            source: 源文件。
            offset: 音频数据起始偏移。
            output: 输出文件。
            keystream: 256 字节周期密钥流。
        """
        # 保证块大小是密钥流周期的整数倍
        block = (_XOR_CHUNK // len(keystream)) * len(keystream)

        with source.open("rb") as src, output.open("wb") as dst:
            src.seek(offset)
            while True:
                chunk = src.read(block)
                if not chunk:
                    break
                dst.write(_xor_with_keystream(chunk, keystream))


def _parse_ncm_metadata(blob: bytes) -> dict[str, Any]:
    """解析 NCM 元数据块。

    Args:
        blob: 原始元数据字节。

    Returns:
        元数据字典；解析失败返回空字典（元数据缺失不应导致解密失败）。
    """
    if not blob:
        return {}
    try:
        data = _xor_bytes(blob, 0x63)
        # 前 22 字节是格式固有前缀 "163 key(Don't modify):"
        data = base64.b64decode(data[22:])
        data = _unpad_pkcs7(_aes_ecb_decrypt(data, NCM_META_KEY))
        # 前 6 字节是 "music:" 前缀
        text = data[6:].decode("utf-8", errors="replace").strip()
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception as exc:  # noqa: BLE001 - 元数据解析失败不影响音频还原
        logger.info("NCM 元数据解析失败（不影响音频还原）：%s", exc)
        return {}


def _peek_container(
    source: Path,
    audio_offset: int,
    keystream: bytes,
    metadata: dict[str, Any],
) -> str:
    """判定解密后音频的容器类型。

    做法：读取加密音频的头部若干字节，用同一密钥流做一次预解密，
    再按 magic 判定。这比依赖元数据的 ``format`` 字段更可靠
    （部分文件的元数据不完整），但元数据可作为兜底。

    Args:
        source: 源文件。
        audio_offset: 音频数据起始偏移。
        keystream: 周期密钥流。
        metadata: 已解析的元数据（提供 ``format`` 字段作为兜底）。

    Returns:
        容器标识（mp3 / flac / ogg / m4a）。
    """
    try:
        with source.open("rb") as fp:
            fp.seek(audio_offset)
            head = fp.read(64)
    except OSError:
        head = b""

    if head:
        plain = _xor_with_keystream(head, keystream)
        if plain.startswith(b"fLaC"):
            return "flac"
        if plain.startswith(b"OggS"):
            return "ogg"
        if plain.startswith(b"ID3"):
            return "mp3"
        if plain[4:8] == b"ftyp":
            return "m4a"
        if plain[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa", b"\xff\xf1"):
            return "mp3"

    # 兜底：元数据里的 format 字段（"mp3" / "flac"）
    declared = str(metadata.get("format") or "").strip().lower()
    if declared in {"mp3", "flac", "ogg", "m4a", "aac", "wav"}:
        return "ogg" if declared == "aac" else declared

    logger.info("无法从文件头判定 NCM 内层容器，回退为 mp3")
    return "mp3"


def _guess_image_mime(data: bytes | None) -> str | None:
    """按 magic 判定封面图片的 MIME。

    Args:
        data: 图片字节。

    Returns:
        MIME 字符串；无法判定返回 None。
    """
    if not data:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return None


# ---------------------------------------------------------------- 占位插件


class UnsupportedDecryptor:
    """未实现变体的占位插件。

    存在的价值：当用户上传 QMC / KGM / KWM 等文件时，给出**明确且诚实**的
    「本版本不支持该变体」提示，而不是让任务以一段 Python 异常收场。
    同时它让插件注册表保持完整 —— 扩充支持时只需替换这个类的实例。
    """

    def __init__(self, suffix: str, platform: str, note: str = "") -> None:
        """初始化。

        Args:
            suffix: 扩展名。
            platform: 平台展示名。
            note: 补充说明。
        """
        self.suffix = suffix
        self.platform = platform
        self.note = note
        self.implemented = False

    def decrypt(self, source: Path, workdir: Path) -> DecryptResult:
        """始终抛出「不支持」。

        Args:
            source: 源文件。
            workdir: 输出目录（未使用）。

        Raises:
            DecryptError: 固定抛出，错误码为 UNSUPPORTED_VARIANT。
        """
        raise DecryptError(
            f"{self.platform} 的 .{self.suffix} 格式需要平台下发的密钥材料才能还原，"
            "本工具不含该密钥，因此不支持解封装该变体。"
            "本工具也不会尝试破解受授权保护的载荷 —— 请使用平台官方渠道获取通用格式。"
            + (f"（{self.note}）" if self.note else ""),
            code="UNSUPPORTED_VARIANT",
        )


# ---------------------------------------------------------------- 插件注册

# 已实现的变体
register_decryptor(NcmDecryptor())

# 未实现的变体（保持注册表完整，给出诚实的拒绝理由）
for _suffix, _platform, _note in (
    ("qmc0", "QQ 音乐", "QMCv2 静态密钥变体"),
    ("qmc3", "QQ 音乐", "QMCv2 静态密钥变体"),
    ("qmcflac", "QQ 音乐", "QMCv2 静态密钥变体"),
    ("qmcogg", "QQ 音乐", "QMCv2 静态密钥变体"),
    ("mgg", "QQ 音乐", "新版加密变体"),
    ("mflac", "QQ 音乐", "新版加密变体"),
    ("kgm", "酷狗音乐", ""),
    ("vpr", "酷狗音乐", ""),
    ("kwm", "酷我音乐", ""),
    ("mg3d", "咪咕音乐", ""),
):
    register_decryptor(UnsupportedDecryptor(_suffix, _platform, _note))


# ---------------------------------------------------------------- 对外入口


def decrypt_if_needed(source: Path, workdir: Path) -> DecryptResult | None:
    """按需解封装。

    Args:
        source: 已落盘的音频文件。
        workdir: 输出目录。

    Returns:
        DecryptResult（需要并完成解封装时）；**通用音频返回 None**（无需解密）。

    Raises:
        DecryptError: 解封装失败或不支持该变体。
    """
    from app.modules.audio.probe import detect_proprietary

    spec = detect_proprietary(source)
    if spec is None:
        # 通用音频：无需解封装，直接交给转码器
        return None

    decryptor = get_decryptor(spec.suffix)
    if decryptor is None:
        raise DecryptError(
            f"未注册 .{spec.suffix} 格式的解封装插件，无法处理该文件。",
            code="UNSUPPORTED_VARIANT",
        )
    return decryptor.decrypt(source, workdir)


__all__ = [
    "NCM_CORE_KEY",
    "NCM_MAGIC",
    "NCM_META_KEY",
    "DecryptResult",
    "Decryptor",
    "NcmDecryptor",
    "UnsupportedDecryptor",
    "decrypt_if_needed",
    "get_decryptor",
    "register_decryptor",
    "registered_suffixes",
]
