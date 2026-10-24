"""视频下载模块路由：``/api/video/*``。

契约来源：``docs/视频下载模块-实现契约.md`` §3。分层纪律：本文件**只做协议转换**
（Pydantic 校验 + 调 service），不写业务逻辑。

所有端点统一挂载：
    · ``GuardDep``            —— 鉴权（与其它业务模块一致）；
    · ``require_video_enabled`` —— 模块关闭时 503 ``SERVICE_DISABLED``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import ClientIpDep, GuardDep
from app.modules.video import history, service, settings_store

router = APIRouter(tags=["video"], dependencies=[Depends(service.require_video_enabled)])


# ---------------------------------------------------------------- 请求模型


class ParseRequest(BaseModel):
    """解析请求体。

    Attributes:
        urls: 用户输入（链接或含链接的分享文案）。
        prefer_quality: 默认清晰度偏好（可空，空则用配置）。
    """

    urls: list[str] = Field(default_factory=list)
    prefer_quality: str | None = None


class DownloadItemModel(BaseModel):
    """单个下载项（契约 §3.3）。"""

    url: str
    platform: str | None = None
    item_id: str | None = None
    format_id: str | None = None
    output: str | None = None
    title: str | None = None
    author: str | None = None
    quality: str | None = None


class DownloadRequest(BaseModel):
    """批量下载请求体。"""

    items: list[DownloadItemModel] = Field(default_factory=list)


class SettingsPatch(BaseModel):
    """设置更新请求体（全部可选，支持部分更新）。"""

    enabled: bool | None = None
    prefer_quality: str | None = None
    prefer_format: str | None = None
    max_concurrent: int | None = None
    download_segments: int | None = None
    max_file_mb: int | None = None
    segment_size_mb: int | None = None
    platforms: dict[str, dict[str, bool]] | None = None
    cookies: dict[str, str] | None = None


# ---------------------------------------------------------------- 平台


@router.get("/api/video/platforms")
def video_platforms(_user: GuardDep) -> dict[str, Any]:
    """支持的平台、能力矩阵、开关与引擎状态。

    Args:
        _user: 鉴权主体。

    Returns:
        契约 §3.1 结构。
    """
    return service.platforms_payload()


# ---------------------------------------------------------------- 解析


@router.post("/api/video/parse")
def video_parse(
    payload: ParseRequest,
    ip: ClientIpDep,
    user: GuardDep,
) -> dict[str, Any]:
    """解析 1–20 条链接（逐条返回，单条失败不影响整批）。

    Args:
        payload: 请求体。
        ip: 客户端 IP（限流）。
        user: 鉴权主体。

    Returns:
        ``{"results": [...], "elapsed_ms": n}``。
    """
    return service.parse_urls(payload.urls, payload.prefer_quality, ip=ip, user=user)


# ---------------------------------------------------------------- 下载


@router.post("/api/video/download")
def video_download(
    payload: DownloadRequest,
    ip: ClientIpDep,
    user: GuardDep,
) -> list[dict[str, Any]]:
    """提交下载任务（返回任务数组）。

    Args:
        payload: 请求体。
        ip: 客户端 IP（限流/审计）。
        user: 鉴权主体。

    Returns:
        ``TaskRecord.to_api()`` 数组；复用命中追加 ``reused=True``。
    """
    items = [item.model_dump() for item in payload.items]
    return service.submit_downloads(items, ip=ip, user=user)


# ---------------------------------------------------------------- 历史


@router.get("/api/video/history")
def video_history(
    _user: GuardDep,
    platform: Annotated[str | None, Query(description="平台过滤")] = None,
    keyword: Annotated[str | None, Query(description="标题/作者搜索")] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100, description="每页条数")] = 20,
    page_size_snake: Annotated[
        int | None, Query(alias="page_size", ge=1, le=100, description="兼容下划线写法")
    ] = None,
) -> dict[str, Any]:
    """分页查询下载历史。

    Args:
        _user: 鉴权主体。
        platform: 平台过滤。
        keyword: 关键词。
        page: 页码。
        page_size: 每页条数（前端 camelCase）。
        page_size_snake: 下划线写法。

    Returns:
        ``{"total", "page", "page_size", "items"}``。
    """
    return history.history_store.list_items(
        platform=platform,
        keyword=keyword,
        page=page,
        page_size=page_size_snake or page_size,
    )


@router.delete("/api/video/history/{item_id}")
def video_history_remove(
    item_id: str,
    _user: GuardDep,
    platform: Annotated[str | None, Query(description="平台过滤")] = None,
) -> dict[str, Any]:
    """删除单条历史（不删产物）。

    Args:
        item_id: 作品 ID。
        _user: 鉴权主体。
        platform: 平台过滤。

    Returns:
        ``{"ok": True, "removed": n}``。
    """
    removed = history.history_store.remove(item_id, platform)
    return {"ok": True, "removed": removed}


@router.delete("/api/video/history")
def video_history_clear(_user: GuardDep) -> dict[str, Any]:
    """清空历史（不删产物，产物按 TTL 回收）。

    Args:
        _user: 鉴权主体。

    Returns:
        ``{"ok": True, "removed": n}``。
    """
    return {"ok": True, "removed": history.history_store.clear()}


# ---------------------------------------------------------------- 设置


@router.get("/api/video/settings")
def video_settings_get(_user: GuardDep) -> dict[str, Any]:
    """读取视频模块设置（Cookie 脱敏）。

    Args:
        _user: 鉴权主体。

    Returns:
        契约 §3.5 结构。
    """
    return settings_store.read_video_settings()


@router.put("/api/video/settings")
def video_settings_update(payload: SettingsPatch, _user: GuardDep) -> dict[str, Any]:
    """更新视频模块设置（部分字段）。

    Args:
        payload: 请求体。
        _user: 鉴权主体。

    Returns:
        更新后的完整设置。
    """
    patch = payload.model_dump(exclude_unset=True)
    patch = {key: value for key, value in patch.items() if value is not None}
    return settings_store.update_video_settings(patch)


__all__ = ["router"]
