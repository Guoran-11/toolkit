"""配置中心。

设计要点：
1. 配置源优先级：环境变量 > config.toml > 内置默认值。环境变量用于 Docker/CI 注入，
   配置文件用于离线单机版的可视化编辑，二者互不干扰。
2. 全部路径在加载时即解析为绝对路径。子进程调用（LibreOffice / FFmpeg）时工作目录不可控，
   相对路径会导致"引擎明明存在却找不到"的隐性故障。
3. 不使用 pydantic-settings 的 TomlConfigSettingsSource：该能力依赖 pydantic-settings >= 2.2，
   版本漂移会让离线包在客户机上直接起不来。这里用标准库 tomllib + 显式 env 覆盖，
   零版本风险，且校验逻辑依然由 Pydantic v2 保证。
"""

from __future__ import annotations

import logging
import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 路径常量

# config.py -> core -> app -> <项目根>
BASE_DIR: Path = Path(__file__).resolve().parents[2]

APP_DIR: Path = BASE_DIR / "app"
STATIC_DIR: Path = APP_DIR / "static"
CONFIG_FILE: Path = BASE_DIR / "config.toml"


def _resolve_dir(env_key: str, default: Path) -> Path:
    """解析可被环境变量覆盖的目录。

    用途有二：
        1. **测试隔离** —— 测试夹具把 storage 指向临时目录，避免写脏真实数据；
        2. **部署灵活性** —— 容器/服务器可把 storage 挂到独立数据盘。

    Args:
        env_key: 环境变量名。
        default: 环境变量缺失时的默认目录。

    Returns:
        解析为绝对路径的目录。
    """
    raw = os.environ.get(env_key, "").strip()
    if not raw:
        return default
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    return candidate.resolve()


# 存储与运行时目录均支持环境变量覆盖（见 _resolve_dir）
STORAGE_DIR: Path = _resolve_dir("TOOLKIT_STORAGE_DIR", BASE_DIR / "storage")
RUNTIME_DIR: Path = _resolve_dir("TOOLKIT_RUNTIME_DIR", BASE_DIR / "runtime")

UPLOAD_DIR: Path = STORAGE_DIR / "uploads"
TEMP_DIR: Path = STORAGE_DIR / "temp"
OUTPUT_DIR: Path = STORAGE_DIR / "outputs"
LOG_DIR: Path = STORAGE_DIR / "logs"
# 账号体系数据库（SQLite 单文件）。跟随 STORAGE_DIR，备份即拷贝此文件。
DB_PATH: Path = STORAGE_DIR / "toolkit.db"


class ServerConfig(BaseModel):
    """HTTP 服务与鉴权开关。"""

    host: str = "127.0.0.1"
    port: int = Field(default=5000, ge=1, le=65535)
    # 管理员用户名。本期为单管理员模式（待确认事项 Q4）
    username: str = "admin"
    auth_required: bool = False
    password_hash: str = ""
    # 首次启动自动生成的初始密码（仅内存中持有，用于打印到控制台）
    initial_password: str | None = None
    # 局域网访问开关：勾选后强制要求鉴权（§9.1 第 6 条）
    allow_lan: bool = False


class LimitsConfig(BaseModel):
    """资源限额。前端 config.js 的 limits 段必须与此保持一致。"""

    max_upload_mb: int = Field(default=100, ge=1, le=4096)
    # 音频模块单文件上限，**独立于**上方通用上限：音频走流式转码，内存占用与
    # 体积无关，且无损格式（flac/wav/ape）动辄上百 MB，因此单独放宽。
    max_audio_upload_mb: int = Field(default=200, ge=1, le=4096)
    max_batch_files: int = Field(default=20, ge=1, le=200)
    workers: int = Field(default=4, ge=1, le=64)
    task_timeout_sec: int = Field(default=300, ge=5, le=7200)
    output_ttl_hours: int = Field(default=24, ge=0, le=720)
    # 队列上限：超出返回 429（NFR-02）
    max_queue: int = Field(default=200, ge=1, le=10000)

    @property
    def max_upload_bytes(self) -> int:
        """上传体积上限（字节）。"""
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_audio_upload_bytes(self) -> int:
        """音频模块上传体积上限（字节）。"""
        return self.max_audio_upload_mb * 1024 * 1024


class EnginesConfig(BaseModel):
    """外部引擎可执行文件路径。

    既接受显式路径（相对项目根即可），也接受裸命令名（此时交由 PATH 查找）。
    """

    libreoffice: str = "runtime/libreoffice/program/soffice"
    ffmpeg: str = "runtime/ffmpeg"
    chromium: str = "runtime/chromium/chrome"
    # 视频下载的可选兜底解析引擎；未安装时自动跳过该降级链，不影响主流程
    ytdlp: str = "yt-dlp"
    # LibreOffice 单次调用超时（§5.1.4 NFR-DC-01）
    libreoffice_timeout_sec: int = Field(default=120, ge=10, le=1800)


class PlatformToggle(BaseModel):
    """单个视频平台的开关（``[video.platforms.<name>]``）。

    版权方/平台通知时，管理员只需把对应平台置 false 即可在 1 分钟内下线该能力
    （PRD NFR-VD-C04），无需改代码或重启进程（配置热读）。
    """

    enabled: bool = True


def _default_platform_toggles() -> dict[str, PlatformToggle]:
    """三个支持平台的默认开关（全部启用）。

    Returns:
        平台名 → 开关对象。
    """
    return {name: PlatformToggle() for name in ("douyin", "bilibili", "kuaishou")}


class VideoConfig(BaseModel):
    """视频下载模块配置（``[video]`` 段）。

    字段与 PRD 4.10 / 8.3 对齐；这里的默认值同时是「配置缺失时的兜底值」，
    因此必须与 ``config.toml`` 中注释的默认值保持一致。
    """

    # 模块总开关。false 时所有 /api/video/* 返回 503 SERVICE_DISABLED
    enabled: bool = True
    # 默认清晰度偏好：4K / 1080P / 720P / 480P
    prefer_quality: str = "1080P"
    # 默认输出：mp4 / mp3 / m4a
    prefer_format: str = "mp4"
    # 同时下载任务数（1-4）。过高易触发平台风控
    max_concurrent: int = Field(default=2, ge=1, le=4)
    # 单任务分片并发（1-8）
    download_segments: int = Field(default=4, ge=1, le=8)
    # 分片大小（MB）
    segment_size_mb: int = Field(default=2, ge=1, le=32)
    # 单文件大小上限（MB）
    max_file_mb: int = Field(default=2048, ge=1, le=10240)
    # 视频任务超时（秒），独立于通用 limits.task_timeout_sec
    task_timeout_sec_video: int = Field(default=600, ge=30, le=7200)
    # 解析结果缓存（秒）
    parse_cache_ttl_sec: int = Field(default=600, ge=0, le=86400)
    # 产物复用窗口（小时）
    dedupe_window_hours: int = Field(default=24, ge=0, le=720)
    # 最低可用磁盘（GB），低于则拒绝新任务
    min_free_gb: int = Field(default=2, ge=0, le=1024)
    # 单次最多解析链接数
    max_urls: int = Field(default=20, ge=1, le=100)
    # 历史记录上限
    history_limit: int = Field(default=200, ge=1, le=5000)
    # 平台开关。TOML 写法：platforms.douyin = { enabled = true }
    platforms: dict[str, PlatformToggle] = Field(default_factory=_default_platform_toggles)

    def platform_enabled(self, name: str) -> bool:
        """判断某平台是否启用（未知平台视为启用，由适配器注册表兜底拒绝）。

        Args:
            name: 平台标识（douyin / bilibili / kuaishou）。

        Returns:
            是否启用。
        """
        toggle = self.platforms.get(name)
        return True if toggle is None else bool(toggle.enabled)



class SshConfig(BaseModel):
    """SSH 模块配置。默认关闭（风险 R6：避免误开成为公网跳板）。"""

    enabled: bool = False
    lan_only: bool = True
    idle_timeout_min: int = Field(default=30, ge=1, le=1440)
    audit_log: bool = True
    # 最大并发会话数，防止资源耗尽
    max_sessions: int = Field(default=16, ge=1, le=256)


class SecurityConfig(BaseModel):
    """安全策略。"""

    # SSH 目标白名单，空列表表示不限（NFR-SSH-04）
    allowed_hosts: list[str] = Field(default_factory=list)
    # 是否下发严格 CSP（开发期可关闭，便于浏览器调试）
    strict_csp: bool = True
    # 主机密钥策略：'auto' 首次自动信任；'strict' 拒绝未知主机（NFR-SSH 主机密钥策略）
    ssh_host_key_policy: str = "auto"
    # 可信反向代理地址（IP 或 CIDR）。只有来自这些地址的请求，其
    # X-Forwarded-For / X-Real-IP 才会被采信——否则任何人都能伪造来源 IP，
    # 从而绕过登录失败限流、污染审计日志与 SSH 白名单判定。
    # 回环地址始终视为可信（Nginx 与应用同机部署是默认拓扑）。
    trusted_proxies: list[str] = Field(default_factory=lambda: ["127.0.0.1", "::1"])


class AuthConfig(BaseModel):
    """账号体系策略（邮箱注册 / 验证码 / 密码强度）。

    设计取向：默认**不开放注册**。自托管工具一旦暴露在网络上，
    开放注册等于把服务变成公共资源；需要时必须显式打开。
    """

    # 是否开放邮箱注册
    allow_registration: bool = False
    # 新注册用户的默认角色：user / admin
    default_role: str = "user"
    # 注册是否需要邮箱验证码（关闭后仅用于内网调试，切勿在生产关闭）
    require_email_verification: bool = True
    # 登录是否叠加邮箱验证码（二次验证）
    login_require_code: bool = False
    # 密码最小长度
    min_password_length: int = Field(default=8, ge=6, le=128)
    # 验证码有效期（秒）
    code_ttl_sec: int = Field(default=300, ge=60, le=3600)
    # 同一目标重发验证码的最小间隔（秒）
    code_resend_sec: int = Field(default=60, ge=10, le=3600)
    # 同一目标在有效期内允许的最大校验尝试次数
    code_max_attempts: int = Field(default=5, ge=1, le=20)
    # 同一 IP 每小时最多发码次数（防刷）
    code_ip_hourly_limit: int = Field(default=20, ge=1, le=1000)
    # 注册用户默认状态：active（直接可用）/ pending（需管理员审批）
    default_status: str = "active"


class EmailConfig(BaseModel):
    """发信配置（标准 SMTP）。

    自建邮件服务器（如 BillionMail）与第三方邮箱都走这一套：
    host/port/security/username/password 指向 SMTP 提交端口即可。
    """

    # 是否启用真实发信；关闭时退化为「日志发送器」（验证码写日志，便于开发联调）
    enabled: bool = False
    host: str = ""
    # 465=SSL(implicit) / 587=STARTTLS / 25=明文（多数云厂商封禁，不推荐）
    port: int = Field(default=465, ge=1, le=65535)
    # ssl | starttls | none
    security: str = "ssl"
    username: str = ""
    # SMTP 授权码 / 邮箱密码。切勿提交到版本库。
    password: str = ""
    # 发件人地址；留空时回退到 username
    from_address: str = ""
    from_name: str = "全能工具站"
    timeout_sec: int = Field(default=15, ge=3, le=120)
    # 是否校验 SMTP 服务器 TLS 证书。
    # 自建邮件服务器（BillionMail / Postfix）常用自签名证书，此时必须置 false，
    # 否则会因 CERTIFICATE_VERIFY_FAILED 而无法发信。
    # ⚠️ 公网第三方邮件服务请保持 true，置 false 会失去对中间人攻击的防护。
    tls_verify: bool = True
    # 站点对外访问地址，用于邮件正文中的链接
    site_url: str = ""


class SmsConfig(BaseModel):
    """短信通道配置（可插拔）。

    provider:
        · ``log``     —— 只写日志（默认，无需任何凭据）
        · ``webhook`` —— POST JSON 到自建网关，由网关对接具体厂商
    """

    enabled: bool = False
    provider: str = "log"
    webhook_url: str = ""
    webhook_token: str = ""
    timeout_sec: int = Field(default=10, ge=3, le=60)
    # 短信签名（若网关需要）
    sign_name: str = ""


class AppConfig(BaseModel):
    """应用级元信息。"""

    name: str = "全能工具站"
    codename: str = "ToolKit"
    version: str = "1.0.0"
    # 输出文件名的安全字符集之外一律替换为该占位符
    demo_mode: bool = False


class Settings(BaseModel):
    """聚合配置对象。"""

    app: AppConfig = Field(default_factory=AppConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    engines: EnginesConfig = Field(default_factory=EnginesConfig)
    ssh: SshConfig = Field(default_factory=SshConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    sms: SmsConfig = Field(default_factory=SmsConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)

    @field_validator("server")
    @classmethod
    def _force_auth_on_lan(cls, v: ServerConfig) -> ServerConfig:
        """局域网暴露时强制开启鉴权（§9.1 第 6 条为硬性约束，不接受配置失误）。"""
        if v.allow_lan:
            v.auth_required = True
        return v

    # ------------------------------------------------------------ 派生属性

    @property
    def versions(self) -> dict[str, str]:
        """对外暴露的版本信息（/healthz 使用）。"""
        return {
            "app": self.app.version,
            "codename": self.app.codename,
        }


# ---------------------------------------------------------------- 加载逻辑

# 环境变量覆盖表：ENV_KEY -> (section, field)
_ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "TOOLKIT_HOST": ("server", "host"),
    "TOOLKIT_PORT": ("server", "port"),
    "TOOLKIT_USERNAME": ("server", "username"),
    "TOOLKIT_AUTH_REQUIRED": ("server", "auth_required"),
    "TOOLKIT_PASSWORD": ("server", "initial_password"),
    "TOOLKIT_MAX_UPLOAD_MB": ("limits", "max_upload_mb"),
    "TOOLKIT_MAX_AUDIO_UPLOAD_MB": ("limits", "max_audio_upload_mb"),
    "TOOLKIT_WORKERS": ("limits", "workers"),
    "TOOLKIT_OUTPUT_TTL_HOURS": ("limits", "output_ttl_hours"),
    "TOOLKIT_LIBREOFFICE": ("engines", "libreoffice"),
    "TOOLKIT_FFMPEG": ("engines", "ffmpeg"),
    "TOOLKIT_CHROMIUM": ("engines", "chromium"),
    "TOOLKIT_SSH_ENABLED": ("ssh", "enabled"),
    "TOOLKIT_ALLOW_REGISTRATION": ("auth", "allow_registration"),
    "TOOLKIT_EMAIL_ENABLED": ("email", "enabled"),
    "TOOLKIT_EMAIL_HOST": ("email", "host"),
    "TOOLKIT_EMAIL_PORT": ("email", "port"),
    "TOOLKIT_EMAIL_SECURITY": ("email", "security"),
    "TOOLKIT_EMAIL_USERNAME": ("email", "username"),
    "TOOLKIT_EMAIL_PASSWORD": ("email", "password"),
    "TOOLKIT_EMAIL_FROM": ("email", "from_address"),
    "TOOLKIT_EMAIL_FROM_NAME": ("email", "from_name"),
    "TOOLKIT_SITE_URL": ("email", "site_url"),
    "TOOLKIT_SMS_ENABLED": ("sms", "enabled"),
    "TOOLKIT_SMS_PROVIDER": ("sms", "provider"),
    "TOOLKIT_SMS_WEBHOOK_URL": ("sms", "webhook_url"),
    "TOOLKIT_SMS_WEBHOOK_TOKEN": ("sms", "webhook_token"),
    # 视频下载模块（PRD 8.3）。平台级开关不走环境变量：它们是运维/合规开关，
    # 通过 config.toml 或管理接口调整，避免一次 env 覆盖把三个平台全部改掉。
    "TOOLKIT_VIDEO_ENABLED": ("video", "enabled"),
    "TOOLKIT_VIDEO_PREFER_QUALITY": ("video", "prefer_quality"),
    "TOOLKIT_VIDEO_PREFER_FORMAT": ("video", "prefer_format"),
    "TOOLKIT_VIDEO_MAX_CONCURRENT": ("video", "max_concurrent"),
    "TOOLKIT_VIDEO_DOWNLOAD_SEGMENTS": ("video", "download_segments"),
    "TOOLKIT_VIDEO_MAX_FILE_MB": ("video", "max_file_mb"),
    "TOOLKIT_VIDEO_TASK_TIMEOUT": ("video", "task_timeout_sec_video"),
    "TOOLKIT_VIDEO_MAX_URLS": ("video", "max_urls"),
    "TOOLKIT_VIDEO_MIN_FREE_GB": ("video", "min_free_gb"),
}

_BOOL_ENV = {"true", "1", "yes", "on"}


def _coerce(raw: str, field_type: Any) -> Any:
    """把环境变量的字符串转成目标类型。

    Args:
        raw: 环境变量的原始字符串值。
        field_type: Pydantic 字段类型（通常为 ``type(annotation)``）。

    Returns:
        转换后的值；无法识别时原样返回字符串，交由 Pydantic 校验报错。
    """
    if field_type is bool:
        return raw.strip().lower() in _BOOL_ENV
    if field_type is int:
        try:
            return int(raw)
        except ValueError:
            return raw
    return raw


def ensure_directories() -> None:
    """确保运行时目录存在。

    storage/ 与 runtime/ 可能因用户手工清理或首次运行而缺失，
    启动时统一补齐，避免后续写盘抛出 FileNotFoundError。
    """
    for path in (STORAGE_DIR, UPLOAD_DIR, TEMP_DIR, OUTPUT_DIR, LOG_DIR, RUNTIME_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _load_toml() -> dict[str, Any]:
    """读取 config.toml；文件缺失或损坏时回退为空配置（使用默认值）。

    **失败必须可见**：静默回落默认值会让 ``auth_required`` / ``password_hash``
    / ``ssh.*`` 等安全配置在用户毫无察觉的情况下失效（历史事故：一个非法布尔值
    导致整份配置被忽略，且每次启动重新生成管理员密码）。因此解析失败时打
    ERROR 日志，而不是默默返回空字典。

    Returns:
        解析后的字典；文件缺失或解析失败时返回空字典以保障服务可启动。
    """
    if not CONFIG_FILE.exists():
        return {}
    try:
        with CONFIG_FILE.open("rb") as fp:
            return tomllib.load(fp)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        # 配置损坏不应导致服务无法启动，但必须让用户立刻知道"配置没生效"。
        logger.error(
            "配置文件 %s 解析失败，已整体回退到内置默认值"
            "（其中鉴权、SSH 白名单等安全配置均不会生效）：%s",
            CONFIG_FILE,
            exc,
        )
        return {}


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """把环境变量合并进配置字典（就地修改并返回）。

    Args:
        data: 来自 config.toml 的原始配置字典。

    Returns:
        叠加环境变量后的配置字典。
    """
    for env_key, (section, field) in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        data.setdefault(section, {})[field] = raw

    # 类型化：section 已构造为 dict 后，用目标模型的注解做转换
    model_map = {
        "server": ServerConfig,
        "limits": LimitsConfig,
        "engines": EnginesConfig,
        "ssh": SshConfig,
        "auth": AuthConfig,
        "email": EmailConfig,
        "sms": SmsConfig,
        "video": VideoConfig,
    }
    for section, model in model_map.items():
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for field_name, value in list(block.items()):
            if isinstance(value, str):
                annotation = model.model_fields[field_name].annotation
                block[field_name] = _coerce(value, annotation)
    return data


def load_settings() -> Settings:
    """构建全局配置对象。

    Returns:
        校验通过且路径已解析的 Settings 实例。

    Raises:
        pydantic.ValidationError: 配置值非法（如端口越界）时抛出，让问题在启动期暴露。
    """
    data = _apply_env_overrides(_load_toml())
    return Settings.model_validate(data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取进程级单例配置。

    Returns:
        Settings 实例（带 lru_cache，重复调用零开销）。
    """
    return load_settings()


def resolve_engine_path(configured: str) -> Path | None:
    """把配置中的引擎路径解析为绝对路径。

    解析顺序：
        1. 绝对路径 → 直接使用（存在才返回）；
        2. 相对路径 → 相对项目根解析；
        3. 裸命令名 → 在 PATH 中查找（离线包通常不适用，但便于开发机复用系统安装）。

    Args:
        configured: 配置中的引擎路径或命令名。

    Returns:
        可用的绝对路径；找不到时返回 None（由调用方给出友好提示）。
    """
    if not configured:
        return None

    candidate = Path(configured)
    if candidate.is_absolute():
        return candidate if candidate.exists() else None

    # 相对路径优先按项目根解析（配置示例写作 runtime/ffmpeg）
    root_relative = (BASE_DIR / configured).resolve()
    if root_relative.exists():
        return root_relative

    # 裸命令名：交给 PATH（Windows 需补 .exe 后缀）
    from shutil import which

    found = which(configured)
    if found:
        return Path(found)
    if os.name == "nt":
        found = which(f"{configured}.exe")
        if found:
            return Path(found)
    return None


# 便于其他模块直接引用
settings: Settings = get_settings()
