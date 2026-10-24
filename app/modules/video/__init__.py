"""视频下载模块（``/api/video/*``）。

对外只暴露 :data:`router`，其余子模块（适配器、下载器、历史、凭据等）供模块内部与
测试直接导入。模块按契约分层：

    · ``router``    —— 只做协议转换（Pydantic 校验 + 调 service）
    · ``service``   —— 编排解析 / 下载任务 / 历史 / 设置 / 限流 / 熔断 / 审计
    · ``adapters``  —— 「链接 → 元数据 / 媒体地址」，不碰任务、不落盘
    · ``downloader``—— 分片下载、续传、FFmpeg 合并、图文打包
"""

from __future__ import annotations

from app.modules.video.router import router

__all__ = ["router"]
