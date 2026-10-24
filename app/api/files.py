"""结果下载端点：``/api/download/{file_id}``。

契约来源：``app/static/js/api.js`` 的 ``api.download`` 与 ``api.fileUrl``。

本端点的两种消费方式决定了它的安全设计：
    1. ``fetch → Blob → <a download>``（``api.download``）—— 需要常规鉴权。
    2. ``<audio src>`` / ``<iframe src>``（``api.fileUrl``）—— 浏览器原生发起的请求
       **无法携带 Authorization 头**，这是 Web 平台的硬限制，无法绕过。

因此本端点采用 **capability（能力）式访问控制**：
    ``file_id`` 是 ``uuid4().hex``，122 位随机熵，且仅在任务成功后由服务端下发，
    不会出现在任何可枚举的位置。持有它即被视为有权访问该文件 ——
    这与 Google Docs 的"知道链接即可访问"是同一模型，也是自托管工具站的合理取舍。

配套的加固措施：
    · 响应头加 ``X-Content-Type-Options: nosniff``，禁止浏览器做 MIME 嗅探；
    · HTML 产物强制 ``Content-Disposition: attachment`` 并附加 ``CSP: sandbox``，
      即使用户把链接直接贴进地址栏，也不会在同源下执行其中的脚本；
    · PDF / 图片 / 音频保持 ``inline``，因为预览框与播放器依赖内联渲染。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.api.deps import OptionalUserDep
from app.core.errors import NotFoundError
from app.core.storage import registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["files"])

# 需要强制下载（而非内联渲染）的 MIME 前缀
_ATTACHMENT_MIME_PREFIXES: tuple[str, ...] = (
    "text/html",
    "image/svg",
    "application/xhtml",
)


def _content_disposition(mime: str) -> str:
    """按 MIME 决定内容呈现方式。

    Args:
        mime: 文件 MIME。

    Returns:
        ``inline`` 或 ``attachment``。
    """
    return "attachment" if mime.lower().startswith(_ATTACHMENT_MIME_PREFIXES) else "inline"


@router.get("/api/download/{file_id}")
def download(file_id: str, _user: OptionalUserDep) -> FileResponse:
    """下载任务产物。

    Args:
        file_id: 产物标识。
        _user: 登录态（可为 None：见模块 docstring 的 capability 说明）。

    Returns:
        文件响应流。

    Raises:
        NotFoundError: 标识非法、不存在、已过期或文件已被清理。
    """
    record = registry.get(file_id)
    path = Path(record.path)
    if not path.exists():
        # registry.get 已做过检查，这里是防御竞态（清理线程恰好删掉了文件）
        raise NotFoundError("文件已被清理，请重新发起转换")

    disposition = _content_disposition(record.mime)

    headers: dict[str, str] = {
        # 禁止 MIME 嗅探：避免被伪装成安全类型的文件在浏览器里被误解析执行
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=300",
    }
    if disposition == "attachment":
        # 下载即落盘，绝不在同源上下文里渲染 HTML/SVG
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"

    logger.debug("下载产物 %s（%s，%s）", file_id, record.name, disposition)

    return FileResponse(
        path=str(path),
        media_type=record.mime,
        filename=record.name,  # Starlette 会做 RFC 5987 编码，中文名安全
        content_disposition_type=disposition,
        headers=headers,
    )


__all__ = ["router"]
