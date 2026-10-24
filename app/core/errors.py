"""统一错误语义。

前端 api.js 的错误解析约定（见 parseErrorBody）：
    - ``{"detail": "字符串"}``          → 直接作为用户可读文案
    - ``{"detail": [{...}]}``          → FastAPI 校验错误，前端会拼接 msg
    - ``{"message": "字符串"}``         → 兼容非 FastAPI 风格
    - ``{"code": "UNSUPPORTED_PAIR"}`` → 前端可据此做程序化分支

因此本模块的错误体统一形如：
    {"detail": "<用户可读中文>", "code": "<机器可读码>"}

所有面向用户的文案都必须是「用户可读、非堆栈」——这是 §7.1 error 字段的硬性要求。
"""

from __future__ import annotations

from typing import Any

from fastapi import status


class ToolkitError(Exception):
    """业务异常基类。

    子类通过类属性声明 HTTP 状态码与错误码，避免在抛出点散落魔法值。

    Attributes:
        status_code: 返回给客户端的 HTTP 状态码。
        code: 机器可读的错误码，前端可据此分支处理。
        detail: 面向用户的中文说明。
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "BAD_REQUEST"

    def __init__(
        self,
        detail: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """初始化业务异常。

        Args:
            detail: 面向用户的中文错误说明（不得包含堆栈）。
            code: 覆盖类级错误码。
            status_code: 覆盖类级 HTTP 状态码。
            extra: 附加到错误体顶层的字段（如排队位置 queue_position）。
        """
        super().__init__(detail)
        self.detail = detail
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.extra: dict[str, Any] = extra or {}

    def to_body(self) -> dict[str, Any]:
        """构造符合前端约定的错误响应体。

        Returns:
            含 ``detail`` 与 ``code`` 的字典，附加字段平铺在最外层。
        """
        body: dict[str, Any] = {"detail": self.detail, "code": self.code}
        body.update(self.extra)
        return body


class ValidationError(ToolkitError):
    """入参校验失败。"""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "VALIDATION_ERROR"


class UnauthorizedError(ToolkitError):
    """未登录或登录态失效（前端收到 401 会清 Token 并弹登录框）。"""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "UNAUTHORIZED"


class ForbiddenError(ToolkitError):
    """已登录但无权限（如 SSH 模块未启用、目标主机不在白名单）。"""

    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"


class NotFoundError(ToolkitError):
    """资源不存在（任务、文件、会话）。"""

    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"


class ConflictError(ToolkitError):
    """资源冲突（邮箱/手机号已被注册、邀请码已被使用）。"""

    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"


class RateLimitedError(ToolkitError):
    """请求过于频繁（验证码重发、发码配额超限）。

    ``extra`` 里会带上 ``retry_after`` 秒数，前端据此做倒计时。
    """

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "RATE_LIMITED"


class PayloadTooLargeError(ToolkitError):
    """上传体积超限（前端会展示 maxUploadMb 提示）。"""

    # 413 / 422 直接写数字而非 status.HTTP_*：
    # Starlette 已把这两个常量的名字改过（HTTP_413_CONTENT_TOO_LARGE /
    # HTTP_422_UNPROCESSABLE_CONTENT），沿用旧名会触发弃用警告、跟进新名又会
    # 抬高最低版本要求。数字字面量是两个版本区间都稳定且无警告的写法。
    status_code = 413
    code = "FILE_TOO_LARGE"


class UnsupportedFormatError(ToolkitError):
    """文件类型不在白名单，或 magic bytes 与扩展名不符（NFR-04）。"""

    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "UNSUPPORTED_FORMAT"


class UnsupportedPairError(ToolkitError):
    """转换方向不支持（矩阵中标记为 ×）。"""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "UNSUPPORTED_PAIR"


class QueueFullError(ToolkitError):
    """队列溢出，返回 429 并告知排队位置（NFR-02）。"""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "QUEUE_FULL"


class EngineUnavailableError(ToolkitError):
    """所需外部引擎缺失或不可用（附安装指引，避免用户面对裸 500）。"""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "ENGINE_UNAVAILABLE"


class NoUsableOutputError(ToolkitError):
    """请求的资源集合中没有任何可用产物（业务状态冲突）。

    与 ``ConversionFailedError`` 的区别：后者表示「引擎执行失败」，属于服务端
    处理异常；本异常表示「请求的资源当前处于不可用状态」，是客户端可预期的
    状态冲突，因此用 409 而不是 422。

    典型场景：批量打包时所选任务全部失败或产物已过期。前端只需提示用户重新
    选择；若沿用 422，API 调用方会误判为自己的请求参数格式有误。
    """

    status_code = status.HTTP_409_CONFLICT
    code = "NO_USABLE_OUTPUT"


class ConversionFailedError(ToolkitError):
    """引擎执行失败（超时、崩溃、输出非法）。"""

    status_code = 422
    code = "CONVERSION_FAILED"


class TaskTimeoutError(ConversionFailedError):
    """任务超时。"""

    code = "TASK_TIMEOUT"


class DecryptError(ToolkitError):
    """音频解封装失败。

    注意：涉及 DRM 严格保护的文件必须走 ``code='DRM_PROTECTED'`` 分支，
    明确告知"不支持"而非尝试破解（§5.3.4 NFR-AU-03 合规红线）。
    """

    status_code = 422
    code = "DECRYPT_FAILED"


class ServiceDisabledError(ToolkitError):
    """功能模块未启用（如 SSH 默认关闭）。"""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "SERVICE_DISABLED"


# ---------------------------------------------------------------- 视频下载模块
#
# 视频模块的错误码在 PRD 4.11 有完整清单，前端按 code 做分支：
#   LOGIN_REQUIRED / COOKIE_INVALID → 引导设置页
#   RISK_CONTROL / RATE_LIMITED     → 展示倒计时
#   VIP_REQUIRED                    → 置灰不下载
# 因此这些错误**必须**是独立子类，不能复用泛化的 422/502。


class VideoError(ToolkitError):
    """视频模块业务异常基类。"""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "VIDEO_ERROR"


class LinkUnrecognizedError(VideoError):
    """输入里没有识别到任何作品链接。"""

    code = "LINK_UNRECOGNIZED"


class PlatformUnsupportedError(VideoError):
    """域名不在三平台白名单内。"""

    code = "PLATFORM_UNSUPPORTED"


class UrlBlockedError(VideoError):
    """SSRF 校验命中（内网 / 回环 / 元数据地址 / 非法 scheme）。"""

    code = "URL_BLOCKED"


class ShortlinkError(VideoError):
    """短链展开失败（跳转链断裂、超过 5 跳、目标不在白名单）。"""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "SHORTLINK_EXPAND_FAILED"


class VideoParseError(VideoError):
    """解析失败（页面结构变化、缺少元数据）。"""

    status_code = 422
    code = "PARSE_FAILED"


class ItemNotFoundError(VideoError):
    """作品不存在 / 已删除 / 私密 / 审核中。"""

    status_code = status.HTTP_404_NOT_FOUND
    code = "ITEM_NOT_FOUND"


class LoginRequiredError(VideoError):
    """该清晰度需要登录态，但当前未配置 Cookie（允许降级下载）。"""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "LOGIN_REQUIRED"


class CookieInvalidError(VideoError):
    """已配置的 Cookie 失效（SESSDATA 过期等）。"""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "COOKIE_INVALID"


class VipRequiredError(VideoError):
    """该清晰度为会员专属，明确不下载（合规红线，不做任何绕过）。"""

    status_code = status.HTTP_403_FORBIDDEN
    code = "VIP_REQUIRED"


class RegionLockedError(VideoError):
    """作品在服务端所在地区不可用。"""

    status_code = status.HTTP_403_FORBIDDEN
    code = "REGION_LOCKED"


class RiskControlError(ToolkitError):
    """平台风控中（熔断期内）。``extra`` 带 ``retry_after`` 秒数。"""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "RISK_CONTROL"


class DownloadFailedError(VideoError):
    """下载中断且重试耗尽。"""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "DOWNLOAD_FAILED"


class OutputTooLargeError(VideoError):
    """产物超过单文件上限。"""

    status_code = 413
    code = "OUTPUT_TOO_LARGE"


class DiskFullError(VideoError):
    """磁盘可用空间低于阈值，拒绝新任务。"""

    status_code = 507
    code = "DISK_FULL"
