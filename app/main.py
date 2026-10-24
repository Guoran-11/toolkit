"""FastAPI 应用入口。

装配顺序（不要随意调整）：
    1. lifespan  —— 目录准备、引擎预热、后台维护协程
    2. 中间件     —— 安全响应头、请求体积限制
    3. 异常处理器 —— 把业务异常翻译成前端约定的 ``{"detail", "code"}``
    4. 业务路由   —— /api/*、/healthz、/ws/*
    5. 静态挂载   —— **必须最后**，因为它挂在 ``/`` 上会捕获所有未匹配路径

静态挂载为什么必须在最后：FastAPI/Starlette 按注册顺序匹配路由，
``app.mount("/", StaticFiles(...))`` 一旦前置，所有 API 请求都会被它截胡并返回 404。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.auth import router as auth_router
from app.api.files import router as files_router
from app.api.system import router as system_router
from app.api.system import warm_up_engines
from app.api.tasks import router as tasks_router
from app.api.users import router as admin_users_router
from app.core.config import STATIC_DIR, ensure_directories, get_settings
from app.core.db import init_db
from app.core.engines import engine_probe
from app.core.errors import ToolkitError
from app.core.mailer import is_configured as mail_is_configured
from app.core.security import bootstrap_password, credential_vault
from app.core.storage import registry, upload_store
from app.core.tasks import get_task_manager
from app.core.verification import purge_expired as purge_expired_codes
from app.modules.audio.router import router as audio_router
from app.modules.converter.router import router as converter_router
from app.modules.markdown.router import router as markdown_router
from app.modules.ssh.router import router as ssh_router
from app.modules.ssh.session import session_registry
from app.modules.video.router import router as video_router

logger = logging.getLogger(__name__)

# 后台维护的检查间隔（秒）
_MAINTENANCE_INTERVAL: float = 300.0

# 全局维护任务句柄（用于优雅停止）
_maintenance_task: asyncio.Task[None] | None = None

# 每次使用前必须向服务器重验证的前端资源后缀（详见 _register_middleware 中的说明）。
# 只对前端资源生效，/api/* 等动态响应不干预其缓存语义。
_REVALIDATE_SUFFIXES: tuple[str, ...] = (
    ".html",
    ".js",
    ".css",
    ".json",
    ".svg",
    ".webmanifest",
    ".map",
)


# ---------------------------------------------------------------- lifespan


def _configure_logging() -> None:
    """配置根日志。

    离线单机版以 GUI 形态运行时没有控制台，日志是唯一的排障手段，
    因此同时写标准输出与 storage/logs/toolkit.log。
    """
    from app.core.config import LOG_DIR

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = []
    try:
        file_handler = logging.FileHandler(LOG_DIR / "toolkit.log", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        handlers.append(file_handler)
    except OSError:
        pass

    # 仅当存在真实的控制台时才添加 StreamHandler（pythonw 下 sys.stderr 为 None）
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        handlers.append(stream_handler)

    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    # uvicorn 的访问日志噪音较大，且与本项目的审计日志重复
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _sweep_video_artifacts() -> int:
    """清理视频模块的过期历史与残留临时目录。

    采用**惰性导入 + 全异常兜底**，原因有二：
        1. 视频模块是可选能力（``[video] enabled=false`` 时仍会加载路由），
           不能让它的导入失败拖垮整个维护循环；
        2. 维护循环本身是后台任务，任何未捕获异常都会让清理永久停摆。

    Returns:
        被清理的条目数；模块不可用时返回 0。
    """
    try:
        from app.modules.video.maintenance import sweep

        return int(sweep())
    except Exception as exc:  # noqa: BLE001 - 维护失败只记录，不影响主循环
        logger.warning("视频模块维护失败：%s", exc)
        return 0


async def _maintenance_loop() -> None:
    """周期性维护：清理过期产物、残留临时文件、空闲 SSH 会话。

    对应 NFR-03（禁止磁盘泄漏）、FR-SYS-04（自动清理）、FR-SSH-05（空闲断开）、
    AC-11（连续 100 个任务后临时目录无残留）。
    """
    settings = get_settings()
    while True:
        try:
            await asyncio.sleep(_MAINTENANCE_INTERVAL)

            purged = registry.purge_expired()
            # 上传/临时文件的兜底清理阈值取任务超时的 3 倍
            swept = upload_store.sweep_stale(max_age_sec=settings.limits.task_timeout_sec * 3)
            idle = session_registry.sweep_idle()
            # 过期验证码：留着只会让账号库缓慢膨胀，且已无任何用处
            codes = purge_expired_codes()
            # 视频模块的临时分片目录与历史记录（模块自身负责容错，失败不影响主循环）
            video_swept = _sweep_video_artifacts()

            if purged or swept or idle or codes or video_swept:
                logger.info(
                    "维护完成：清理产物 %s 个、临时文件 %s 个、空闲 SSH 会话 %s 个、"
                    "过期验证码 %s 条、视频临时项 %s 个",
                    purged,
                    swept,
                    idle,
                    codes,
                    video_swept,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 维护失败绝不能让循环退出
            logger.warning("后台维护异常：%s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期管理。

    Args:
        app: FastAPI 实例。

    Yields:
        在 yield 处应用开始对外服务；yield 之后执行关闭清理。
    """
    global _maintenance_task

    _configure_logging()
    settings = get_settings()

    # ---------- 启动 ----------
    ensure_directories()
    # 账号库建表（幂等）。放在最前面：后续 bootstrap_password 与注册流程都依赖它。
    init_db()

    # 首次启动生成初始密码（仅在未配置时）
    generated = bootstrap_password()
    if generated:
        banner = "=" * 62
        print(banner)  # noqa: T201 - 单机版必须让用户在控制台看到初始密码
        print(f"  {settings.app.name} 首次启动：已生成管理员密码")  # noqa: T201
        print(f"  用户名：{settings.server.username}")  # noqa: T201
        print(f"  密码：{generated}")  # noqa: T201
        print("  请登录后立即在「设置」中修改；此密码不会再次显示。")  # noqa: T201
        print(banner)  # noqa: T201

    # 注册已开放但邮件未配置：用户拿不到验证码，等于注册流程不可用。
    # 这属于典型的"以为配好了"的静默故障，必须在启动日志里说清楚。
    if settings.auth.allow_registration and settings.auth.require_email_verification:
        if not mail_is_configured(settings.email):
            logger.warning(
                "已开放注册且要求邮箱验证，但 SMTP 未配置（email.enabled=false 或 host 为空）："
                "注册用户将收不到验证码。请在 config.toml 的 [email] 段填入邮件服务器信息。"
            )

    # 引擎预热（后台线程，不阻塞启动）
    warm_up_engines()
    logger.info(
        "%s v%s 启动中：%s:%s（鉴权 %s）",
        settings.app.name,
        settings.app.version,
        settings.server.host,
        settings.server.port,
        "开启" if settings.server.auth_required else "关闭",
    )

    _maintenance_task = asyncio.create_task(_maintenance_loop(), name="toolkit-maintenance")

    yield

    # ---------- 关闭 ----------
    logger.info("正在停止服务…")

    if _maintenance_task is not None:
        _maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await _maintenance_task
        _maintenance_task = None

    # 关闭顺序：先断 SSH（释放 socket 与线程），再停任务池，最后清凭据
    session_registry.close_all()
    get_task_manager().shutdown()
    credential_vault.clear()
    engine_probe.invalidate()

    logger.info("服务已停止")


# ---------------------------------------------------------------- 应用工厂


def create_app() -> FastAPI:
    """构建 FastAPI 应用。

    Returns:
        完成装配的 FastAPI 实例。
    """
    settings = get_settings()

    app = FastAPI(
        title=f"{settings.app.name}（{settings.app.codename}）",
        description=(
            "自托管的本地文件处理工具站：文档转换 / SSH 终端 / 音频转换 / Markdown 预览 / 地图定位。"
            "文件全程本地处理，不上传第三方。"
        ),
        version=settings.app.version,
        lifespan=lifespan,
        # 离线单机版无需暴露交互式文档；服务器版可通过环境变量开启
        docs_url="/api/docs" if not settings.server.auth_required else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if not settings.server.auth_required else None,
    )

    _register_middleware(app)
    _register_exception_handlers(app)
    _register_routers(app)
    _mount_static(app)
    return app


# ---------------------------------------------------------------- 中间件


def _register_middleware(app: FastAPI) -> None:
    """注册安全响应头与体积限制中间件。

    Args:
        app: FastAPI 实例。
    """

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable):  # noqa: ANN202
        """注入安全响应头。

        CSP 的关键约束（与前端设计配套，见 index.html 的注释）：
            · ``script-src 'self'`` —— 禁止内联脚本与外部脚本，这是前端把
              ``theme-boot.js`` 外置的原因；
            · ``style-src 'self' 'unsafe-inline'`` —— 前端大量使用元素级 style 属性
              （``el()`` 的 style 选项），必须允许内联样式；
            · ``connect-src 'self' ws: wss:`` —— 允许 WebSocket 终端；
            · ``media-src 'self' blob:`` —— 允许音频播放器消费 Blob URL；
            · ``img-src`` 额外放行瓦片域名（高德 / OpenStreetMap）—— 地图定位模块的
              「在线底图」是**用户可切换**的选项（默认高德，瓦片失败自动退回离线底图）；
            · ``connect-src`` 额外放行逆地理接口（高德 regeo / Nominatim）——
              省市区与道路只能由在线接口给出，请求由**浏览器直连**，不经本站服务器转发，
              因此服务端不需要外网出口；两者都是 ``fetch``，必须出现在 connect-src。

        Args:
            request: 请求对象。
            call_next: 下游处理器。

        Returns:
            响应对象。
        """
        response = await call_next(request)
        settings_now = get_settings()

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        # 本应用需要用到剪贴板（终端粘贴）与全屏（终端全屏）
        response.headers["Permissions-Policy"] = "clipboard-write=(self), fullscreen=(self), geolocation=()"

        if settings_now.security.strict_csp:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                # 视频下载的结果卡片直接引用平台封面图（referrerpolicy=no-referrer），
                # 因此 img-src 必须放行三平台图片 CDN；否则封面全部裂图。
                "img-src 'self' data: blob: https://tile.openstreetmap.org https://*.is.autonavi.com "
                "https://*.douyinpic.com https://*.douyinstatic.com https://*.hdslb.com "
                "https://*.bilibili.com https://*.kuaishou.com https://*.kwimgs.com "
                "https://*.kuaishouapp.com; "
                "font-src 'self' data:; "
                "connect-src 'self' ws: wss: https://restapi.amap.com https://nominatim.openstreetmap.org; "
                "media-src 'self' blob:; "
                "worker-src 'self' blob:; "
                "object-src 'none'; "
                "frame-ancestors 'self'; "
                "base-uri 'self'; "
                "form-action 'self'"
            )
        return response

    @app.middleware("http")
    async def static_revalidation(request: Request, call_next: Callable):  # noqa: ANN202
        """让前端静态资源每次走 ETag 重验证，避免浏览器长期运行旧 JS/CSS。

        背景（2026-10-13 实际踩坑）：静态资源此前不带任何 ``Cache-Control``，
        浏览器对这类响应会启用「启发式新鲜度」（约等于资源年龄的 10%）。于是上次
        部署时缓存的旧 ``app.js`` 被判定仍然新鲜，普通刷新根本不回服务器询问，
        表现就是「功能已上线但界面里看不到」——例如登录弹窗里缺了「注册」页签。

        这里对前端资源声明 ``no-cache``：**允许缓存，但每次使用前必须用 ETag
        重验证**。内容未变时服务器只回一个 304，开销可忽略；一旦部署了新版本，
        用户刷新即可拿到，无需强制刷新或清缓存。

        Args:
            request: 请求对象。
            call_next: 下游处理器。

        Returns:
            响应对象。
        """
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.lower().endswith(_REVALIDATE_SUFFIXES):
            response.headers["Cache-Control"] = "no-cache"
        return response


# ---------------------------------------------------------------- 异常处理


def _register_exception_handlers(app: FastAPI) -> None:
    """注册异常处理器。

    Args:
        app: FastAPI 实例。
    """

    @app.exception_handler(ToolkitError)
    async def handle_toolkit_error(request: Request, exc: ToolkitError) -> JSONResponse:
        """把业务异常翻译为前端约定的错误体。

        Args:
            request: 请求对象。
            exc: 业务异常。

        Returns:
            含 ``detail`` 与 ``code`` 的 JSON 响应。
        """
        if exc.status_code >= 500:
            logger.error("业务异常 %s：%s", exc.code, exc.detail)
        else:
            logger.info("业务异常 %s：%s", exc.code, exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_body(),
            headers={"X-Error-Code": exc.code},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """把 FastAPI 的校验错误转成中文可读文案。

        前端 ``parseErrorBody`` 会把 ``detail`` 数组里的 ``msg`` 拼接展示，
        因此这里把 Pydantic 的英文消息总结成一句中文，避免用户看到
        "value is not a valid integer" 这类开发者文案。

        Args:
            request: 请求对象。
            exc: 校验异常。

        Returns:
            400 响应。
        """
        problems: list[str] = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
            message = str(error.get("msg", "")).replace("Value error, ", "")
            problems.append(f"{location}：{message}" if location else message)

        summary = "；".join(problems[:5]) or "请求参数不合法"
        logger.info("请求参数校验失败：%s", summary)
        return JSONResponse(
            status_code=400,
            content={"detail": f"请求参数不合法（{summary}）", "code": "VALIDATION_ERROR"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """统一 HTTP 异常的错误体格式。

        静态资源返回 404 时保持原样（浏览器需要标准的 404 语义），
        但 API 路径必须返回前端可解析的 JSON。

        Args:
            request: 请求对象。
            exc: HTTP 异常。

        Returns:
            统一格式的错误响应。
        """
        if not request.url.path.startswith(("/api", "/ws", "/healthz")):
            return JSONResponse(status_code=exc.status_code, content={"detail": str(exc.detail)})

        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc.detail), "code": f"HTTP_{exc.status_code}"},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """兜底处理器：绝不让裸堆栈返回给客户端（AC-12）。

        Args:
            request: 请求对象。
            exc: 任意异常。

        Returns:
            500 响应（错误体不含堆栈细节）。
        """
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "detail": "服务端内部错误，请查看服务端日志获取详情。",
                "code": "INTERNAL_ERROR",
            },
        )


# ---------------------------------------------------------------- 路由装配


def _register_routers(app: FastAPI) -> None:
    """注册全部业务路由。

    Args:
        app: FastAPI 实例。
    """
    # 系统与鉴权
    app.include_router(system_router)
    app.include_router(auth_router)
    app.include_router(tasks_router)
    app.include_router(files_router)
    # 账号管理（仅管理员）
    app.include_router(admin_users_router)

    # 四大业务模块
    app.include_router(converter_router)
    app.include_router(ssh_router)
    app.include_router(audio_router)
    app.include_router(markdown_router)
    # 第五个业务模块：视频下载（解析 / 后台下载 / 历史 / 设置）
    app.include_router(video_router)


def _mount_static(app: FastAPI) -> None:
    """挂载前端静态资源（必须最后调用）。

    Args:
        app: FastAPI 实例。
    """
    if not STATIC_DIR.exists():
        logger.error("静态资源目录不存在：%s。界面将无法访问。", STATIC_DIR)
        return

    # html=True 让 / 与 /index.html 都返回单页外壳
    # 前端使用 hash 路由（#/converter），因此不需要 history fallback
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


# 模块级应用实例（供 `uvicorn app.main:app` 使用）
app = create_app()


__all__ = ["app", "create_app", "lifespan"]
