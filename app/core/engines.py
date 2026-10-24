"""外部引擎探测与调用封装。

支持的引擎：
    - LibreOffice headless（Office → PDF / 图片，以及少量 HTML 导出）
    - Chromium via Playwright（HTML → PDF / PNG，Markdown 导出 PDF）
    - FFmpeg static（音频转码与解封装后的容器封装）

设计原则：
    1. 探测结果缓存。引擎版本不会在运行期变化，每次请求都起子进程会让 /healthz 变成慢接口。
    2. 引擎缺失时**不抛裸异常**，而是返回结构化状态 + 安装指引（FR-SYS-06、AC-12）。
    3. 所有子进程调用统一带超时，超时后强杀进程树（NFR-DC-01：防止 soffice 进程残留）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.core.config import (
    BASE_DIR,
    RUNTIME_DIR,
    TEMP_DIR,
    get_settings,
    resolve_engine_path,
)

logger = logging.getLogger(__name__)

# 引擎可用性的缓存有效期（秒）。运行期引擎不会变化，长缓存即可。
_PROBE_TTL_SEC: Final[float] = 300.0


@dataclass(slots=True)
class EngineStatus:
    """单个引擎的自检结果（结构与前端 normalizeEngines 兼容）。

    Attributes:
        state: ``ok`` / ``warn`` / ``fail`` / ``unknown``。
        version: 版本号字符串，未探测到时为 None。
        path: 可执行文件绝对路径。
        hint: 缺失或异常时给出的修复指引（面向用户的中文）。
    """

    state: str
    version: str | None = None
    path: str | None = None
    hint: str | None = None

    def to_api(self) -> dict[str, object]:
        """转换为前端期望的字典结构。

        Returns:
            含 ``state`` / ``version`` / ``path`` / ``hint`` 的字典。
        """
        return {
            "state": self.state,
            "version": self.version,
            "path": self.path,
            "hint": self.hint,
        }


# ---------------------------------------------------------------- 子进程工具


def _no_window_kwargs() -> dict[str, object]:
    """返回抑制 Windows 控制台窗口的 subprocess 参数。

    离线单机版以 GUI 形态运行（无控制台），若不加此参数，
    每次调用 FFmpeg / soffice 都会闪出黑框，属于明显的体验缺陷。

    Returns:
        subprocess 关键字参数字典。
    """
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def terminate_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """强制终止子进程及其后代。

    LibreOffice 会派生 ``soffice.bin`` 子进程，只 kill 父进程会留下孤儿占用
    UserProfile 目录，导致后续并发转换随机失败（风险 R8）。

    Args:
        proc: 目标进程。
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            # taskkill /T 连带子进程树；/F 强制
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                **_no_window_kwargs(),  # type: ignore[arg-type]
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError, subprocess.SubprocessError):
        with contextlib.suppress(OSError):
            proc.kill()
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("子进程 %s 无法终止，已放弃等待", proc.pid)


def run_process(
    args: Sequence[str],
    *,
    timeout: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """执行外部命令并带超时强杀。

    Args:
        args: 命令与参数列表（**不使用 shell**，杜绝参数注入）。
        timeout: 超时秒数。
        cwd: 工作目录。
        env: 追加/覆盖的环境变量。

    Returns:
        ``(返回码, stdout, stderr)``；超时返回 ``(-9, '', '超时')``。

    Raises:
        FileNotFoundError: 可执行文件不存在。
    """
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    # POSIX 下新建进程组，便于整组终止
    popen_kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "cwd": str(cwd) if cwd else None,
        "env": merged_env,
        **_no_window_kwargs(),
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(list(args), **popen_kwargs)  # type: ignore[arg-type]
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process_tree(proc)
        return -9, "", f"执行超时（>{timeout}s）"

    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


# ---------------------------------------------------------------- 引擎探测器


class EngineProbe:
    """引擎可用性探测器（带 TTL 缓存与线程安全）。"""

    def __init__(self) -> None:
        """初始化探测缓存。"""
        self._lock = threading.RLock()
        self._cache: dict[str, EngineStatus] = {}
        self._cached_at: float = 0.0

    def _settings(self):  # noqa: ANN202 - 返回类型为 Settings，避免循环导入标注
        """取当前配置对象。"""
        return get_settings()

    # -------------------------------------------------- 各引擎探测

    def _probe_libreoffice(self) -> EngineStatus:
        """探测 LibreOffice headless。

        Returns:
            EngineStatus；未找到时 hint 中给出可执行的修复步骤。
        """
        cfg = self._settings().engines
        path = resolve_engine_path(cfg.libreoffice)

        # 便携版常见布局：runtime/libreoffice/program/soffice(.exe|.bin)
        if path is None:
            for candidate in (
                RUNTIME_DIR / "libreoffice" / "program" / "soffice.exe",
                RUNTIME_DIR / "libreoffice" / "program" / "soffice",
                RUNTIME_DIR / "libreoffice" / "program" / "soffice.bin",
            ):
                if candidate.exists():
                    path = candidate
                    break

        if path is None:
            # 系统级安装兜底（开发机常见）
            system_guess = shutil.which("soffice") or shutil.which("libreoffice")
            path = Path(system_guess) if system_guess else None

        if path is None:
            return EngineStatus(
                state="fail",
                hint=(
                    "未找到 LibreOffice。离线版请将便携版解压到 runtime/libreoffice/；"
                    "Linux 可执行 apt install libreoffice，或在 config.toml [engines] 中指定绝对路径。"
                ),
            )

        code, out, err = run_process([str(path), "--version"], timeout=30)
        version_text = (out or err).strip().splitlines()
        version = version_text[0].strip() if version_text else None
        if code != 0 and not version:
            return EngineStatus(
                state="warn",
                path=str(path),
                hint="LibreOffice 存在但无法获取版本号，转换可能失败，请检查安装完整性。",
            )
        return EngineStatus(state="ok", version=version, path=str(path))

    def _probe_ffmpeg(self) -> EngineStatus:
        """探测 FFmpeg。

        Returns:
            EngineStatus。
        """
        cfg = self._settings().engines
        path = resolve_engine_path(cfg.ffmpeg)

        if path is None:
            # 便携版常见布局：runtime/ffmpeg/bin/ffmpeg(.exe)
            for candidate in (
                RUNTIME_DIR / "ffmpeg" / "bin" / "ffmpeg.exe",
                RUNTIME_DIR / "ffmpeg" / "bin" / "ffmpeg",
                RUNTIME_DIR / "ffmpeg.exe",
            ):
                if candidate.exists():
                    path = candidate
                    break

        if path is None:
            system_guess = shutil.which("ffmpeg")
            path = Path(system_guess) if system_guess else None

        if path is None:
            return EngineStatus(
                state="fail",
                hint=(
                    "未找到 FFmpeg。请将静态构建解压至 runtime/ffmpeg/bin/，"
                    "或在 config.toml [engines] 中配置 ffmpeg 的绝对路径。"
                ),
            )

        code, out, err = run_process([str(path), "-version"], timeout=20)
        first_line = (out or err).strip().splitlines()
        if code != 0 or not first_line:
            return EngineStatus(
                state="warn", path=str(path), hint="FFmpeg 存在但无法正常执行，请检查二进制是否完整。"
            )
        # 形如 "ffmpeg version 6.1.1-full_build ..."
        parts = first_line[0].split()
        version = parts[2] if len(parts) > 2 else first_line[0][:40]
        return EngineStatus(state="ok", version=version, path=str(path))

    def _probe_chromium(self) -> EngineStatus:
        """探测 Playwright Chromium。

        Playwright 的浏览器不通过 PATH 暴露，因此探测分两步：
            1. 检查 python 包是否可导入；
            2. 检查浏览器可执行文件是否已下载。

        Returns:
            EngineStatus。
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return EngineStatus(
                state="fail",
                hint="未安装 Playwright。执行 pip install playwright 后运行 playwright install chromium。",
            )

        try:
            with sync_playwright() as pw:
                executable = pw.chromium.executable_path
        except Exception as exc:  # noqa: BLE001 - Playwright 异常类型不稳定，统一兜底
            return EngineStatus(
                state="fail",
                hint=f"Playwright 已安装但浏览器未就绪：{exc}。请执行 playwright install chromium。",
            )

        if not executable or not Path(executable).exists():
            return EngineStatus(
                state="fail",
                hint="Chromium 可执行文件缺失，请执行 playwright install chromium 后重启服务。",
            )

        try:
            from importlib.metadata import version as pkg_version

            version = pkg_version("playwright")
        except Exception:  # noqa: BLE001
            version = None
        return EngineStatus(state="ok", version=version, path=executable)

    def _probe_ytdlp(self) -> EngineStatus:
        """探测 yt-dlp（视频下载模块的可选兜底解析引擎）。

        探测顺序与其它引擎一致：配置路径 → PATH 命令名 → 当前解释器的
        ``python -m yt_dlp``。三者都没有时返回 fail + 安装指引，但**绝不抛错**：
        视频模块的解析主链路不依赖它，缺它只是少一条降级链（PRD 11.1）。

        Returns:
            EngineStatus。
        """
        cfg = self._settings().engines
        path = resolve_engine_path(cfg.ytdlp)
        if path is None:
            found = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
            path = Path(found) if found else None

        args: list[str]
        if path is not None:
            args = [str(path), "--version"]
        else:
            # 兜底：pip 安装的模块形态（可执行脚本可能不在 PATH 上）
            import importlib.util

            if importlib.util.find_spec("yt_dlp") is None:
                return EngineStatus(
                    state="fail",
                    hint=(
                        "未安装 yt-dlp（可选引擎，缺失不影响三平台主链路）。"
                        "需要时可执行 pip install yt-dlp，或在 config.toml [engines] 中配置 ytdlp 路径。"
                    ),
                )
            args = [sys.executable, "-m", "yt_dlp", "--version"]

        code, out, err = run_process(args, timeout=20)
        first_line = (out or err).strip().splitlines()
        if code != 0 or not first_line:
            return EngineStatus(
                state="warn",
                path=str(path) if path else None,
                hint="yt-dlp 存在但无法执行；视频下载仍可走平台直连解析，兜底链将跳过。",
            )
        return EngineStatus(state="ok", version=first_line[0].strip(), path=str(path) if path else None)

    # -------------------------------------------------- 对外接口

    def probe(self, *, force: bool = False) -> dict[str, EngineStatus]:
        """探测全部引擎（带缓存）。

        Args:
            force: 忽略缓存强制重新探测。

        Returns:
            ``{"libreoffice": EngineStatus, "chromium": ..., "ffmpeg": ...}``。
        """
        with self._lock:
            fresh = (time.monotonic() - self._cached_at) < _PROBE_TTL_SEC
            if self._cache and fresh and not force:
                return dict(self._cache)

        # 探测本身要起子进程，放到锁外执行以免阻塞其他请求读取缓存
        result = {
            "libreoffice": self._probe_libreoffice(),
            "chromium": self._probe_chromium(),
            "ffmpeg": self._probe_ffmpeg(),
            # 可选引擎：视频下载的兜底解析，缺失只影响降级链
            "ytdlp": self._probe_ytdlp(),
        }
        with self._lock:
            self._cache = result
            self._cached_at = time.monotonic()
        return dict(result)

    def cached(self) -> dict[str, EngineStatus]:
        """只读缓存，不做任何探测（绝不阻塞调用方）。

        用途：``/healthz`` 必须在一两百毫秒内返回，而引擎探测要起子进程
        （LibreOffice 冷启动可达数百毫秒）。前端 ``probeBackend`` 的探测超时是 1.5 秒，
        一旦超时就会把整个界面降级为演示模式 —— 那是很糟糕的体验。
        因此 ``/healthz`` 读缓存，缓存由启动预热或自检页刷新来填充。

        Returns:
            缓存内的引擎状态；无有效缓存时返回空字典。
        """
        with self._lock:
            if self._cache and (time.monotonic() - self._cached_at) < _PROBE_TTL_SEC:
                return dict(self._cache)
            return {}

    def status_of(self, name: str) -> EngineStatus:
        """取单个引擎状态。

        Args:
            name: 引擎名（libreoffice / chromium / ffmpeg）。

        Returns:
            EngineStatus；未知名称返回 unknown 状态。
        """
        return self.probe().get(name, EngineStatus(state="unknown", hint=f"未知引擎 {name}"))

    def api_payload(self) -> dict[str, dict[str, object]]:
        """构造 /healthz 与 /api/system/engines 直接可用的字典。

        Returns:
            引擎名 → 状态字典。
        """
        return {name: status.to_api() for name, status in self.probe().items()}

    def invalidate(self) -> None:
        """使缓存失效（引擎自检页手动刷新时使用）。"""
        with self._lock:
            self._cache = {}
            self._cached_at = 0.0


# ---------------------------------------------------------------- 路径获取


def libreoffice_path() -> Path | None:
    """取 LibreOffice 可执行文件路径。

    Returns:
        可用路径；不可用时返回 None。
    """
    status = engine_probe.status_of("libreoffice")
    return Path(status.path) if status.path else None


def ffmpeg_path() -> Path | None:
    """取 FFmpeg 可执行文件路径。

    Returns:
        可用路径；不可用时返回 None。
    """
    status = engine_probe.status_of("ffmpeg")
    return Path(status.path) if status.path else None


def ytdlp_path() -> Path | None:
    """取 yt-dlp 可执行文件路径（可选引擎）。

    Returns:
        可用路径；模块形态安装（``python -m yt_dlp``）或未安装时返回 None。
    """
    status = engine_probe.status_of("ytdlp")
    return Path(status.path) if status.path else None


def runtime_info() -> dict[str, str]:
    """运行时信息（/api/system/engines 的 runtime 字段）。

    Returns:
        含 platform / python / base_dir 的字典。
    """
    return {
        "platform": f"{sys.platform}-{os.name}",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "base_dir": str(BASE_DIR),
        "executable": sys.executable,
    }


engine_probe: Final[EngineProbe] = EngineProbe()


# ---------------------------------------------------------------- 临时目录管理


class ProfileAllocator:
    """LibreOffice UserProfile 分配器。

    单个 soffice 进程无法并发共享同一 UserProfile（NFR-DC-04），
    因此每个 worker 独占一个 profile 目录；复用而非每次新建，避免冷启动开销。
    """

    def __init__(self, slots: int = 8) -> None:
        """初始化。

        Args:
            slots: 可分配的 profile 槽位数（通常 ≥ 并发 worker 数）。
        """
        self.slots = max(1, slots)
        # 走 TEMP_DIR（= STORAGE_DIR/temp）而非硬编码 BASE_DIR/storage：
        # 否则 TOOLKIT_STORAGE_DIR 部署下 profile 会被写到另一个目录，
        # 与 sweep_stale 的清理范围不一致，造成孤儿目录。
        self._root = TEMP_DIR / "lo-profiles"
        self._root.mkdir(parents=True, exist_ok=True)
        self._pool: list[Path] = []
        self._lock = threading.RLock()
        for index in range(self.slots):
            slot = self._root / f"profile-{index}"
            slot.mkdir(parents=True, exist_ok=True)
            self._pool.append(slot)

    def acquire(self) -> Path:
        """取一个空闲 profile 目录。

        使用阻塞等待而非直接报错：转换任务本身在队列里排队，
        短暂等待比直接失败更符合用户预期。

        Returns:
            独占的 profile 目录路径。
        """
        while True:
            with self._lock:
                if self._pool:
                    return self._pool.pop()
            time.sleep(0.05)

    def release(self, slot: Path) -> None:
        """归还 profile 目录。

        Args:
            slot: 之前 acquire 得到的目录。
        """
        with self._lock:
            if slot not in self._pool and len(self._pool) < self.slots:
                self._pool.append(slot)


profile_allocator: Final[ProfileAllocator] = ProfileAllocator()
