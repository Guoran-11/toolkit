"""SSH 会话管理（Paramiko）。

线程模型（这是本模块最容易出错的地方，务必看懂再改）：
    Paramiko 是**同步阻塞**库，而 FastAPI 的 WebSocket 处理器是协程。
    两者必须通过一个「桥」连接：

        [Paramiko 读线程]  channel.recv(n)  ──阻塞读──▶  asyncio.Queue
                                                              │
        [WS 协程]  queue.get() ──▶ websocket.send_bytes()  ◀──┘

        反方向：websocket.receive_bytes() ──▶ channel.send(data)（同步调用，极快，可直接执行）

    读线程通过 ``asyncio.run_coroutine_threadsafe`` 把数据投回事件循环，
    这是跨线程向 asyncio 投递数据的标准做法。

安全约束（NFR-SSH-01）：
    凭据只在 ``connect`` 调用期间以局部变量存在，连接建立后立即从保管箱销毁，
    **绝不**写入任何日志、异常信息或磁盘。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Final

from app.core.config import get_settings
from app.core.errors import ForbiddenError, ServiceDisabledError, UnauthorizedError
from app.core.security import SshCredential, write_audit_log

logger = logging.getLogger(__name__)

# 单次从 channel 读取的最大字节数。过大反而增加延迟（终端回显要求低延迟）。
_RECV_CHUNK: Final[int] = 8192

# 会话空闲超时的检查间隔（秒）
_IDLE_CHECK_INTERVAL: Final[float] = 30.0


@dataclass
class SshSessionInfo:
    """会话的对外信息（不含任何凭据）。

    Attributes:
        session_id: 会话标识。
        host: 目标主机。
        port: 端口。
        username: 用户名。
        host_key: 主机密钥指纹。
        server_banner: 服务端 banner。
        cols/rows: 终端尺寸。
        created_at: 创建时间戳。
        last_activity: 最后活动时间戳。
        closed: 是否已关闭。
        close_reason: 关闭原因。
    """

    session_id: str
    host: str
    port: int
    username: str
    host_key: str = ""
    server_banner: str = ""
    cols: int = 80
    rows: int = 24
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    closed: bool = False
    close_reason: str = ""

    def to_api(self) -> dict[str, Any]:
        """转换为前端 ``GET /api/ssh/sessions`` 期望的结构。

        Returns:
            会话信息字典（不含凭据，可安全下发）。
        """
        return {
            "session_id": self.session_id,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "host_key": self.host_key,
            "server_banner": self.server_banner,
            "cols": self.cols,
            "rows": self.rows,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "closed": self.closed,
            "close_reason": self.close_reason,
        }


class SshSession:
    """单个 SSH 会话。"""

    def __init__(
        self,
        *,
        session_id: str,
        host: str,
        port: int,
        username: str,
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        """初始化会话对象（尚未连接）。

        Args:
            session_id: 会话标识。
            host: 目标主机。
            port: 端口。
            username: 用户名。
            cols: 初始列数。
            rows: 初始行数。
        """
        self.session_id = session_id
        self.host = host
        self.port = port
        self.username = username
        self.cols = cols
        self.rows = rows

        self._client: Any = None
        self._channel: Any = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._queue: asyncio.Queue[bytes] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self.info = SshSessionInfo(
            session_id=session_id,
            host=host,
            port=port,
            username=username,
            cols=cols,
            rows=rows,
        )

    # -------------------------------------------------- 连接

    def connect(self, credential: SshCredential, *, strict_host_key: bool = False) -> None:
        """建立 SSH 连接并打开交互式 shell。

        Args:
            credential: 登录凭据（仅在此方法存活期内使用）。
            strict_host_key: True 时拒绝未知主机密钥（高安全场景）。

        Raises:
            ServiceDisabledError: 未安装 Paramiko。
            UnauthorizedError: 认证失败或网络不可达。
            ForbiddenError: 目标主机不在白名单内。
        """
        paramiko = _load_paramiko()

        settings = get_settings()
        _assert_host_allowed(self.host, settings.security.allowed_hosts)

        client = paramiko.SSHClient()
        if strict_host_key:
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            # 首次连接自动信任并写入 known_hosts（规格书 §5.2.2 主机密钥策略）
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.WarningPolicy())

        connect_kwargs: dict[str, Any] = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            # 交互式终端需要保持连接，超时只用于握手阶段
            "timeout": 15,
            "banner_timeout": 20,
            "auth_timeout": 20,
            "look_for_keys": False,  # 不允许读取本机私钥，凭据只能来自用户输入
            "allow_agent": False,
        }

        if credential.auth_method == "key":
            if not credential.private_key:
                raise UnauthorizedError("未提供私钥内容。")
            key = _parse_private_key(paramiko, credential.private_key, credential.passphrase)
            connect_kwargs["pkey"] = key
            connect_kwargs["password"] = None
        else:
            if not credential.password:
                raise UnauthorizedError("未提供登录密码。")
            connect_kwargs["password"] = credential.password

        started = time.monotonic()
        try:
            client.connect(**connect_kwargs)
        except paramiko.AuthenticationException as exc:
            raise UnauthorizedError(
                "SSH 认证失败：用户名、密码或私钥不正确。若目标主机启用了密钥登录，请改用私钥认证方式。"
            ) from exc
        except paramiko.SSHException as exc:
            raise UnauthorizedError(f"SSH 协议错误：{str(exc)[:160]}") from exc
        except OSError as exc:
            raise UnauthorizedError(
                f"无法连接到 {self.host}:{self.port} —— {exc.strerror or exc}。"
                "请确认主机可达、端口开放、防火墙已放行。"
            ) from exc

        self._client = client

        # 取主机密钥指纹（前端连接成功提示会展示）
        try:
            transport = client.get_transport()
            if transport and transport.get_remote_server_key():
                key_obj = transport.get_remote_server_key()
                self.info.host_key = f"{key_obj.get_name()} {key_obj.get_fingerprint().hex()}"
            if transport and transport.remote_version:
                self.info.server_banner = transport.remote_version
        except Exception as exc:  # noqa: BLE001 - 指纹获取失败不影响会话可用
            logger.debug("获取主机指纹失败：%s", exc)

        # 打开交互式 shell 并申请 pty
        try:
            channel = client.invoke_shell(
                term="xterm-256color",
                width=self.cols,
                height=self.rows,
            )
            channel.settimeout(0.0)  # 非阻塞，配合线程内的 select 式读取
        except Exception as exc:
            self.close(reason="无法打开交互式 shell")
            raise UnauthorizedError(
                f"已通过认证但无法打开终端会话：{str(exc)[:160]}。"
                "目标主机可能禁用了 shell 或限制了并发会话数。"
            ) from exc

        self._channel = channel
        logger.info(
            "SSH 会话 %s 已建立：%s@%s:%s（握手 %.2fs）",
            self.session_id,
            self.username,
            self.host,
            self.port,
            time.monotonic() - started,
        )

    # -------------------------------------------------- 数据桥

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue[bytes]:
        """绑定事件循环并启动读线程。

        Args:
            loop: 运行 WebSocket 处理器的事件循环。

        Returns:
            接收远端数据的队列。
        """
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=512)
        self._stop_event.clear()

        self._reader_thread = threading.Thread(
            target=self._read_loop,
            name=f"ssh-reader-{self.session_id[:8]}",
            daemon=True,
        )
        self._reader_thread.start()
        return self._queue

    def _read_loop(self) -> None:
        """在独立线程中持续读取 channel 数据并投递到事件循环。

        使用 ``channel.recv_ready()`` 轮询而非阻塞 ``recv``：
        Paramiko 的 channel 无超时参数，阻塞 recv 会在会话关闭前无法响应停止信号，
        导致读线程无法退出、会话资源泄漏（AC-11 资源回收要求）。
        """
        channel = self._channel
        queue = self._queue
        loop = self._loop
        if channel is None or queue is None or loop is None:
            return

        while not self._stop_event.is_set():
            try:
                if not channel.recv_ready():
                    if channel.exit_status_ready() and not channel.recv_ready():
                        # 远端已退出且无残留数据
                        self._emit_close("远端已关闭连接")
                        break
                    time.sleep(0.01)  # 10ms 轮询：终端交互的延迟感知阈值
                    continue

                data = channel.recv(_RECV_CHUNK)
                if not data:
                    self._emit_close("远端已关闭连接")
                    break
                self.info.last_activity = time.time()
                asyncio.run_coroutine_threadsafe(queue.put(data), loop)
            except Exception as exc:  # noqa: BLE001 - socket 异常种类多
                if not self._stop_event.is_set():
                    logger.info("SSH 会话 %s 读线程结束：%s", self.session_id, exc)
                    self._emit_close(str(exc)[:120] or "连接中断")
                break

    def _emit_close(self, reason: str) -> None:
        """通知事件循环会话已断开（投递空字节作为哨兵）。

        Args:
            reason: 断开原因。
        """
        self.info.closed = True
        self.info.close_reason = reason
        queue = self._queue
        loop = self._loop
        if queue is None or loop is None or loop.is_closed():
            return
        # 事件循环已关闭（服务正在停止）时应静默忽略，不能影响会话回收
        with contextlib.suppress(RuntimeError):
            asyncio.run_coroutine_threadsafe(queue.put(b""), loop)

    def write(self, data: bytes) -> None:
        """把客户端输入写入远端 channel。

        Args:
            data: 原始字节（终端按键序列）。
        """
        channel = self._channel
        if channel is None or self.info.closed:
            return
        try:
            channel.sendall(data)
            self.info.last_activity = time.time()
        except Exception as exc:  # noqa: BLE001
            logger.info("SSH 会话 %s 写入失败：%s", self.session_id, exc)
            self._emit_close("连接已中断")

    def resize(self, cols: int, rows: int) -> None:
        """同步终端尺寸到远端 pty（FR-SSH-06）。

        Args:
            cols: 列数。
            rows: 行数。
        """
        # 尺寸必须有下限，否则远端 vim/top 会因 0 列而崩溃
        cols = max(20, min(1000, int(cols)))
        rows = max(5, min(500, int(rows)))
        self.cols, self.rows = cols, rows
        self.info.cols, self.info.rows = cols, rows

        channel = self._channel
        if channel is None or self.info.closed:
            return
        try:
            channel.resize_pty(width=cols, height=rows)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pty resize 失败：%s", exc)

    # -------------------------------------------------- 关闭

    def close(self, *, reason: str = "客户端关闭") -> None:
        """关闭会话并释放全部资源。

        Args:
            reason: 关闭原因（写入会话信息，供前端展示）。
        """
        self._stop_event.set()
        self.info.closed = True
        self.info.close_reason = reason

        # 先关 channel 再关 client：反序会让 channel 层的关闭操作报 socket 已关闭
        for transport in (self._channel, self._client):
            if transport is None:
                continue
            try:
                transport.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 SSH 资源失败：%s", exc)

        self._channel = None
        self._client = None

        if self._reader_thread and self._reader_thread.is_alive():
            # 读线程是守护线程，最多等 1 秒；超时也不阻塞服务关闭
            self._reader_thread.join(timeout=1.0)
        self._reader_thread = None

    @property
    def is_alive(self) -> bool:
        """会话是否仍可用。"""
        if self.info.closed or self._channel is None:
            return False
        try:
            transport = self._client.get_transport() if self._client else None
            return bool(transport and transport.is_active())
        except Exception:  # noqa: BLE001
            return False

    def idle_seconds(self) -> float:
        """距最后一次活动经过的秒数。

        Returns:
            空闲秒数。
        """
        return time.time() - self.info.last_activity


# ---------------------------------------------------------------- 辅助函数


def _load_paramiko():  # noqa: ANN202
    """惰性导入 Paramiko。

    Returns:
        paramiko 模块。

    Raises:
        ServiceDisabledError: 未安装 Paramiko。
    """
    try:
        import paramiko  # type: ignore[import-not-found]

        return paramiko
    except ImportError as exc:
        raise ServiceDisabledError(
            "SSH 模块依赖 Paramiko，当前未安装。请执行：pip install paramiko",
            extra={"missing_package": "paramiko"},
        ) from exc


def _parse_private_key(paramiko: Any, key_text: str, passphrase: str | None):  # noqa: ANN202
    """解析私钥（自动识别 OpenSSH / RSA / ECDSA / Ed25519 格式）。

    Args:
        paramiko: paramiko 模块。
        key_text: 私钥文本。
        passphrase: 私钥口令。

    Returns:
        Paramiko PKey 对象。

    Raises:
        UnauthorizedError: 私钥格式非法或口令错误。
    """
    last_error: Exception | None = None
    # 依次尝试各类密钥解析器：用户不会关心私钥类型，自动识别体验最好
    for key_class in (
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
        paramiko.RSAKey,
        paramiko.DSSKey,
    ):
        try:
            return key_class.from_private_key(io.StringIO(key_text), password=passphrase or None)
        except paramiko.PasswordRequiredException as exc:
            raise UnauthorizedError("该私钥需要口令（passphrase），请在「私钥口令」中填写。") from exc
        except paramiko.SSHException as exc:
            last_error = exc
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue

    raise UnauthorizedError(
        f"私钥解析失败：{str(last_error)[:160] if last_error else '格式不受支持'}。"
        "请确认粘贴的是完整的 PEM / OpenSSH 私钥（含首尾的 BEGIN/END 行）。"
    )


def _assert_host_allowed(host: str, allowed_hosts: list[str]) -> None:
    """校验目标主机是否在白名单内（NFR-SSH-04）。

    支持精确 IP/域名 与 ``192.168.1.0/24`` 形式的 CIDR。

    Args:
        host: 目标主机。
        allowed_hosts: 白名单；空列表表示不限制。

    Raises:
        ForbiddenError: 目标主机不在白名单内。
    """
    if not allowed_hosts:
        return

    import ipaddress

    # 先做字符串精确匹配（覆盖域名场景）
    if host in allowed_hosts:
        return

    try:
        target = ipaddress.ip_address(host)
    except ValueError:
        # 是域名且未精确命中：拒绝（域名无法安全地做网段匹配）
        raise ForbiddenError(
            f"目标主机 {host} 不在允许的连接白名单内。"
            "管理员可在 config.toml 的 [security].allowed_hosts 中调整。"
        ) from None

    for entry in allowed_hosts:
        try:
            if "/" in entry:
                if target in ipaddress.ip_network(entry, strict=False):
                    return
            elif target == ipaddress.ip_address(entry):
                return
        except ValueError:
            continue

    raise ForbiddenError(
        f"目标主机 {host} 不在允许的连接白名单内（NFR-SSH-04）。"
        "管理员可在 config.toml 的 [security].allowed_hosts 中调整。"
    )


# ---------------------------------------------------------------- 会话注册表


class SessionRegistry:
    """活跃 SSH 会话注册表（线程安全）。"""

    def __init__(self) -> None:
        """初始化。"""
        self._lock = threading.RLock()
        self._sessions: dict[str, SshSession] = {}

    def create(self, **kwargs: Any) -> SshSession:
        """创建并登记一个新会话。

        Args:
            **kwargs: 传给 SshSession 的参数。

        Returns:
            SshSession 实例。

        Raises:
            ForbiddenError: 并发会话数已达上限。
        """
        limit = get_settings().ssh.max_sessions
        with self._lock:
            if len(self._sessions) >= limit:
                raise ForbiddenError(f"并发 SSH 会话数已达上限（{limit}）。请先断开不用的会话。")
            session = SshSession(**kwargs)
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> SshSession | None:
        """按 ID 取会话。

        Args:
            session_id: 会话标识。

        Returns:
            SshSession；不存在返回 None。
        """
        with self._lock:
            return self._sessions.get(session_id)

    def remove(self, session_id: str, *, reason: str = "客户端关闭") -> None:
        """移除并关闭会话。

        Args:
            session_id: 会话标识。
            reason: 关闭原因。
        """
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session:
            session.close(reason=reason)
            write_audit_log({"action": "ssh.disconnect", "session": session_id, "host": session.host})

    def list_all(self) -> list[SshSession]:
        """列出全部会话。

        Returns:
            SshSession 列表。
        """
        with self._lock:
            return list(self._sessions.values())

    def sweep_idle(self) -> int:
        """清理空闲超时的会话（FR-SSH-05）。

        Returns:
            被清理的会话数。
        """
        limit_sec = get_settings().ssh.idle_timeout_min * 60
        victims = [s.session_id for s in self.list_all() if s.idle_seconds() > limit_sec or not s.is_alive()]
        for session_id in victims:
            self.remove(session_id, reason=f"空闲超过 {get_settings().ssh.idle_timeout_min} 分钟，已自动断开")
        return len(victims)

    def close_all(self) -> None:
        """关闭全部会话（服务停止时调用）。"""
        with self._lock:
            session_ids = list(self._sessions)
        for session_id in session_ids:
            self.remove(session_id, reason="服务已停止")

    def count(self) -> int:
        """活跃会话数。

        Returns:
            会话数。
        """
        with self._lock:
            return len(self._sessions)


def require_ssh_enabled() -> None:
    """确认 SSH 模块已启用。

    Raises:
        ServiceDisabledError: 模块未启用（默认关闭，风险 R6 要求显式开启）。
    """
    if not get_settings().ssh.enabled:
        raise ServiceDisabledError(
            "SSH 模块未启用。出于安全考虑本模块默认关闭，"
            "请管理员在 config.toml 的 [ssh] 段设置 enabled = true 后重启服务。"
        )


session_registry: Final[SessionRegistry] = SessionRegistry()

__all__ = [
    "SessionRegistry",
    "SshSession",
    "SshSessionInfo",
    "require_ssh_enabled",
    "session_registry",
]
