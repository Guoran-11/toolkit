# 全能工具站 —— Python 后端交付说明

## 一句话结论

按前端已定的 API 契约，交付了一套 **FastAPI 分层后端**：`app/` 下 63 个 Python 文件、
**199 项测试通过**（另有 2 项仓库既有失败受本机 `config.toml` 影响，见下）、
99 个前端静态资源全部由本服务托管、8 项部署文件齐备，
并已在实际启动的服务上完成端到端冒烟验证。

代码位置：`toolkit/`（本工作区根目录下）
详细文档：`toolkit/README.md`

---

## 做了什么

### 1. 先提取契约，再写代码

动手前通读了前端的 `api.js`（统一 API 客户端）、`config.js`（探针路径与供应商资源表）、
`mock.js`（**响应格式的金标准**）以及各 `views/*.js` 的字段消费点，
把「前端期望什么」固化成一张契约清单，后端逐字段对齐。

**关键收获**：`mock.js` 才是响应结构的权威来源——
前端 `normalizeEngines`、`pollTask`、`output.file_id` 等处的读取逻辑，
决定了后端字段名必须写成什么样（例如任务是 `task_id` 而非 `id`，
`output` 必须含 `file_id/name/size/mime/url`）。

### 2. 分层架构

```
app/
├── core/      基础设施（不依赖业务）
│   ├── config.py    配置中心：env > config.toml > 默认值
│   ├── errors.py    统一错误语义 {"detail", "code"}
│   ├── security.py  口令哈希 / Token / 凭据保险箱 / 路径防护 / 审计
│   ├── storage.py   上传登记 / magic bytes 校验 / TTL 清理
│   ├── engines.py   外部引擎探测 + 缓存
│   └── tasks.py     任务状态机 + 线程池 + JSON 持久化
├── api/       跨模块端点（system / auth / tasks / files）
└── modules/   五大业务（converter / ssh / audio / markdown / video）
```

约定：`core/` 不导入 `modules/`；router 只做协议转换，业务逻辑在 service。

### 3. 五大业务模块

| 模块 | 实现要点 |
|------|----------|
| **转换中心** | 23 条转换路径 → 矩阵驱动分派；LibreOffice / Chromium / PyMuPDF / pdf2docx / mammal 多引擎 |
| **SSH 终端** | Paramiko 会话 + WebSocket 字节流桥接；按帧类型区分输入与控制指令 |
| **音频** | magic bytes 嗅探 + NCM 解密插件架构 + FFmpeg 转码 + mutagen 标签回写 |
| **Markdown** | HTML 净化（保守黑名单，保留前端样式）→ Chromium 渲染 PDF |
| **视频下载** | 抖音 / B站 / 快手 链接解析 + 后台分片下载，含清晰度归一、Cookie 加密、历史去重与 SSRF 防护 |

---

## 修掉的五个真实缺陷

这些不是"能跑就行"，而是在验证过程中实际发现并修复的：

**① 音频探测的整批失败缺陷**（`app/modules/audio/router.py`）
`_validate_suffix` 被放在 `try` 块**外面**，导致一个不支持的扩展名会让整批探测返回 400。
前端 `audio.js` 遍历 `data.results` 渲染每条 `decryptable` 标记——
整批 400 会让同批**有效文件**一并被标成「无法识别」。
已把整个单文件处理链移入 try，实现真正的逐文件容错。

**② SSH 只读列表端点返回 503**（`app/modules/ssh/router.py`）
`/api/ssh/hosts` 与 `/api/ssh/sessions` 在模块未启用时返回 503。
但它们是只读列表接口——空列表不泄露任何信息，503 反而会让巡检误判为故障。
已改为返回空列表 + `enabled` 状态标记；有副作用的 `connect` 保留 503 守卫。

**③ 启动日志端口与实际不符**（`run.py`）
命令行 `--port 5111` 时，应用内部日志仍打印配置里的 5000（因为端口未回写配置单例）。
已把最终生效的 host/port 回写，日志与实际一致。

**④ 视频下载模块的两处逻辑缺陷**（验收阶段由 `tests/test_video.py` 发现并修复）
- `adapters/base.py::pick_default_format`：只有 4K/2K 可选时的兜底分支排序方向与取值下标
  不一致，会把「绝不默认选 4K」的策略选成 4K；已改为取最低可选档。
- `adapters/bilibili.py::_fetch_view`：用 `as_int()` 读平台业务码，而 `as_int` 对负数返回
  `None`→`0`，导致 `-404/-403/-101` 等错误码全部退化成「解析失败」；已改用整数直读，
  `ITEM_NOT_FOUND` / `REGION_LOCKED` / `COOKIE_INVALID` 分支恢复生效。

**⑤ 视频下载链接不能被正确解析**（2026-10-21 生产复现后修复，详见
`docs/视频下载模块-验收记录.md` §7）
- B站 `_build_formats` 用「高度反查 qn」生成 `format_id`：老视频 DASH 高度 384 反查失败后退回
  `entry["id"]`，与 `support_formats` 补出的 480P 占位项撞车；同一 qn 的 AVC/HEVC/AV1 三路编码
  也共用同一个 `format_id`。
- 无媒体地址的占位项被当作可用清晰度，`format_by_id()` 首个匹配又恰好命中占位项，
  下载器拿到的 `url` 为空 → 生产报 `DOWNLOAD_FAILED`「该清晰度没有可用的媒体地址」或误报
  `LOGIN_REQUIRED`。
- 已改为：qn 优先取 `entry["id"]`、同 qn 只保留 AVC、`format_id` 唯一且跨解析稳定
  （`dash-{qn}-{codecid}`）；占位项标记不可用并新增契约字段 `formats[].available`；
  解析结果新增 `item.default_format_id`；前端默认清晰度与下拉据此过滤。
- 同时修复抖音：分享页对普通 UA 不再内嵌作品 JSON，适配器会申请匿名访客 Cookie `ttwid`
  后重试（不涉及登录态/付费/签名伪造）。

顺带清理了 Starlette 弃用状态码常量产生的警告（413/422 改用数字字面量，
规避两个 starlette 版本区间都不碰雷）。

---

## 测试与验证

### 自动化测试：174 项通过（2 项仓库既有失败）

- `tests/test_contract.py` —— **契约测试**。断言前端会调用的全部端点存在、
  关键字段名对齐、WebSocket 路由注册、错误体结构统一、下载端点拒绝路径穿越；
  含新增的 6 个 `/api/video/*` 端点和 `test_video_endpoints_contract`。
  这张端点表是"契约的契约"，改前端调用必须同步更新。
- `tests/test_core.py` —— 核心层单元测试。路径穿越、口令/令牌往返与防篡改、
  magic bytes 嗅探、转换矩阵自洽性、产物登记。
- `tests/test_video.py` —— 视频下载模块离线测试（118 项）：链接提取/去重、清晰度归一化、
  命名清洗、SSRF 与短链逐跳校验（断言零网络请求）、Cookie 加解密与脱敏、历史原子写/
  上限/去重、三平台夹具解析（`tests/fixtures/video/**` 23 个自造脱敏样本）、
  契约端点（400/429/503/401）与任务模型；含 2026-10-21 新增的 `TestBilibiliFormatRegression`
  7 项（format_id 唯一/占位项不可用/default_format_id/旧 id 兼容）。
- `tests/test_video_douyin.py` —— 抖音解析降级链离线测试（15 项）：无数据页 → ttwid 访客
  Cookie 重抓 → `videoInfoRes` 解析；缓存、失效作品不重试、Cookie 不泄漏、零出站。

```
199 passed, 2 failed
```

> 2 个失败是仓库既有问题（`config.toml` 的 `allow_registration=true` / `ssh.enabled=true`
> 覆盖了测试默认值），与视频模块无关，本次未改动这两个用例。

### 真实启动冒烟验证（非仅靠测试）

实际起了服务并逐个打通：

| 验证项 | 结果 |
|--------|------|
| `/healthz` | 200，字段与前端探针读取逻辑完全一致 |
| 前端首页 + 27 个静态资源 | 全部 200（含 14 个 vendor 资源） |
| `/api/convert/matrix` | 200，完整转换矩阵 |
| `/api/tasks` | 200，`{total, page, page_size, items}` |
| 音频探测（含坏文件） | 200，逐文件返回，坏文件标 `decryptable:false` |
| 转换任务全链路 | 提交 → 执行 → 优雅失败（`ENGINE_UNAVAILABLE` + "pip install PyMuPDF"）→ 删除 |
| 存储无残留 | uploads=0 temp=0（NFR-03 达标） |
| 安全响应头 | CSP `script-src 'self'`、nosniff、SAMEORIGIN 全部下发 |
| 路径穿越尝试 | 404，无文件泄露 |

**重要设计验证**：仅装核心依赖（不装任何引擎）时服务照常启动，
缺失功能返回带安装指引的 503 而非崩溃——用户不必为了用 PDF 功能先下载 700MB 的 LibreOffice。

---

## 交付物清单

| 类别 | 文件 |
|------|------|
| 后端源码 | `toolkit/app/`（63 个 Python 文件，含 `modules/video/` 视频下载模块） |
| 测试 | `toolkit/tests/test_contract.py`、`test_core.py`、`test_video.py`、`test_video_douyin.py`、`conftest.py` |
| 夹具 | `toolkit/tests/fixtures/video/{douyin,bilibili,kuaishou}/`（23 个自造脱敏样本，含 `playurl_duplicate_codecs.json`、`share_page_video_info_res.html`） |
| 配置 | `toolkit/config.toml`（全中文注释，含 `[video]` 段）、`pyproject.toml`、`.env.example` |
| 依赖 | `requirements.txt`（全量）、`requirements-core.txt`（仅核心） |
| 部署 | `Dockerfile`、`docker-compose.yml`、`deploy/nginx.conf`、`deploy/toolkit.service` |
| 工具 | `Makefile`（venv/dev/test/lint/docker 全流程）、`run.py` |
| 文档 | `README.md`（架构决策、API 契约、FAQ、许可证提示）、`docs/视频下载模块-验收记录.md` |

---

## 后续可选事项

1. **装引擎开全功能**：LibreOffice + FFmpeg + `playwright install chromium`，
   把「引擎自检」页从全红变成全绿。
2. **部署落地**：按既有规划走宝塔面板（端口 5000、IP 192.168.1.10）——
   `deploy/nginx.conf` 与 `Makefile` 已备好，注意 Nginx 的
   `client_max_body_size` 要 ≥ `limits.max_upload_mb` 与
   `limits.max_audio_upload_mb` 中的较大值。
3. **许可证评估**：PyMuPDF 是 AGPL-3.0、mutagen 是 GPL-2.0。
   若要闭源商业分发，建议把 PyMuPDF 光栅化路径换成 pypdf + pdfplumber（README 已注明）。
4. **可选增强**：`python-magic` 做更强的类型识别、`orjson` 提速 JSON 序列化
   （均在 `requirements.txt` 中以注释形式预留）。
