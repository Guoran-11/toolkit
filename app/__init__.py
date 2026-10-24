"""全能工具站（ToolKit）后端应用包。

一个进程、一个端口，提供四大能力：
    1. converter  —— HTML / Office / PDF 互相转换
    2. ssh        —— 浏览器内 SSH 终端
    3. audio      —— 音频格式转换（含平台专有格式解封装）
    4. markdown   —— Markdown 渲染与导出

对外契约由 ``app/static/js/api.js`` 定义，本包的所有路由与响应结构均以其为准。
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
