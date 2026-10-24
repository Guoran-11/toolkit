"""统一任务中心端点：``/api/tasks*``。

契约来源：``app/static/js/api.js`` 的 ``api.tasks``。

    前端实际调用：
        ``api.tasks.list({ pageSize: 200 })``                → GET  /api/tasks?pageSize=200
        ``api.tasks.get(taskId)``                            → GET  /api/tasks/{id}
        ``api.tasks.cancel(taskId)``                         → DELETE /api/tasks/{id}
        ``api.tasks.remove(taskId)``                         → DELETE /api/tasks/{id}?purge=true

    注意查询参数名是 **camelCase ``pageSize``**（前端直接把对象丢进 URLSearchParams），
    而响应里的字段名是 ``page_size``（snake_case，来自 mock.listTasks）。
    这看起来不一致，但两边都必须严格照做，否则前端会拿不到数据。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import ConfigDep, GuardDep
from app.core.errors import ValidationError
from app.core.tasks import TERMINAL_STATUSES, get_task_manager

router = APIRouter(tags=["tasks"])


@router.get("/api/tasks")
def list_tasks(
    _user: GuardDep,
    settings: ConfigDep,
    status: Annotated[str | None, Query(description="按状态过滤；all 或留空表示不过滤")] = None,
    module: Annotated[str | None, Query(description="按模块过滤；all 或留空表示不过滤")] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=500, description="每页条数")] = 50,
    page_size_snake: Annotated[
        int | None, Query(alias="page_size", ge=1, le=500, description="兼容下划线写法")
    ] = None,
) -> dict[str, Any]:
    """分页列出任务（按创建时间倒序）。

    Args:
        _user: 鉴权主体。
        settings: 全局配置。
        status: 状态过滤。
        module: 模块过滤。
        page: 页码。
        page_size: 每页条数（前端使用 camelCase 参数名）。
        page_size_snake: 下划线写法的等价参数（为第三方调用方保留）。

    Returns:
        ``{"total": n, "page": p, "page_size": s, "items": [...]}``。

    Raises:
        ValidationError: 状态值非法。
    """
    valid_statuses = {"all", *TERMINAL_STATUSES, "pending", "running"}
    if status and status not in valid_statuses:
        raise ValidationError(f"未知的任务状态：{status}。可选：{', '.join(sorted(valid_statuses))}")

    effective_page_size = page_size_snake or page_size
    effective_page_size = min(effective_page_size, settings.limits.max_batch_files * 10)

    return get_task_manager().list(
        status=status,
        module=module,
        page=page,
        page_size=effective_page_size,
    )


@router.get("/api/tasks/{task_id}")
def get_task(task_id: str, _user: GuardDep) -> dict[str, Any]:
    """取任务详情（前端轮询入口，间隔 1 秒）。

    Args:
        task_id: 任务标识。
        _user: 鉴权主体。

    Returns:
        任务的 API 表示。

    Raises:
        NotFoundError: 任务不存在或已被清理。
    """
    return get_task_manager().get(task_id).to_api()


@router.delete("/api/tasks/{task_id}")
def delete_task(
    task_id: str,
    _user: GuardDep,
    purge: Annotated[bool, Query(description="true 表示连同产物一起删除")] = False,
) -> dict[str, Any]:
    """取消或删除任务。

    语义按 ``purge`` 参数区分，与前端两个方法的映射关系：
        · ``DELETE /api/tasks/{id}``               —— 取消（``cancel``）
        · ``DELETE /api/tasks/{id}?purge=true``    —— 删除记录与产物（``remove``）

    取消对已结束的任务是幂等的：直接返回当前状态，不报错。

    Args:
        task_id: 任务标识。
        _user: 鉴权主体。
        purge: 是否连同产物一起删除。

    Returns:
        取消时返回更新后的任务对象；删除时返回 ``{"ok": True, "task_id": ...}``。

    Raises:
        NotFoundError: 任务不存在。
    """
    manager = get_task_manager()

    if purge:
        removed = manager.delete(task_id, purge_output=True)
        if not removed:
            # 已经被清理过：对删除操作而言这是幂等的成功
            return {"ok": True, "task_id": task_id, "already_removed": True}
        return {"ok": True, "task_id": task_id}

    return manager.cancel(task_id).to_api()


@router.post("/api/tasks/purge-finished")
def purge_finished(
    _user: GuardDep,
    older_than_sec: Annotated[int, Query(ge=0, description="只清理结束时间早于该秒数的任务")] = 0,
) -> dict[str, Any]:
    """清理已结束的任务记录（「清理历史」按钮）。

    该端点不在规格书的 API 清单里，但任务中心的「清理历史文件」需求
    （FR-SYS-04 / UC-07）需要它，否则用户只能逐条删除。

    Args:
        _user: 鉴权主体。
        older_than_sec: 时间阈值。

    Returns:
        ``{"ok": True, "removed": n}``。
    """
    removed = get_task_manager().purge_finished(older_than_sec)
    return {"ok": True, "removed": removed}


__all__ = ["router"]
