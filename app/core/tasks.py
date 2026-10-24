"""任务模型、状态机与执行调度。

为什么用 ThreadPoolExecutor 而不是规格书建议的 ProcessPoolExecutor：
本项目的重活全部发生在**外部子进程**里（LibreOffice / Chromium / FFmpeg），
Python 侧只是在等待 IO 与搬运文件，不存在 GIL 争抢的 CPU 密集段。
ProcessPool 反而会带来三个真实代价：
    1. 任务函数与其闭包必须可 pickle（引擎句柄、文件流都不可序列化）；
    2. Windows 使用 spawn 启动方式，每个 worker 都要重新导入整个应用（含引擎探测）；
    3. 跨进程回传进度需要额外 IPC。
因此这里用线程池 + 子进程超时控制，既满足并发要求，也避免上述复杂度。
若未来引入纯 Python 的 CPU 密集任务（如大规模 OCR），再为那类任务单独引入进程池。

并发换算：规格书 NFR-DC-02 要求 worker 数 ≤ CPU 核心数的一半（避免 LO 抢占资源），
因此默认值取 ``max(1, cpu_count // 2)``，并通过 config.toml 覆盖。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from app.core.config import STORAGE_DIR, get_settings
from app.core.errors import NotFoundError, QueueFullError, TaskTimeoutError
from app.core.storage import OutputFile, registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 类型

TaskStatus = Literal["pending", "running", "success", "failed", "cancelled"]
# ``video`` 为视频下载模块（任务输入不是上传文件，而是远程作品链接，见 TaskInput.source）
TaskModule = Literal["converter", "audio", "markdown", "system", "video"]

TERMINAL_STATUSES: frozenset[str] = frozenset({"success", "failed", "cancelled"})

_TASKS_FILE: Path = STORAGE_DIR / "tasks.json"


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 字符串。

    与前端 mock.js 的 ``new Date().toISOString()`` 保持同一格式（含毫秒与 Z 后缀），
    前端会用 ``new Date(task.created_at)`` 解析，格式不一致会导致耗时显示为 NaN。

    Returns:
        形如 ``2026-09-16T13:10:13.123Z`` 的字符串。
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def epoch_to_iso(epoch: float | None) -> str | None:
    """把 Unix 时间戳转为与 :func:`utc_now_iso` 同格式的 ISO 8601 字符串。

    任务对象的 ``expire_at`` 有两个来源：任务引擎（``_finalize``）与同步完成
    路径（``register_completed``）。两者必须走同一个换算函数，否则同一页面里
    两种任务的过期时间字符串格式会漂移，前端 ``formatTime`` 的解析结果不一致。

    Args:
        epoch: Unix 时间戳（秒）。

    Returns:
        形如 ``2026-09-17T13:10:13.123Z`` 的字符串；
        0 或 None 返回 None（语义为「产物不过期」，对应 ``output_ttl_hours = 0``）。
    """
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- 数据结构


@dataclass(slots=True)
class TaskInput:
    """任务输入文件的元信息（不含内容）。

    Attributes:
        name: 原始文件名（视频下载任务里是作品标题）。
        size: 字节数（视频下载任务在提交时尚不知道体积，填 0）。
        sha256: SHA256 前 8 位。
        source: 远程来源链接（仅视频下载模块使用）。

    ``source`` 是**追加字段**：旧任务（``tasks.json`` 里没有该键）反序列化后为
    ``None``，``to_api()`` 不输出它，因此老前端的 ``inputs[]`` 结构完全不变；
    新前端按 ``?? ''`` 容错。
    """

    name: str
    size: int
    sha256: str = ""
    source: str | None = None

    def to_api(self) -> dict[str, Any]:
        """转换为前端期望的结构。

        Returns:
            含 ``name`` / ``size`` / ``sha256`` 的字典；``source`` 非空时一并输出。
        """
        data: dict[str, Any] = {"name": self.name, "size": self.size, "sha256": self.sha256}
        if self.source:
            data["source"] = self.source
        return data


@dataclass(slots=True)
class JobResult:
    """任务产出的描述。

    Attributes:
        path: 引擎产出的文件路径。
        name: 对用户可见的文件名。
        mime: MIME 类型。
    """

    path: Path
    name: str
    mime: str


class JobContext:
    """传给任务函数的执行上下文（进度上报 + 取消协作）。"""

    def __init__(self, task_id: str, manager: TaskManager) -> None:
        """初始化。

        Args:
            task_id: 任务标识。
            manager: 所属管理器（用于回写进度与查询取消标记）。
        """
        self.task_id = task_id
        self._manager = manager
        self._last_progress = 0

    def progress(self, value: int) -> None:
        """上报进度（单调递增，自动夹取到 0–100）。

        Args:
            value: 进度百分比。
        """
        clamped = max(0, min(100, int(value)))
        # 进度只增不减：避免多阶段子任务回退导致进度条抖动
        if clamped <= self._last_progress:
            return
        self._last_progress = clamped
        self._manager._set_progress(self.task_id, clamped)

    def advance(self, delta: int) -> None:
        """在当前进度上前进若干百分点。

        Args:
            delta: 增量。
        """
        self.progress(self._last_progress + delta)

    @property
    def cancelled(self) -> bool:
        """外部是否已请求取消。"""
        return self._manager.is_cancel_requested(self.task_id)

    def raise_if_cancelled(self) -> None:
        """若已请求取消则抛出中断异常。

        Raises:
            TaskCancelled: 任务被取消。
        """
        if self.cancelled:
            raise TaskCancelled()


class TaskCancelled(Exception):  # noqa: N818 - 「取消」是控制流信号而非错误，加 Error 后缀会误导调用方
    """任务被用户取消（内部信号，不对外暴露）。"""


class JobFunc(Protocol):
    """任务函数签名。"""

    def __call__(self, ctx: JobContext) -> JobResult:
        """执行任务。

        Args:
            ctx: 执行上下文。

        Returns:
            产出描述。

        Raises:
            Exception: 任意失败原因（会被转换为用户可读的 error 文案）。
        """
        ...


# ---------------------------------------------------------------- 任务记录


@dataclass
class TaskRecord:
    """任务完整记录（可 JSON 序列化）。

    Attributes:
        task_id: UUID4 十六进制。
        module: 所属模块。
        operation: 操作标识（如 ``pdf2docx``、``ncm2mp3``）。
        status: 状态机当前状态。
        progress: 0–100。
        inputs: 输入文件元信息。
        output: 产出元信息（成功时有值）。
        error: 失败原因（用户可读，非堆栈）。
        created_at/started_at/finished_at: 时间戳。
        expire_at: 产物过期时间。
        error_code: 机器可读错误码（前端可分支）。
    """

    task_id: str
    module: str
    operation: str
    status: str = "pending"
    progress: int = 0
    inputs: list[dict[str, Any]] = field(default_factory=list)
    output: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    expire_at: str | None = None

    def to_api(self) -> dict[str, Any]:
        """转换为前端任务模型结构。

        Returns:
            与 mock.js 的 createTask 输出逐字段对齐的字典。
        """
        return {
            "task_id": self.task_id,
            "module": self.module,
            "operation": self.operation,
            "status": self.status,
            "progress": self.progress,
            "inputs": list(self.inputs),
            "output": self.output,
            "error": self.error,
            "error_code": self.error_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "expire_at": self.expire_at,
        }


# ---------------------------------------------------------------- 管理器


class TaskManager:
    """任务调度与状态管理（线程安全 + JSON 持久化）。"""

    def __init__(self, workers: int | None = None) -> None:
        """初始化调度器。

        Args:
            workers: 并发 worker 数；None 时按 CPU 核心数的一半自动取值。
        """
        settings = get_settings()
        if workers is None:
            cpu = os.cpu_count() or 2
            # NFR-DC-02：不超过核心数一半，且至少 1 个
            workers = max(1, cpu // 2)
        self.workers = min(workers, settings.limits.workers) if workers else settings.limits.workers
        self.workers = max(1, self.workers)

        self._lock = threading.RLock()
        self._tasks: dict[str, TaskRecord] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._cancel_requested: set[str] = set()
        self._executor = ThreadPoolExecutor(
            max_workers=self.workers,
            thread_name_prefix="toolkit-worker",
        )
        self._load()

    # -------------------------------------------------- 持久化

    def _load(self) -> None:
        """载入历史任务；把中断的未完成任务标记为失败。"""
        if not _TASKS_FILE.exists():
            return
        try:
            raw = json.loads(_TASKS_FILE.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("任务表损坏，已从空状态启动")
            return

        for item in raw.get("tasks", []):
            try:
                record = TaskRecord(**item)
            except TypeError:
                continue
            # 进程非正常退出时残留的 pending/running 任务不可能再推进，
            # 保留原状态会让前端永远轮询不到终态，必须显式收尾。
            if record.status in {"pending", "running"}:
                record.status = "failed"
                record.error = "服务重启导致任务中断，请重新提交"
                record.error_code = "INTERRUPTED"
                record.finished_at = record.finished_at or utc_now_iso()
            self._tasks[record.task_id] = record

    def _flush(self) -> None:
        """把任务表写回磁盘（原子替换）。"""
        try:
            _TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _TASKS_FILE.with_suffix(".tmp")
            payload = {"tasks": [asdict(t) for t in self._tasks.values()]}
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, _TASKS_FILE)
        except OSError as exc:
            logger.warning("任务表写入失败：%s", exc)

    # -------------------------------------------------- 内部状态更新

    def _set_progress(self, task_id: str, value: int) -> None:
        """回写进度（不加锁刷盘，避免高频写盘）。"""
        with self._lock:
            record = self._tasks.get(task_id)
            if record and record.status == "running":
                record.progress = value

    def is_cancel_requested(self, task_id: str) -> bool:
        """查询取消标记。

        Args:
            task_id: 任务标识。

        Returns:
            是否已请求取消。
        """
        with self._lock:
            return task_id in self._cancel_requested

    # -------------------------------------------------- 提交与执行

    def stats(self) -> dict[str, int]:
        """统计各状态任务数（供排队位置计算与监控）。

        Returns:
            ``{"pending": n, "running": n, ...}``。
        """
        with self._lock:
            result = {"pending": 0, "running": 0, "success": 0, "failed": 0, "cancelled": 0}
            for record in self._tasks.values():
                result[record.status] = result.get(record.status, 0) + 1
            return result

    def submit(
        self,
        *,
        module: TaskModule,
        operation: str,
        inputs: list[TaskInput],
        job: JobFunc,
        ttl_hours: int | None = None,
        timeout_sec: int | None = None,
    ) -> TaskRecord:
        """提交一个任务并立即返回（异步执行）。

        Args:
            module: 模块标识。
            operation: 操作标识。
            inputs: 输入文件元信息。
            job: 任务函数。
            ttl_hours: 产物保留时长；None 时取配置默认值。
            timeout_sec: 本任务超时秒数；None 时取 ``limits.task_timeout_sec``。
                视频下载任务单文件可能上百 MB，需要比通用转换更长的超时
                （``[video] task_timeout_sec_video``，默认 600 秒）。

        Returns:
            刚创建的 TaskRecord（状态为 pending）。

        Raises:
            QueueFullError: 待执行任务数超过 ``limits.max_queue``，附带排队位置。
        """
        settings = get_settings()
        pending = self.stats()["pending"]
        if pending >= settings.limits.max_queue:
            raise QueueFullError(
                "任务队列已满，请稍后重试",
                extra={"queue_position": pending, "queue_limit": settings.limits.max_queue},
            )

        record = TaskRecord(
            task_id=uuid.uuid4().hex,
            module=module,
            operation=operation,
            inputs=[item.to_api() for item in inputs],
        )
        with self._lock:
            self._tasks[record.task_id] = record
        self._flush()

        future = self._executor.submit(self._run, record.task_id, job, ttl_hours, timeout_sec)
        with self._lock:
            self._futures[record.task_id] = future
        return record

    def register_completed(
        self,
        *,
        module: TaskModule,
        operation: str,
        inputs: list[TaskInput],
        output: OutputFile,
    ) -> TaskRecord:
        """登记一个在请求内同步跑完的任务。

        与 :meth:`submit` 的分工：``submit`` 把任务丢进线程池异步执行；本方法用于
        那些「无需排队、请求内即可完成」的操作（典型是 Markdown 导出 HTML ——
        纯文本落盘，没有任何外部引擎依赖，让它排队只增加无谓延迟）。

        这类任务**同样必须进入任务表**，否则会产生两个用户可见的缺陷：

            1. 前端刷新后从 ``/api/tasks`` 重新拉取列表，该任务凭空消失，
               用户再也找不到刚导出的产物入口；
            2. 该 ``task_id`` 无法被 ``/api/convert/batch-zip`` 解析
               （它通过 ``manager.get()`` 查任务），选中后打包必然失败。

        Args:
            module: 模块标识。
            operation: 操作标识。
            inputs: 输入文件元信息。
            output: 已登记进产物表（``registry``）的输出文件。

        Returns:
            终态（``status='success'``、``progress=100``）的 TaskRecord。
        """
        now = utc_now_iso()
        record = TaskRecord(
            task_id=uuid.uuid4().hex,
            module=module,
            operation=operation,
            status="success",
            progress=100,
            inputs=[item.to_api() for item in inputs],
            output=output.to_api(),
            created_at=now,
            started_at=now,
            finished_at=now,
            expire_at=epoch_to_iso(output.expire_at),
        )
        with self._lock:
            self._tasks[record.task_id] = record
        self._flush()
        return record

    def _run(
        self,
        task_id: str,
        job: JobFunc,
        ttl_hours: int | None,
        timeout_sec: int | None = None,
    ) -> None:
        """在 worker 线程中执行任务并维护状态机。

        Args:
            task_id: 任务标识。
            job: 任务函数。
            ttl_hours: 产物保留时长。
            timeout_sec: 本任务超时秒数；None 时取全局 ``limits.task_timeout_sec``。
        """
        settings = get_settings()
        effective_timeout = settings.limits.task_timeout_sec if timeout_sec is None else int(timeout_sec)
        ctx = JobContext(task_id, self)

        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            if task_id in self._cancel_requested:
                record.status = "cancelled"
                record.finished_at = utc_now_iso()
                self._flush()
                return
            record.status = "running"
            record.started_at = utc_now_iso()
            record.progress = 1

        result: JobResult | None = None
        try:
            result = self._execute_with_timeout(job, ctx, effective_timeout)
        except TaskCancelled:
            self._finalize(task_id, status="cancelled", error="任务已取消")
            return
        except TaskTimeoutError as exc:
            self._finalize(task_id, status="failed", error=str(exc), code=exc.code)
            return
        except BaseException as exc:  # noqa: BLE001 - 任何异常都要落成用户可读文案
            message, code = describe_exception(exc)
            logger.warning("任务 %s 失败：%s", task_id, message)
            self._finalize(task_id, status="failed", error=message, code=code)
            return

        # 产出登记 + 状态收尾
        try:
            if ctx.cancelled:
                # 取消发生在产出之后：丢弃产物，避免用户拿到"已取消任务"的文件
                result.path.unlink(missing_ok=True)
                self._finalize(task_id, status="cancelled", error="任务已取消")
                return

            effective_ttl = settings.limits.output_ttl_hours if ttl_hours is None else ttl_hours
            output = registry.register(
                result.path, name=result.name, mime=result.mime, ttl_hours=effective_ttl
            )
            self._finalize(task_id, status="success", output=output)
        except BaseException as exc:  # noqa: BLE001
            message, code = describe_exception(exc)
            self._finalize(task_id, status="failed", error=message, code=code)

    def _execute_with_timeout(self, job: JobFunc, ctx: JobContext, timeout_sec: int) -> JobResult:
        """在守护线程中执行任务并施加超时。

        为什么不直接 ``future.result(timeout=...)`` 就抛错：那只是"不再等待"，
        任务线程仍在后台跑，会持续占用 worker 槽位。这里用协作式取消：
        超时后置取消标记，任务函数在关键检查点自行退出。

        Args:
            job: 任务函数。
            ctx: 执行上下文。
            timeout_sec: 超时秒数。

        Returns:
            JobResult。

        Raises:
            TaskTimeoutError: 超时。
        """
        box: dict[str, Any] = {}
        done = threading.Event()

        def _target() -> None:
            try:
                box["result"] = job(ctx)
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(target=_target, name=f"job-{ctx.task_id}", daemon=True)
        thread.start()

        if not done.wait(timeout=timeout_sec):
            with self._lock:
                self._cancel_requested.add(ctx.task_id)
            # 再给 5 秒让任务在检查点优雅退出，避免产物写一半
            done.wait(timeout=5)
            raise TaskTimeoutError(
                f"任务执行超时（超过 {timeout_sec} 秒），已中断。"
                "请尝试减小文件体积、降低输出 DPI，或提高 config.toml 中的 task_timeout_sec。"
            )

        if "error" in box:
            raise box["error"]
        result = box.get("result")
        if not isinstance(result, JobResult):
            raise RuntimeError("任务未返回有效的产出")
        return result

    def _finalize(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        output: OutputFile | None = None,
        error: str | None = None,
        code: str | None = None,
    ) -> None:
        """统一收尾：写终态、算过期时间、持久化、释放 future。

        Args:
            task_id: 任务标识。
            status: 终态。
            output: 产物（成功时）。
            error: 错误文案。
            code: 错误码。
        """
        settings = get_settings()
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            record.status = status
            record.finished_at = utc_now_iso()
            if status == "success":
                record.progress = 100
                record.output = output.to_api() if output else None
                # 统一走 epoch_to_iso，保证与同步完成路径的格式完全一致
                record.expire_at = epoch_to_iso(output.expire_at) if output else None
            else:
                record.error = error
                record.error_code = code
                # 失败任务不产出文件，保留 24h 供用户查看原因后清理
                record.expire_at = None
            self._futures.pop(task_id, None)
            self._cancel_requested.discard(task_id)
            del settings  # 仅用于类型提示上的一致性，无副作用
        self._flush()

    # -------------------------------------------------- 查询

    def get(self, task_id: str) -> TaskRecord:
        """取任务详情。

        Args:
            task_id: 任务标识。

        Returns:
            TaskRecord。

        Raises:
            NotFoundError: 任务不存在或已被清理。
        """
        with self._lock:
            record = self._tasks.get(task_id)
        if record is None:
            raise NotFoundError("任务不存在或已被清理")
        return record

    def list(
        self,
        *,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
        module: str | None = None,
    ) -> dict[str, Any]:
        """分页列出任务（按创建时间倒序）。

        Args:
            status: 状态过滤；``all`` 或空表示不过滤。
            page: 页码，从 1 开始。
            page_size: 每页条数。
            module: 模块过滤。

        Returns:
            形如 ``{"total": n, "page": p, "page_size": s, "items": [...]}`` 的字典。
            字段名与 mock.listTasks 一致（前端直接读 ``items``）。
        """
        with self._lock:
            items = list(self._tasks.values())

        if status and status != "all":
            items = [t for t in items if t.status == status]
        if module and module != "all":
            items = [t for t in items if t.module == module]

        items.sort(key=lambda t: t.created_at, reverse=True)
        page = max(1, page)
        page_size = max(1, min(500, page_size))
        start = (page - 1) * page_size
        window = items[start : start + page_size]

        return {
            "total": len(items),
            "page": page,
            "page_size": page_size,
            "items": [t.to_api() for t in window],
        }

    # -------------------------------------------------- 取消与删除

    def cancel(self, task_id: str) -> TaskRecord:
        """取消任务。

        pending 任务直接置为 cancelled；running 任务置取消标记，
        由任务函数在检查点协作退出（正在等待的外部子进程无法强杀，
        此时任务会在产出后丢弃结果）。

        Args:
            task_id: 任务标识。

        Returns:
            更新后的 TaskRecord。

        Raises:
            NotFoundError: 任务不存在。
        """
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise NotFoundError("任务不存在")
            if record.status in TERMINAL_STATUSES:
                return record
            self._cancel_requested.add(task_id)
            if record.status == "pending":
                record.status = "cancelled"
                record.finished_at = utc_now_iso()
                record.error = "任务已取消"
                record.error_code = "CANCELLED"
        self._flush()
        return record

    def delete(self, task_id: str, *, purge_output: bool = True) -> bool:
        """删除任务记录（可选连同产物）。

        Args:
            task_id: 任务标识。
            purge_output: 是否同时删除产物文件。

        Returns:
            是否确实删除了记录。
        """
        with self._lock:
            record = self._tasks.pop(task_id, None)
            self._cancel_requested.add(task_id)
            self._futures.pop(task_id, None)
        if record is None:
            return False

        if purge_output and record.output:
            file_id = record.output.get("file_id")
            if isinstance(file_id, str):
                registry.remove(file_id)
        self._flush()
        return True

    # -------------------------------------------------- 维护

    def purge_finished(self, older_than_sec: int = 0) -> int:
        """清理已结束的任务记录。

        Args:
            older_than_sec: 只清理结束时间早于该秒数的任务；0 表示全部已结束任务。

        Returns:
            清理条数。
        """
        cutoff = time.time() - older_than_sec
        removed = 0
        with self._lock:
            victims: list[str] = []
            for task_id, record in self._tasks.items():
                if record.status not in TERMINAL_STATUSES:
                    continue
                if older_than_sec > 0 and record.finished_at:
                    try:
                        finished = datetime.fromisoformat(record.finished_at.replace("Z", "+00:00"))
                        if finished.timestamp() > cutoff:
                            continue
                    except ValueError:
                        pass
                victims.append(task_id)
            for task_id in victims:
                self._tasks.pop(task_id, None)
                removed += 1
        if removed:
            self._flush()
        return removed

    def shutdown(self) -> None:
        """优雅关闭：等待在途任务结束（带超时），随后停止线程池。"""
        with self._lock:
            self._cancel_requested.update(
                tid for tid, rec in self._tasks.items() if rec.status in {"pending", "running"}
            )
        self._executor.shutdown(wait=True, cancel_futures=True)


def describe_exception(exc: BaseException) -> tuple[str, str]:
    """把任意异常转换为「用户可读文案 + 错误码」。

    这是 §7.1 对 ``error`` 字段"用户可读，非堆栈"要求的落点：
    绝不允许把 Python traceback 直接抛给用户。

    Args:
        exc: 捕获到的异常。

    Returns:
        ``(用户可读文案, 错误码)``。
    """
    from app.core.errors import ToolkitError

    if isinstance(exc, ToolkitError):
        return exc.detail, exc.code

    if isinstance(exc, MemoryError):
        return "内存不足，文件过大。请尝试缩小文件或降低输出质量。", "OUT_OF_MEMORY"
    if isinstance(exc, PermissionError):
        return "文件写入被拒绝，请检查 storage 目录权限。", "PERMISSION_DENIED"
    if isinstance(exc, FileNotFoundError):
        return "转换引擎或依赖文件缺失，请在「引擎自检」中查看安装指引。", "ENGINE_MISSING"
    if isinstance(exc, OSError):
        return f"文件或系统操作失败：{exc.strerror or exc}", "OS_ERROR"

    text = str(exc).strip() or exc.__class__.__name__
    return f"转换失败：{text[:300]}", "FAILED"


# ---------------------------------------------------------------- 单例

_manager_lock = threading.Lock()
_manager: TaskManager | None = None


def get_task_manager() -> TaskManager:
    """获取全局任务管理器（惰性单例）。

    Returns:
        TaskManager 实例。
    """
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = TaskManager()
        return _manager


def reset_task_manager() -> None:
    """重置单例（测试用）。"""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown()
        _manager = None


__all__ = [
    "JobContext",
    "JobResult",
    "TaskCancelled",
    "TaskInput",
    "TaskManager",
    "TaskRecord",
    "get_task_manager",
    "reset_task_manager",
    "utc_now_iso",
]
