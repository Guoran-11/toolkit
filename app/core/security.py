"""鉴权、凭据保管与路径安全。

三类安全职责集中于此，便于审计：
    1. **登录鉴权**：PBKDF2 密码哈希 + 自签 Token（HMAC-SHA256）。
    2. **SSH 凭据保管**：仅存内存字典，会话结束即销毁（NFR-SSH-01）。
    3. **路径与输入防护**：路径穿越拦截、命令注入防护（NFR-DC-03 / NFR-04）。

关于 Token 实现：不引入 python-jose / pyjwt，用标准库 hmac + base64 自签。
理由是本项目的 Token 只在单一服务内自用，无需 JWT 的互操作性，
少一个依赖就少一个离线打包与安全更新的负担。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from app.core.config import LOG_DIR, STORAGE_DIR, get_settings
from app.core.errors import ForbiddenError, UnauthorizedError

# PBKDF2 迭代次数：兼顾安全与单机版登录速度（约 50ms 量级）
_PBKDF2_ITERATIONS: Final[int] = 260_000
_TOKEN_TTL_SEC: Final[int] = 24 * 3600
_TOKEN_ALGO: Final[str] = "HS256-toolkit"


# ---------------------------------------------------------------- 密码哈希


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """生成密码哈希。

    存储格式：``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``
    自带参数，便于未来提高迭代次数而不破坏旧哈希的可验证性。

    Args:
        password: 明文密码。
        iterations: PBKDF2 迭代次数。

    Returns:
        可安全写入配置文件的哈希字符串。
    """
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码。

    Args:
        password: 用户提交的明文密码。
        stored: 配置文件中保存的哈希串。

    Returns:
        匹配返回 True。使用 ``hmac.compare_digest`` 做常数时间比较，防时序侧信道。
    """
    if not stored:
        return False
    try:
        algo, iterations_text, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations_text),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived.hex(), hash_hex)


# ---------------------------------------------------------------- Token


class SecretStore:
    """Token 签名密钥的持久化存储。

    密钥落盘（storage/.secret，权限 0600）而非每次启动随机生成：
    否则每次重启都会让已登录用户的 Token 全部失效，服务器版体验很差。
    该文件不是用户凭据，泄露风险与 Flask SECRET_KEY 同级。
    """

    def __init__(self, path: Path | None = None) -> None:
        """初始化。

        Args:
            path: 密钥文件路径，默认 storage/.secret。
        """
        self.path = path or (STORAGE_DIR / ".secret")
        self._lock = threading.RLock()
        self._secret: bytes = self._load_or_create()

    def _load_or_create(self) -> bytes:
        """读取密钥；不存在则生成并收紧权限。

        Returns:
            32 字节密钥。
        """
        with self._lock:
            try:
                if self.path.exists():
                    raw = self.path.read_bytes().strip()
                    if len(raw) >= 32:
                        return raw
            except OSError:
                pass

            self.path.parent.mkdir(parents=True, exist_ok=True)
            secret = secrets.token_bytes(32)
            try:
                self.path.write_bytes(secret)
                # 仅属主可读写（Windows 上该调用基本无效，但保留了 POSIX 语义）
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                # 只读文件系统等场景下退化为进程内密钥，仅影响重启后的登录态
                pass
            return secret

    @property
    def secret(self) -> bytes:
        """当前签名密钥。"""
        return self._secret


def _b64encode(raw: bytes) -> str:
    """URL 安全的 base64 编码（去掉填充）。

    Args:
        raw: 原始字节。

    Returns:
        无 ``=`` 的 base64url 字符串。
    """
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    """URL 安全 base64 解码（自动补齐填充）。

    Args:
        text: base64url 字符串。

    Returns:
        解码后的字节。

    Raises:
        ValueError: 字符串非法。
    """
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class TokenManager:
    """自签 Token 的签发与校验。"""

    def __init__(self, secret_store: SecretStore | None = None, ttl: int = _TOKEN_TTL_SEC) -> None:
        """初始化。

        Args:
            secret_store: 密钥存储。
            ttl: Token 有效期（秒）。
        """
        self._secrets = secret_store or SecretStore()
        self.ttl = ttl

    def _sign(self, payload_b64: str) -> str:
        """对载荷做 HMAC-SHA256 签名。

        Args:
            payload_b64: 已编码的载荷。

        Returns:
            base64url 编码的签名。
        """
        mac = hmac.new(self._secrets.secret, payload_b64.encode("ascii"), hashlib.sha256)
        return _b64encode(mac.digest())

    def issue(
        self,
        username: str,
        *,
        role: str = "admin",
        user_id: int | None = None,
        token_version: int = 0,
        display_name: str = "",
    ) -> dict[str, Any]:
        """签发 Token。

        Args:
            username: 用户名（数据库用户为邮箱）。
            role: 角色。
            user_id: 数据库用户主键；``None`` 表示 config.toml 里的带外超级管理员。
            token_version: 该用户的 Token 版本号，用于改密/禁用后立即失效旧 Token。
            display_name: 显示名。

        Returns:
            含 ``token`` / ``user`` / ``expires_in`` 的字典，字段与 mock.login 一致。
        """
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": username,
            "role": role,
            "iat": now,
            "exp": now + self.ttl,
            "nonce": secrets.token_hex(8),
        }
        # 只有数据库用户才带 uid/tv：超级管理员不落库，无法做版本校验
        if user_id is not None:
            payload["uid"] = int(user_id)
            payload["tv"] = int(token_version)
        payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        return {
            "token": f"{payload_b64}.{self._sign(payload_b64)}",
            "user": {
                "username": username,
                "role": role,
                "display_name": display_name or username,
            },
            "expires_in": self.ttl,
        }

    def verify(self, token: str) -> dict[str, Any]:
        """校验 Token。

        Args:
            token: 客户端提交的 Token。

        Returns:
            解码后的载荷字典。

        Raises:
            UnauthorizedError: 格式非法、签名不符或已过期。
        """
        if not token or "." not in token:
            raise UnauthorizedError("登录状态已失效，请重新登录")
        payload_b64, _, signature = token.partition(".")
        expected = self._sign(payload_b64)
        # 常数时间比较，避免签名被逐步爆破
        if not hmac.compare_digest(expected, signature):
            raise UnauthorizedError("登录状态已失效，请重新登录")
        try:
            payload = json.loads(_b64decode(payload_b64))
        except (ValueError, json.JSONDecodeError) as exc:
            raise UnauthorizedError("登录状态已失效，请重新登录") from exc
        if int(payload.get("exp", 0)) < int(time.time()):
            raise UnauthorizedError("登录已过期，请重新登录")
        return payload


# ---------------------------------------------------------------- SSH 凭据保管


@dataclass(slots=True)
class SshCredential:
    """SSH 登录凭据（**只存在于内存**）。

    该类刻意不实现 ``__repr__``/``__str__`` 的输出，以避免凭据被日志意外打印；
    同时不提供任何序列化方法，杜绝写入磁盘的可能（NFR-SSH-01）。

    Attributes:
        auth_method: ``password`` 或 ``key``。
        password: 密码（仅 password 认证）。
        private_key: 私钥文本（仅 key 认证）。
        passphrase: 私钥口令。
    """

    auth_method: str
    password: str | None = field(default=None, repr=False)
    private_key: str | None = field(default=None, repr=False)
    passphrase: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        """返回不含凭据的表示，防止日志泄露。

        Returns:
            仅含认证方式的字符串。
        """
        return f"SshCredential(auth_method={self.auth_method!r}, <credentials redacted>)"

    __str__ = __repr__

    def wipe(self) -> None:
        """擦除内存中的凭据（会话结束时调用）。

        注意：Python 字符串不可变，此处只能解除引用并置空，
        真正的内存擦除需依赖 CPython 的引用计数回收，这里做的是"尽可能早释放"。
        """
        self.password = None
        self.private_key = None
        self.passphrase = None


class CredentialVault:
    """凭据保管箱（内存字典，无任何持久化路径）。"""

    def __init__(self) -> None:
        """初始化。"""
        self._lock = threading.RLock()
        self._items: dict[str, SshCredential] = {}

    def put(self, key: str, credential: SshCredential) -> None:
        """存入凭据。

        Args:
            key: 会话 ID。
            credential: 凭据对象。
        """
        with self._lock:
            self._items[key] = credential

    def pop(self, key: str) -> SshCredential | None:
        """取出并销毁凭据。

        Args:
            key: 会话 ID。

        Returns:
            凭据对象；不存在时返回 None。
        """
        with self._lock:
            credential = self._items.pop(key, None)
        if credential:
            credential.wipe()
        return credential

    def clear(self) -> None:
        """清空全部凭据（进程退出前调用）。"""
        with self._lock:
            for credential in self._items.values():
                credential.wipe()
            self._items.clear()

    def count(self) -> int:
        """当前保管的凭据数量（用于健康检查，不暴露内容）。

        Returns:
            条目数。
        """
        with self._lock:
            return len(self._items)


# ---------------------------------------------------------------- 路径安全


def assert_within(path: Path, root: Path) -> Path:
    """断言路径位于指定根目录内，拦截路径穿越。

    先做符号链接归一化再比较，避免 ``storage/../../../etc/passwd`` 与
    指向外部的软链接绕过检查。

    Args:
        path: 待检查路径。
        root: 允许的根目录。

    Returns:
        归一化后的绝对路径。

    Raises:
        ForbiddenError: 路径越界。
    """
    resolved = Path(path).resolve()
    root_resolved = Path(root).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ForbiddenError("非法的文件路径") from exc
    return resolved


def is_safe_relative(name: str) -> bool:
    """判断文件名是否可安全用作相对路径片段。

    Args:
        name: 文件名。

    Returns:
        不含路径分隔符、上级引用与 NUL 时返回 True。
    """
    if not name or "\x00" in name:
        return False
    if any(sep in name for sep in ("/", "\\")):
        return False
    return name not in {".", ".."}


def new_session_id() -> str:
    """生成 SSH 会话标识。

    会话 ID 同时充当 WebSocket 通道的凭据（前端 ``/ws/ssh/{session_id}``），
    因此必须足够不可猜测：使用 ``secrets.token_hex(16)``（128 位熵）。
    这弥补了一个现实约束 —— 浏览器原生 ``WebSocket`` 构造函数无法自定义
    请求头，无法像 fetch 那样携带 ``Authorization: Bearer``。

    Returns:
        形如 ``ssh1a2b3c...`` 的会话标识。
    """
    return f"ssh{secrets.token_hex(16)}"


def audit_log_path() -> Path:
    """取审计日志文件路径。

    必须走 ``LOG_DIR``（即 ``STORAGE_DIR/logs``）而不是硬编码 ``BASE_DIR/storage``：
    后者会绕过 ``TOOLKIT_STORAGE_DIR`` 环境变量，在 systemd / 容器部署下把审计日志
    写到与应用存储完全无关的目录，既难排查也与文档承诺的存储位置不符。

    Returns:
        storage/logs/audit.log。
    """
    return LOG_DIR / "audit.log"


# 审计日志敏感词：命中后整行做部分脱敏（NFR-SSH-05）
_SENSITIVE_PATTERNS: Final[tuple[str, ...]] = (
    "passwd",
    "sshpass",
    "PRIVATE KEY",
    "--password",
    "-p ",
)


def redact_command(command: str, *, limit: int = 400) -> str:
    """对审计日志中的命令行做脱敏与截断。

    Args:
        command: 原始命令行。
        limit: 单条日志最大长度。

    Returns:
        脱敏后的命令行；命中敏感词时只保留到敏感词之前。
    """
    text = (command or "").strip()
    for pattern in _SENSITIVE_PATTERNS:
        index = text.find(pattern)
        if index >= 0:
            return text[:index] + "<redacted>"
    return text[:limit]


def write_audit_log(entry: dict[str, Any]) -> None:
    """追加一条审计记录（失败不影响主流程）。

    Args:
        entry: 审计条目（调用方负责脱敏）。
    """
    if not get_settings().ssh.audit_log:
        return
    path = audit_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry},
            ensure_ascii=False,
        )
        with path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except OSError:
        # 审计日志写失败不能阻断业务
        pass


# ---------------------------------------------------------------- 进程级单例

secret_store: Final[SecretStore] = SecretStore()
token_manager: Final[TokenManager] = TokenManager(secret_store)
credential_vault: Final[CredentialVault] = CredentialVault()


def bootstrap_password() -> str | None:
    """首次启动时准备初始密码。

    行为：
        - 配置里已有 ``password_hash`` → 直接返回 None（无需初始化）；
        - 环境变量提供了 ``TOOLKIT_PASSWORD`` → 用其生成哈希并回写配置；
        - 都没有 → 生成随机密码，返回明文供控制台打印。

    Returns:
        需要提示给用户的明文初始密码；无需初始化时返回 None。
    """
    settings = get_settings()
    if settings.server.password_hash:
        return None

    password = settings.server.initial_password or secrets.token_urlsafe(9)
    settings.server.password_hash = hash_password(password)
    _persist_password_hash(settings.server.password_hash)
    return password


def _persist_password_hash(password_hash: str) -> None:
    """把密码哈希写回 config.toml。

    只更新 ``[server].password_hash`` 一行，其余内容原样保留，
    避免用 toml 序列化器重写整个文件而丢失用户的注释与排版。

    Args:
        password_hash: 待写入的哈希串。
    """
    from app.core.config import CONFIG_FILE

    line = f'password_hash = "{password_hash}"'
    try:
        if CONFIG_FILE.exists():
            text = CONFIG_FILE.read_text(encoding="utf-8")
            lines = text.splitlines()
            replaced = False
            for index, raw in enumerate(lines):
                if raw.strip().startswith("password_hash"):
                    lines[index] = line
                    replaced = True
                    break
            if not replaced:
                # 找到 [server] 段落后插入
                for index, raw in enumerate(lines):
                    if raw.strip() == "[server]":
                        lines.insert(index + 1, line)
                        break
                else:
                    lines.extend(["", "[server]", line])
            CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            CONFIG_FILE.write_text(f"[server]\n{line}\n", encoding="utf-8")
    except OSError:
        # 容器内只读挂载等场景：哈希已存在于内存，本次运行依然可用
        pass
