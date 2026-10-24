"""文件生命周期管理。

职责边界：
    - 上传落盘（流式，绝不整体读入内存 —— 风险 R7）
    - 输出文件登记与查找（file_id → 磁盘文件）
    - 文件类型二次校验（magic bytes，不信任扩展名 —— NFR-04）
    - 过期文件清理与磁盘水位统计

安全约束：
    - 所有落盘名一律 UUID 化（NFR-DC-03），杜绝路径穿越与子进程参数注入
    - file_id 只允许 ``[A-Za-z0-9_-]``，任何含分隔符的输入直接拒绝
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import BinaryIO, Final

from app.core.config import OUTPUT_DIR, TEMP_DIR, UPLOAD_DIR
from app.core.errors import NotFoundError, UnsupportedFormatError

# ---------------------------------------------------------------- 常量

CHUNK_SIZE: Final[int] = 1024 * 1024  # 1 MiB：兼顾吞吐与内存占用

# file_id 白名单：UUID4 与十六进制串都能通过，其余一律拒绝
_SAFE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 文件名清洗：去掉路径分隔符、控制字符与 Windows 保留字符
_UNSAFE_NAME_RE: Final[re.Pattern[str]] = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# magic bytes 签名表：(偏移量, 字节序列, 类型标识)
# 只做「类型归类」而非精确格式识别：目的是拦下伪装扩展名，而非解析文件
_MAGIC_SIGNATURES: Final[tuple[tuple[int, bytes, str], ...]] = (
    (0, b"%PDF", "pdf"),
    (0, b"PK\x03\x04", "zip"),  # OOXML（docx/xlsx/pptx）与普通 zip 同族
    (0, b"\xd0\xcf\x11\xe0", "ole"),  # 旧版 Office 复合文档（doc/xls/ppt）
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"\xff\xd8\xff", "jpg"),
    (0, b"GIF8", "gif"),
    (0, b"RIFF", "riff"),  # WAV / AVI
    (0, b"fLaC", "flac"),
    (0, b"OggS", "ogg"),
    (0, b"ID3", "mp3"),
    (0, b"\xff\xfb", "mp3"),
    (0, b"\xff\xf3", "mp3"),
    (0, b"\xff\xf2", "mp3"),
    (4, b"ftyp", "mp4"),  # M4A / MP4 容器
    (0, b"CTCC", "ncm"),  # 网易云音乐
    (0, b"\x1aE\xdf\xa3", "webm"),  # Matroska / WebM
)

# 文本类文件没有固定 magic：用「无 BOM 且可解码」判定
_TEXT_LIKE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {"html", "htm", "mhtml", "md", "txt", "json", "xml", "css", "js"}
)


@dataclass(slots=True)
class OutputFile:
    """登记在册的输出产物。

    Attributes:
        file_id: 下载标识（UUID4 十六进制，无分隔符）。
        name: 对用户可见的原始文件名（含扩展名，已清洗）。
        path: 磁盘绝对路径字符串。
        size: 字节数。
        mime: MIME 类型。
        created_at: 创建时间戳（秒）。
        expire_at: 过期时间戳（秒），0 表示永不过期。
    """

    file_id: str
    name: str
    path: str
    size: int
    mime: str
    created_at: float
    expire_at: float = 0.0

    def to_api(self) -> dict[str, object]:
        """转换为前端 api.js 期望的 output 结构。

        Returns:
            含 ``file_id`` / ``name`` / ``size`` / ``mime`` / ``url`` 的字典。
            字段名与 mock.js 的 registerFile 完全一致。
        """
        return {
            "file_id": self.file_id,
            "name": self.name,
            "size": self.size,
            "mime": self.mime,
            "url": f"/api/download/{self.file_id}",
        }


# ---------------------------------------------------------------- 工具函数


def new_id(prefix: str = "") -> str:
    """生成无分隔符的随机标识。

    Args:
        prefix: 可选前缀（仅用于可读性，会一并做安全检查）。

    Returns:
        形如 ``t3f2a1b9c...`` 的十六进制字符串。
    """
    raw = uuid.uuid4().hex
    return f"{prefix}{raw}" if prefix else raw


def stage_output(path: Path) -> Path:
    """把引擎产物移出任务临时目录，等待任务管理器登记。

    **为什么必须这么做**（时序约束）：
        1. 任务函数在自己的 ``finally`` 里 ``rmtree(workdir)``；
        2. 任务函数 ``return JobResult(path=...)``；
        3. **返回之后**，任务管理器才调用 :meth:`FileRegistry.register` 把产物移入 outputs。

    产物若留在 workdir 内，第 1 步就已把它删掉，第 3 步的 ``shutil.move`` 必然
    ``FileNotFoundError``，任务被标记为失败——而错误文案还会指向"引擎缺失"，
    与真实原因完全无关。因此所有任务函数都必须在返回前调用本函数。

    Args:
        path: 引擎产出的文件路径。

    Returns:
        搬移后的路径（位于 storage/temp 根目录，不属于任何 workdir）。
    """
    source = Path(path)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    staged = TEMP_DIR / f"staged-{new_id()}{source.suffix}"
    shutil.move(str(source), str(staged))
    return staged


def safe_filename(name: str, fallback: str = "file") -> str:
    """清洗用户提供的文件名，使其可安全用于磁盘与响应头。

    Args:
        name: 原始文件名（可能含路径、控制字符）。
        fallback: 清洗后为空时的兜底名。

    Returns:
        仅保留最后一段路径且剔除危险字符的文件名。
    """
    # 先取 basename，兼容 Windows 反斜杠与 POSIX 斜杠
    base = re.split(r"[\\/]", name or "")[-1].strip()
    cleaned = _UNSAFE_NAME_RE.sub("_", base).strip(" .")
    if not cleaned:
        return fallback
    # 防止超长文件名触发文件系统 ENAMETOOLONG
    if len(cleaned) > 180:
        stem, dot, ext = cleaned.rpartition(".")
        cleaned = f"{stem[:150]}{dot}{ext[:20]}" if dot else cleaned[:180]
    return cleaned


def split_stem(name: str) -> tuple[str, str]:
    """拆分文件名主干与扩展名。

    Args:
        name: 文件名。

    Returns:
        ``(主干, 扩展名)``，扩展名小写且不含点；无扩展名时为空串。
    """
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return name, ""
    return stem, ext.lower()


def sniff_type(head: bytes) -> str | None:
    """按 magic bytes 判定文件类型族。

    Args:
        head: 文件头若干字节（建议至少 32 字节）。

    Returns:
        类型标识（如 ``pdf`` / ``zip`` / ``ncm``）；无法判定返回 None。
    """
    for offset, signature, kind in _MAGIC_SIGNATURES:
        if head[offset : offset + len(signature)] == signature:
            return kind
    return None


def sha256_prefix(path: Path, length: int = 8) -> str:
    """计算文件 SHA256 并返回前 N 位（任务模型 inputs.sha256 字段）。

    Args:
        path: 文件路径。
        length: 保留的十六进制位数。

    Returns:
        小写十六进制前缀；读取失败时返回占位串。
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fp:
            while chunk := fp.read(CHUNK_SIZE):
                digest.update(chunk)
    except OSError:
        return "unreadable"
    return digest.hexdigest()[:length]


def disk_free_bytes(path: Path | None = None) -> int:
    """查询磁盘剩余空间（§FR-SYS-05 与磁盘水位告警）。

    Args:
        path: 目标路径，默认 storage 目录所在盘。

    Returns:
        剩余字节数；查询失败返回 0。
    """
    try:
        usage = shutil.disk_usage(path or UPLOAD_DIR)
        return int(usage.free)
    except OSError:
        return 0


def save_stream(
    src: BinaryIO,
    dest: Path,
    *,
    max_bytes: int,
    chunk_size: int = CHUNK_SIZE,
) -> int:
    """把输入流分块写入磁盘，超限即中断并回滚。

    为什么要手写分块而不是 ``shutil.copyfileobj``：需要在超限的第一时间停止写入
    并删除半成品，否则 4 GB 的恶意上传会先把磁盘写满再报错（NFR-03 禁止磁盘泄漏）。

    Args:
        src: 可读的二进制流（UploadFile.file）。
        dest: 目标路径。
        max_bytes: 允许的最大字节数。
        chunk_size: 分块大小。

    Returns:
        实际写入的字节数。

    Raises:
        PayloadTooLargeError: 超过 ``max_bytes``；半成品文件已被清理。
        OSError: 磁盘写入失败。
    """
    from app.core.errors import PayloadTooLargeError

    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with dest.open("wb") as fp:
            while True:
                chunk = src.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise PayloadTooLargeError(f"文件体积超出服务端限制（{max_bytes // (1024 * 1024)} MB）")
                fp.write(chunk)
    except BaseException:
        # 任何异常（含超限）都要回收半成品，避免磁盘泄漏
        dest.unlink(missing_ok=True)
        raise
    return written


def read_head(path: Path, size: int = 64) -> bytes:
    """读取文件头若干字节用于类型嗅探。

    Args:
        path: 文件路径。
        size: 读取字节数。

    Returns:
        实际读到的字节串（文件过短时可能少于 ``size``）。
    """
    try:
        with path.open("rb") as fp:
            return fp.read(size)
    except OSError:
        return b""


def assert_format_allowed(
    path: Path,
    *,
    allowed_kinds: Iterable[str],
    allowed_suffixes: Iterable[str],
    hint: str = "",
) -> str:
    """校验上传文件是否符合白名单（NFR-04：扩展名 + magic bytes 双重校验）。

    策略：扩展名必须在白名单内，且若文件头可识别，则识别结果必须落在允许的类型族内。
    对于 HTML / Markdown 这类纯文本，文件头无法判定，此时只依赖扩展名 + 可解码性检查。

    Args:
        path: 已落盘的文件路径。
        allowed_kinds: 允许的 magic 类型族集合（如 {"pdf", "zip"}）。
        allowed_suffixes: 允许的扩展名集合（小写、不含点）。
        hint: 附加到错误信息后的提示语。

    Returns:
        实际判定出的类型族标识；纯文本返回 ``"text"``。

    Raises:
        UnsupportedFormatError: 扩展名或内容不符合白名单。
    """
    suffix = path.suffix.lower().lstrip(".")
    allowed_suffix_set = {s.lower().lstrip(".") for s in allowed_suffixes}
    if suffix not in allowed_suffix_set:
        raise UnsupportedFormatError(f"不支持的文件类型 .{suffix or '(无扩展名)'}。" + (hint or ""))

    head = read_head(path)
    kind = sniff_type(head)

    if kind is None:
        # 无 magic 的文件：只可能是文本类；做一次 UTF-8 可解码性抽检
        if suffix in _TEXT_LIKE_SUFFIXES:
            if not _looks_like_text(head):
                raise UnsupportedFormatError(
                    f"文件内容与扩展名 .{suffix} 不符，可能已损坏或为伪装文件。" + (hint or "")
                )
            return "text"
        # 二进制类却嗅不出类型：宁可拒绝，也不把垃圾喂给引擎
        raise UnsupportedFormatError(
            f"无法识别该文件的实际格式（扩展名为 .{suffix}）。请确认文件未损坏。" + (hint or "")
        )

    if kind not in set(allowed_kinds):
        raise UnsupportedFormatError(
            f"文件实际内容为 {kind} 类型，与扩展名 .{suffix} 不符（NFR-04 类型校验）。" + (hint or "")
        )
    return kind


def _looks_like_text(head: bytes) -> bool:
    """粗判字节串是否为文本。

    Args:
        head: 文件头字节。

    Returns:
        无 NUL 字节且可用 UTF-8（容错模式）解码时返回 True。
    """
    if not head:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8", errors="strict")
        return True
    except UnicodeDecodeError:
        # 允许截断在多字节字符中间的情况
        return head.decode("utf-8", errors="ignore").strip() != ""


# ---------------------------------------------------------------- 输出登记表


@dataclass
class _RegistryState:
    """登记表的可序列化形态。"""

    files: dict[str, dict[str, object]] = field(default_factory=dict)


class FileRegistry:
    """输出产物登记表（线程安全，JSON 持久化）。

    为什么落盘：服务重启后用户仍可能点击历史任务里的下载链接，
    内存字典会让这些链接全部 404，体验与「文件保留 24 小时」的承诺不符。
    """

    def __init__(self, index_path: Path | None = None) -> None:
        """初始化登记表。

        Args:
            index_path: 索引文件路径，默认 storage/outputs/_index.json。
        """
        self._index_path = index_path or (OUTPUT_DIR / "_index.json")
        self._lock = threading.RLock()
        self._files: dict[str, OutputFile] = {}
        self._load()

    # -------------------------------------------------- 持久化

    def _load(self) -> None:
        """从磁盘载入索引；损坏时静默重建（不阻塞启动）。"""
        if not self._index_path.exists():
            return
        try:
            raw = json.loads(self._index_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for file_id, item in (raw.get("files") or {}).items():
            try:
                self._files[file_id] = OutputFile(**item)  # type: ignore[arg-type]
            except TypeError:
                continue

    def _flush(self) -> None:
        """把索引写回磁盘（原子替换，避免半截 JSON）。"""
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._index_path.with_suffix(".tmp")
        payload = {"files": {k: asdict(v) for k, v in self._files.items()}}
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._index_path)

    # -------------------------------------------------- 对外操作

    def register(
        self,
        source: Path,
        *,
        name: str,
        mime: str,
        ttl_hours: int,
    ) -> OutputFile:
        """把已生成的文件登记为可下载产物。

        文件会被移动到 storage/outputs 下并以 file_id 命名（NFR-04：输出文件名 UUID 化），
        原始文件名只保留在登记表的 ``name`` 字段里，下载时通过响应头还原。

        Args:
            source: 引擎产出的文件路径。
            name: 对用户可见的文件名。
            mime: MIME 类型。
            ttl_hours: 保留时长（小时）；0 表示不过期。

        Returns:
            登记后的 OutputFile。

        Raises:
            OSError: 文件移动失败。
        """
        with self._lock:
            file_id = new_id()
            target = OUTPUT_DIR / f"{file_id}{source.suffix}"
            shutil.move(str(source), str(target))

            now = time.time()
            record = OutputFile(
                file_id=file_id,
                name=safe_filename(name),
                path=str(target),
                size=target.stat().st_size,
                mime=mime,
                created_at=now,
                expire_at=now + ttl_hours * 3600 if ttl_hours > 0 else 0.0,
            )
            self._files[file_id] = record
            self._flush()
            return record

    def register_foreign(self, path: Path, *, name: str, mime: str, ttl_hours: int) -> OutputFile:
        """登记一个已经在 outputs 目录内的文件（不做移动）。

        Args:
            path: 位于 outputs 目录内的文件路径。
            name: 对用户可见的文件名。
            mime: MIME 类型。
            ttl_hours: 保留时长（小时）。

        Returns:
            登记后的 OutputFile。
        """
        with self._lock:
            now = time.time()
            record = OutputFile(
                file_id=new_id(),
                name=safe_filename(name),
                path=str(path),
                size=path.stat().st_size,
                mime=mime,
                created_at=now,
                expire_at=now + ttl_hours * 3600 if ttl_hours > 0 else 0.0,
            )
            self._files[record.file_id] = record
            self._flush()
            return record

    def get(self, file_id: str) -> OutputFile:
        """按 file_id 取产物。

        Args:
            file_id: 下载标识。

        Returns:
            OutputFile。

        Raises:
            NotFoundError: 标识非法、不存在、已过期或文件已被清理。
        """
        if not _SAFE_ID_RE.match(file_id or ""):
            # 非法 id 直接视为不存在，不泄露内部结构
            raise NotFoundError("文件不存在或已过期（默认保留 24 小时）")

        with self._lock:
            record = self._files.get(file_id)

        if record is None:
            raise NotFoundError("文件不存在或已过期（默认保留 24 小时）")
        if record.expire_at and record.expire_at < time.time():
            self.remove(file_id)
            raise NotFoundError("文件已超过保留期限并被清理")
        if not Path(record.path).exists():
            self.remove(file_id)
            raise NotFoundError("文件已被清理，请重新发起转换")
        return record

    def remove(self, file_id: str) -> None:
        """删除产物与其登记记录。

        Args:
            file_id: 下载标识。
        """
        with self._lock:
            record = self._files.pop(file_id, None)
            if record:
                Path(record.path).unlink(missing_ok=True)
            self._flush()

    def list_all(self) -> list[OutputFile]:
        """列出全部登记产物。

        Returns:
            OutputFile 列表（副本语义，调用方可自由处理）。
        """
        with self._lock:
            return list(self._files.values())

    # -------------------------------------------------- 维护

    def purge_expired(self) -> int:
        """清理过期产物。

        Returns:
            被清理的文件数量。
        """
        now = time.time()
        with self._lock:
            expired = [
                fid
                for fid, rec in self._files.items()
                if (rec.expire_at and rec.expire_at < now) or not Path(rec.path).exists()
            ]
            for fid in expired:
                rec = self._files.pop(fid, None)
                if rec:
                    Path(rec.path).unlink(missing_ok=True)
            if expired:
                self._flush()
        return len(expired)

    def purge_all(self) -> int:
        """清空全部产物（用户手动"立即清理"）。

        Returns:
            被清理的文件数量。
        """
        with self._lock:
            count = len(self._files)
            for rec in self._files.values():
                Path(rec.path).unlink(missing_ok=True)
            self._files.clear()
            self._flush()
        return count


# ---------------------------------------------------------------- 上传区


class UploadStore:
    """上传文件的临时仓储。

    上传文件视为「任务级临时资源」：任务结束后由调用方负责删除，
    进程异常退出时由 ``sweep_stale`` 按时间兜底清理。
    """

    def __init__(self, root: Path | None = None) -> None:
        """初始化。

        Args:
            root: 上传目录，默认 storage/uploads。
        """
        self.root = root or UPLOAD_DIR
        self.root.mkdir(parents=True, exist_ok=True)

    def allocate(self, original_name: str) -> Path:
        """为一次上传分配磁盘路径（UUID 化，保留扩展名以便引擎识别）。

        Args:
            original_name: 上传的原始文件名。

        Returns:
            新的绝对路径。
        """
        _, ext = split_stem(safe_filename(original_name))
        suffix = f".{ext}" if ext else ""
        return self.root / f"{new_id()}{suffix}"

    def sweep_stale(self, max_age_sec: int) -> int:
        """清理超龄的上传/临时文件（NFR-03：禁止磁盘泄漏）。

        Args:
            max_age_sec: 文件年龄阈值（秒）。

        Returns:
            被删除的文件数量。
        """
        cutoff = time.time() - max_age_sec
        removed = 0
        for directory in (self.root, TEMP_DIR):
            if not directory.exists():
                continue
            for item in directory.iterdir():
                if not item.is_file():
                    continue
                try:
                    if item.stat().st_mtime < cutoff:
                        item.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    continue
        return removed


# 进程级单例（保持与 mock.js 相同的"全局登记"语义）
registry: Final[FileRegistry] = FileRegistry()
upload_store: Final[UploadStore] = UploadStore()
