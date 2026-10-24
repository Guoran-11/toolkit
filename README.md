# 全能工具站（ToolKit）

> 自托管的本地文件处理工具站：**文档转换 / SSH 终端 / 音频转换 / Markdown 预览 / 地图定位 / 视频下载**。
> 文件全程在本机处理，不上传任何第三方服务。

后端基于 **FastAPI + Pydantic v2**，前端是一套纯静态 SPA（由本服务直接托管），
两者通过一套明确定义的 HTTP + WebSocket 契约对接。

---

## 本地免注册版（本仓库默认形态）

本仓库默认就是**本机单人自用**形态：`python run.py` 启动后浏览器直接进入控制面板，
**没有登录页、不需要注册、不依赖邮件服务**。

| 配置项 | 默认值 | 作用 |
|--------|--------|------|
| `server.auth_required` | `false` | 关闭登录门禁，打开即用 |
| `auth.allow_registration` | `false` | 不开放注册入口 |
| `auth.require_email_verification` | `false` | 注册流程不要求邮件验证 |
| `email.enabled` | `false` | 不外发邮件（无 SMTP 依赖） |
| `server.host` / `port` | `127.0.0.1` / `5000` | 只监听本机，端口占用自动顺延 |

仓库里**不含** `config.toml`（已被 `.gitignore` 排除），所有凭据字段在
[`config.example.toml`](config.example.toml) 中均为空。想要自定义就复制一份：

```bash
cp config.example.toml config.toml     # 不复制也行，缺失时全部走内置默认值
```

而内置默认值恰好也是「免登录 + 不开放注册」，所以**直接 clone 下来就能用**。

> ⚠️ **一旦要给局域网/公网用，必须把 `auth_required` 改回 `true`。**
> `python run.py --lan` 会自动强制开启鉴权（这是硬性安全约束，不接受配置失误）。

---

## 目录

- [快速开始](#快速开始)
- [它能做什么](#它能做什么)
- [项目结构](#项目结构)
- [配置](#配置)
- [引擎依赖](#引擎依赖)
- [视频下载](#视频下载)
- [部署](#部署)
- [开发](#开发)
- [API 契约](#api-契约)
- [架构与设计决策](#架构与设计决策)
- [安全说明](#安全说明)
- [常见问题](#常见问题)

---

## 快速开始

### Windows：双击 `start.bat`

脚本会按优先级自动寻找可用的解释器：

```
项目内 .venv  →  官方 py launcher  →  PATH 中的 python/python3  →  常见安装目录
```

关键点：**每个候选都会被实际执行一次来验证**（`python -c "pass"`），而不是只看文件是否存在。

> ⚠️ **Microsoft Store 陷阱**
>
> Windows 会在 `%LOCALAPPDATA%\Microsoft\WindowsApps\` 放置一个 `python.exe` 占位程序
> （实际指向 `AppInstallerPythonRedirector.exe`）。它**确实存在于 PATH 中**，
> 因此 `where python` / `Get-Command python` 都会报告"已安装"，
> 但在批处理这类非交互场景下，它**直接返回 9009**（命令未找到）。
>
> 这是 `process exited with code 9009` 最常见的成因 —— 不是脚本写错了，
> 而是把占位程序当成了真 Python。**判定可用性必须实跑，不能只看存在性。**

若本机确实没有可用的 Python，脚本会打印安装指引（含官方下载地址与
"务必勾选 Add python.exe to PATH"的提醒），而不是甩一个难懂的错误码。

### 方式一：命令行运行

```bash
# 1. 安装核心依赖（服务可启动，缺失的引擎会降级为"功能不可用"而非崩溃）
pip install -r requirements-core.txt

# 2. 启动
python run.py
```

浏览器会自动打开 `http://127.0.0.1:5000`。若 5000 端口被占用，会自动顺延并提示实际端口。

> 想体验全部功能（Office 互转、音频转码、HTML→PDF），见 [引擎依赖](#引擎依赖)。

### 方式二：Docker

```bash
cp .env.example .env          # 按需修改（对外暴露务必保持 AUTH_REQUIRED=true）
docker compose up -d --build
docker compose logs -f toolkit | grep 密码     # 首次启动会打印随机管理员密码
```

### 方式三：局域网共享

```bash
python run.py --lan
```

`--lan` 会绑定 `0.0.0.0` 并**强制开启登录鉴权**（这是硬性安全约束，不接受配置失误）。

---

## 它能做什么

| 模块 | 能力 |
|------|------|
| **转换中心** | Office ⇄ PDF、Office 互转、HTML→PDF/PNG、PDF 合并/拆分/旋转/加密、PDF→图片/PPTX/DOCX/HTML/XLSX |
| **SSH 终端** | 基于 xterm.js 的 Web 终端，支持密码与私钥认证、窗口尺寸同步、会话管理 |
| **音频转换** | 格式嗅探、平台专有加密格式（NCM 等）解封装、FFmpeg 转码、标签与封面保留 |
| **Markdown 预览** | 实时预览 + 导出为 PDF / HTML（复用 Chromium 渲染通道） |
| **地图定位** | 输入/粘贴经纬度即在地图上打点；支持十进制度、度分秒、中文方位写法；给出 UTM / GCJ-02 / Maidenhead 换算与离线逆地理；离线底图（Natural Earth）+ 可选在线瓦片 |
| **视频下载** | 抖音 / B站 / 快手 分享链接解析与后台下载：批量粘贴、清晰度选择、仅音频、图文作品打包、登录态 Cookie、历史去重；**不碰 DRM/付费内容，公网部署默认关闭** |
| **任务中心** | 所有耗时操作异步执行，实时进度、取消、产物过期自动清理 |

---

## 项目结构

```
toolkit/
├── app/
│   ├── main.py                 # 应用装配：中间件 → 异常处理 → 路由 → 静态挂载
│   ├── core/                   # 基础设施（与业务无关）
│   │   ├── config.py           # 配置中心（env > config.toml > 默认值）
│   │   ├── errors.py           # 统一错误语义（detail + code）
│   │   ├── security.py         # 口令哈希、Token、凭据保险箱、路径防护、审计
│   │   ├── storage.py          # 上传/产物登记、magic bytes 校验、TTL 清理
│   │   ├── engines.py          # 外部引擎探测与缓存
│   │   └── tasks.py            # 任务状态机 + 线程池调度 + JSON 持久化
│   ├── api/                    # 跨模块端点
│   │   ├── deps.py             # 依赖注入：配置、鉴权守卫、客户端 IP
│   │   ├── system.py           # /healthz、引擎自检、存储概览
│   │   ├── auth.py             # 登录、登出、改密（含失败限流）
│   │   ├── tasks.py            # 任务列表/详情/取消/删除
│   │   └── files.py            # 产物下载（含 HTML 防 XSS 处理）
│   ├── modules/                # 业务模块（router + service 分层）
│   │   ├── converter/          # 转换矩阵、LibreOffice、Chromium、PDF、Office
│   │   ├── ssh/                # Paramiko 会话管理 + WebSocket 字节流桥接
│   │   ├── audio/              # 嗅探、解封装插件、FFmpeg 转码
│   │   ├── markdown/           # HTML 净化 + PDF 导出
│   │   └── video/              # 视频下载：平台适配器、分片下载、历史、Cookie、限流
│   └── static/                 # 前端 SPA（原样托管）
│       ├── js/geo/             # 地图定位：坐标换算（coords）、TopoJSON 解码、Canvas 渲染引擎
│       └── vendor/geo/         # 离线底图数据（Natural Earth 110m TopoJSON，107 KB）
├── tests/                      # 契约测试 + 核心层单元测试 + 视频模块离线测试 + 前端自检（CSS / 地理计算 / 视图集成）
├── deploy/                     # nginx.conf / systemd unit
├── storage/                    # 运行时数据（上传/临时/产物/日志）—— 不入库
├── runtime/                    # 离线引擎目录（LibreOffice/FFmpeg/Chromium）
├── config.toml                 # 配置
├── requirements.txt            # 全量依赖
├── requirements-core.txt       # 仅核心依赖
├── Dockerfile / docker-compose.yml / Makefile
└── run.py                      # 启动入口（端口探测 + 浏览器唤起）
```

分层约定：**router 只做参数校验与协议转换，业务逻辑放 service，跨模块能力放 core**。
`core/` 不导入 `modules/`，保证基础设施可独立测试。

---

## 配置

优先级：**环境变量 > `config.toml` > 内置默认值**。
环境变量用于 Docker/CI，配置文件用于离线单机版的可视化编辑。

完整注释见 [`config.toml`](config.toml)。常用项：

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `server.host` / `server.port` | `127.0.0.1` / `5000` | 监听地址 |
| `server.auth_required` | `true` | 是否要求登录（**true 时未登录只能看到登录页**） |
| `limits.max_upload_mb` | `100` | 单文件上传上限（文档模块） |
| `limits.max_audio_upload_mb` | `200` | 音频模块单文件上传上限（独立放宽） |
| `limits.workers` | `4` | 任务并发线程数 |
| `limits.output_ttl_hours` | `24` | 产物保留时长（0 = 不清理） |
| `engines.libreoffice` | `runtime/libreoffice/program/soffice` | LibreOffice 路径 |
| `engines.ffmpeg` | `runtime/ffmpeg` | FFmpeg 路径 |
| `engines.chromium` | `runtime/chromium/chrome` | Chromium 路径 |
| `ssh.enabled` | `false` | SSH 模块开关 |
| `security.allowed_hosts` | `[]` | SSH 目标白名单（空 = 不限） |
| `video.enabled` | `true` | 视频下载模块开关（**公网部署请置 `false`**） |
| `video.prefer_quality` / `video.prefer_format` | `1080P` / `mp4` | 默认清晰度（4K/1080P/720P/480P）与输出容器（mp4/mp3/m4a） |
| `video.max_concurrent` | `2` | 同时下载任务数（1–4，过高易触发平台风控） |
| `video.max_file_mb` | `2048` | 单文件大小上限（超出任务失败 `OUTPUT_TOO_LARGE`） |
| `video.max_urls` | `20` | 单次最多解析的链接数 |

主要环境变量：`TOOLKIT_HOST` `TOOLKIT_PORT` `TOOLKIT_AUTH_REQUIRED` `TOOLKIT_PASSWORD`
`TOOLKIT_MAX_UPLOAD_MB` `TOOLKIT_MAX_AUDIO_UPLOAD_MB` `TOOLKIT_WORKERS` `TOOLKIT_OUTPUT_TTL_HOURS` `TOOLKIT_SSH_ENABLED`
`TOOLKIT_STORAGE_DIR` `TOOLKIT_RUNTIME_DIR` `TOOLKIT_LIBREOFFICE` `TOOLKIT_FFMPEG` `TOOLKIT_CHROMIUM`
`TOOLKIT_VIDEO_ENABLED` `TOOLKIT_VIDEO_MAX_CONCURRENT` `TOOLKIT_VIDEO_PREFER_QUALITY` `TOOLKIT_VIDEO_MAX_FILE_MB`
`TOOLKIT_VIDEO_TASK_TIMEOUT` `TOOLKIT_VIDEO_MAX_URLS`

---

## 引擎依赖

**核心依赖之外，下面几个引擎决定了大部分转换能力**（LibreOffice / FFmpeg / Chromium，
另有视频下载可选的 yt-dlp）。缺失时相关功能返回带安装指引的 `503`，服务本身照常运行 ——
这个设计让用户不必为了用 PDF 功能而先下载 700MB 的 LibreOffice。

### LibreOffice（Office ⇄ PDF、Office 互转）

```bash
# Linux
sudo apt install libreoffice-writer libreoffice-calc libreoffice-impress
# macOS
brew install --cask libreoffice
# Windows：安装后把路径写入 config.toml
#   libreoffice = "C:/Program Files/LibreOffice/program/soffice.exe"
```

### FFmpeg（音频转码、视频 DASH 合并 / 转 mp3）

```bash
# Linux
sudo apt install ffmpeg
# macOS
brew install ffmpeg
# Windows：把静态构建解压到 runtime/ffmpeg/（含 ffmpeg.exe）
```

视频下载模块**只有 DASH 分离音视频**（B站典型）和「仅音频 mp3」才需要 FFmpeg 合并/转码；
单流 mp4 与图文 zip 不依赖它。缺失时任务返回 `ENGINE_UNAVAILABLE` + 安装指引，服务不会崩。

### yt-dlp（视频下载的可选兜底，不装也能用）

```bash
pip install yt-dlp        # 可选：[video] 里无开关，按「装了就用、没装就跳过」探测
```

三个平台的解析以**平台公开接口/分享页 JSON** 为主路径，yt-dlp 只是适配器全部失效后的最后一级兜底；
未安装时 `/api/system/engines` 会显示 `state=fail` 与安装指引，但**不影响**模块正常使用。

### Chromium（HTML→PDF、Markdown 导出、网页截图）

```bash
pip install playwright
playwright install chromium
```

> 中文渲染需要中文字体。Linux 上如遇方块字，安装 `fonts-noto-cjk`。

---

## 视频下载

> 目标：把**你有权保存的**抖音 / B站 / 快手 作品链接，变成任务中心里可下载的本地文件。
> 默认面向本机 / 内网自用，**公网部署请关闭**（见本节末尾）。

### 支持平台与链接格式

| 平台 | 可粘贴的链接 | 能力 |
|------|--------------|------|
| 抖音 | `https://v.douyin.com/xxx/`、`https://www.douyin.com/video/<id>`、`https://www.iesdouyin.com/share/video/<id>` | 视频、图文（打包 zip）、批量 |
| B站 | `https://www.bilibili.com/video/BV...`、`https://b23.tv/xxx`、带 `?p=2` 的多分P | 清晰度列表、仅音频、多分P |
| 快手 | `https://v.kuaishou.com/xxx`、`https://www.kuaishou.com/short-video/<id>` | 视频、图文 |

支持**整段分享文案**（含中文口令、表情、尾部标点）粘贴：自动提取链接、同批去重、
单次最多 20 条；批量结果逐条返回，单条失效不会让整批失败。

> 抖音分享页自 2026-10 起对普通请求不再内嵌作品数据，适配器会先申请一个**匿名访客标识**
> （`ttwid`，不代表登录态）后重试；若平台再次改版导致解析失败，可在设置中填写本人账号的
> 抖音 Cookie，或安装 yt-dlp 兜底引擎。

### 能力边界（明确不做）

- **不去水印**：只保存原站公开可播地址，不改画面、不绕水印。
- **不碰 DRM / 付费 / 会员 / 地区锁**内容：遇到直接返回 `VIP_REQUIRED` / `REGION_LOCKED` / `LOGIN_REQUIRED`，不尝试绕过。
- **不抓评论/弹幕/用户主页**，不做批量爬取与定时监控；解析与下载分别限流（20 次/分钟、10 次/分钟），同平台连续失败会熔断冷却 10 分钟。
- **不保存媒体直链**：历史记录只留作品链接与产物 `file_id`，媒体地址解析后即用即弃。
- **不绕过登录态**：B站 1080P 等需登录清晰度只标记 `need_login`，由用户在设置里自行配置 Cookie。

### 清晰度与默认配置

| 项 | 默认 | 说明 |
|----|------|------|
| `video.prefer_quality` | `1080P` | 可选 `4K` / `1080P` / `720P` / `480P` |
| `video.prefer_format` | `mp4` | `mp4` / `mp3`（仅音频） / `m4a` |
| `video.max_concurrent` | `2` | 同时下载任务数（1–4） |
| `video.download_segments` | `4` | 单任务分片并发（1–8） |
| `video.max_file_mb` | `2048` | 单文件上限，超出 `OUTPUT_TOO_LARGE` |
| `video.task_timeout_sec_video` | `600` | 视频任务超时（秒） |
| `video.dedupe_window_hours` | `24` | 产物复用窗口：窗口内重复下载直接复用，不重复请求平台 |
| `video.max_urls` | `20` | 单次最多解析链接数 |
| `video.history_limit` | `200` | 历史记录条数上限 |

默认清晰度策略：命中用户偏好则用；否则取 **≤1080P 的最高可用**，**绝不默认选 4K**。
「可用」指平台真的返回了媒体地址：需登录、会员专属、平台只给档位没给流的项会在下拉中置灰，
默认选择与下载都不会落到这些项上。

### 登录态（Cookie）

B 站 1080P 与部分需登录作品需要 Cookie：在「视频下载 → 设置」里粘贴（写后即加密落盘，
响应**只回显脱敏值**，如 `SESSDATA=****9f2a`；留空表示清除）。Cookie 只存在本机
`storage/credentials.json`，不写日志、不进 Git、前端不保存明文。

### 可选依赖

- **FFmpeg**：只有 DASH 分离音视频（B站典型）与「仅音频 mp3」需要合并/转码；单流 mp4 与图文 zip 不依赖。缺失时任务返回 `ENGINE_UNAVAILABLE` + 安装指引。
- **yt-dlp**：三级解析链的最后兜底，未安装时自动跳过（引擎自检显示 `state=fail`，不影响模块使用）。

安装方式见上文「引擎依赖」小节。

### 合规声明

本模块仅供**个人学习研究**，请勿用于商业传播或侵犯他人著作权的内容；下载前请确认你拥有
相应权利或已获授权。链接与产物只在本机处理，但**访问平台页面/接口本身仍受平台用户协议约束**，
相关风险由部署者承担。产物默认 24 小时后自动删除（`limits.output_ttl_hours`）。

### 公网部署默认关闭（重要）

`config.toml` 的 `[video] enabled` 默认为 `true`，是为了本机/内网开箱即用；
**只要服务可能被公网访问，请改为 `false`**：关闭后所有 `/api/video/*` 立即返回
`503 SERVICE_DISABLED`，前端入口同步提示模块未启用。

```toml
[video]
enabled = false
```

---

## 部署

### Docker Compose

`docker-compose.yml` 已包含健康检查、资源限额、日志轮转与存储卷。
默认镜像**不含** LibreOffice/FFmpeg（控制体积），需要时：

```bash
docker build --build-arg WITH_ENGINES=1 -t toolkit:full .
```

或用卷复用宿主已装的引擎，并同步改 `config.toml` 的 `engines` 路径。

### Nginx 反向代理

见 [`deploy/nginx.conf`](deploy/nginx.conf)。三个关键点已在该文件注释中说明：
上传体积上限、WebSocket 升级头（终端必需）、放宽的代理超时。

### systemd

见 [`deploy/toolkit.service`](deploy/toolkit.service)，已包含 `NoNewPrivileges`、
`ProtectSystem=strict`、`PrivateTmp` 等加固项与内存/CPU 限额。

```bash
sudo cp deploy/toolkit.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now toolkit
journalctl -u toolkit -f
```

---

## 开发

```bash
make venv install-dev      # 建虚拟环境 + 装依赖
make dev                   # 热重载启动
make test                  # 跑测试
make lint fmt              # 静态检查 / 格式化
make check                 # lint + test（提交前）
make help                  # 查看全部命令
```

测试分两层：
- `tests/test_contract.py` —— **契约测试**：逐个断言前端 `api.js` 会调用的端点存在、
  字段名对齐、错误体结构统一、登录门禁在未登录时拒绝全部面板接口。改动前端调用后必须同步更新其中的端点表。
- `tests/test_core.py` —— 核心层单元测试：路径穿越、口令/令牌、magic bytes、矩阵一致性。
- `tests/test_video.py` —— 视频下载模块离线测试：链接提取/去重、清晰度归一化、命名清洗、
  SSRF 拦截（断言未发任何网络请求）、Cookie 加密与脱敏、历史原子写/上限/去重、
  三平台夹具解析（`tests/fixtures/video/**`，全部自造脱敏样本）、契约端点与 503/401 分支。

不装 pytest 也能跑的自检脚本（纯标准库 + ASGI 直连）：
`tests/check_gate.py`（登录门禁）、`tests/check_contract.py`（接口契约回归）、
`tests/check_auth_flow.py`（账号全流程，需未配置 SMTP）。

```bash
pytest -q                          # 全部
pytest tests/test_contract.py -v   # 只跑契约
python tests/check_gate.py         # 门禁自检（无需 pytest / httpx）
```

---

## API 契约

所有业务接口统一前缀 `/api`；错误体统一为 `{"detail": "<中文可读>", "code": "<机器码>"}`。

### 系统

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/healthz` | 健康检查（**前端启动探测入口**，无需鉴权；未登录时只返回「在线 + 需要登录」） |
| GET | `/api/system/engines` | 引擎自检（允许阻塞探测） |
| POST | `/api/system/engines/refresh` | 强制重新探测 |
| GET | `/api/system/storage` | 磁盘与资源概览 |

### 鉴权与账号

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/auth/config` | **公开**：是否开放注册、是否需要验证码、邮件是否已配置 |
| POST | `/api/auth/send-code` | 发送邮箱/短信验证码（`target` / `channel` / `purpose`） |
| POST | `/api/auth/register` | 邮箱 + 验证码注册，成功直接返回 Token |
| POST | `/api/auth/login` | 登录，返回 `{token, user, expires_in}` |
| POST | `/api/auth/logout` | 登出（审计留痕） |
| GET | `/api/auth/me` | 当前身份 |
| POST | `/api/auth/password` | 修改密码（数据库用户会拿到新 Token） |
| POST | `/api/auth/reset-password` | 忘记密码：邮箱验证码重置 |

### 账号管理（仅管理员）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/admin/users` | 用户列表（`page` / `pageSize` / `search`） |
| PATCH | `/api/admin/users/{id}` | 改角色 / 状态 / 显示名 |
| POST | `/api/admin/users/{id}/revoke-tokens` | 强制该用户全部下线 |
| DELETE | `/api/admin/users/{id}` | 删除用户 |

### 转换 / 任务 / 下载

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/convert/matrix` | 支持的转换矩阵（按引擎可用性动态禁用） |
| POST | `/api/convert` | 提交转换（multipart：`files` + `source_format` + `target_format` + `params`） |
| POST | `/api/convert/batch-zip` | 批量打包下载 |
| POST | `/api/convert/pdf-tools` | PDF 工具集（合并/拆分/旋转/加解密） |
| GET | `/api/tasks` | 任务列表（`page` / `pageSize` / `status` / `module`） |
| GET | `/api/tasks/{id}` | 任务详情（前端轮询用） |
| DELETE | `/api/tasks/{id}` | `?purge=true` 删除记录与产物；否则为取消 |
| GET | `/api/download/{file_id}` | 下载产物 |

### 音频 / Markdown / SSH

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/audio/probe` | 格式嗅探（**逐文件**返回，单个失败不影响整批） |
| POST | `/api/audio/convert` | 提交音频转换 |
| POST | `/api/md/export` | Markdown → PDF / HTML |
| POST | `/api/ssh/connect` | 建立 SSH 会话 |
| GET | `/api/ssh/sessions` | 活跃会话列表 |
| GET | `/api/ssh/hosts` | 已保存主机（由前端 localStorage 保管，固定返回空） |
| DELETE | `/api/ssh/{id}` | 断开会话 |
| WS | `/ws/ssh/{session_id}` | 终端双向数据通道 |

**WebSocket 帧协议**（与前端约定）：
- 客户端 → 服务端：**二进制帧** = 终端输入字节；**文本帧** = 控制指令（目前仅 `{"type":"resize","cols":80,"rows":24}`）
- 服务端 → 客户端：**二进制帧** = 远端输出原始字节（前端按 UTF-8 流式解码）

### 视频下载

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/video/platforms` | 平台与引擎状态、能力矩阵、Cookie 是否已配置（脱敏） |
| POST | `/api/video/parse` | 解析 1–20 条链接，**逐条返回**，单条失败不影响整批 |
| POST | `/api/video/download` | 提交下载任务，返回 `TaskRecord.to_api()` 数组（`module="video"`） |
| GET | `/api/video/history` | 历史记录（`platform` / `keyword` / `page` / `pageSize`） |
| DELETE | `/api/video/history/{item_id}` | 删除单条历史（`?platform=douyin`） |
| DELETE | `/api/video/history` | 清空历史（不删产物，产物按 TTL 回收） |
| GET | `/api/video/settings` | 模块设置 + 三平台开关 + Cookie 脱敏状态 |
| PUT | `/api/video/settings` | 部分更新设置 / 写入 Cookie（空串=清除，响应永不回显明文） |

模块关闭（`[video] enabled=false`）时以上全部返回 `503 SERVICE_DISABLED`。

### 地图定位

**该模块是纯前端模块，不新增任何后端接口**（唯一的服务端配合是 CSP 放行在线瓦片域名，见下）。

| 能力 | 说明 |
|------|------|
| 坐标解析 | 十进制度、度分秒、空格分隔度分秒、半球字母前置/后置、中文「北纬/东经」、全角标点，粘贴即用 |
| 自动定位 | 输入即定位（420 ms 防抖）；回车/按钮立即定位；点击地图反查坐标 |
| 读数换算 | 十进制度、度分秒、UTM（含带号与纬度带）、GCJ-02 火星坐标、Maidenhead 网格、国家/地区、最近参考地点、距上一次定位 |
| 省市区地址 | 逆地理到**省 / 市 / 区县 / 街道 / 道路 / 门牌**：填高德 Key 走高德（国内最准），留空走 OpenStreetMap Nominatim；8 秒超时并给出提示 |
| 底图来源 | 三选一：**高德**（默认，中文标注 + 道路级，GCJ-02 自动对齐）/ OpenStreetMap / 离线国界底图；瓦片失败自动退回离线 |
| 离线可用 | 即使完全断网：Natural Earth 110m 国界（TopoJSON，107 KB，随站点下发）+ 经纬网 + 参考城市 |
| 分享 | URL 携带 `#/geo?lat=…&lon=…&z=…`，可分享、可刷新恢复；历史记录存本机 localStorage |

设计取舍（详细理由见 `app/static/js/geo/*.js` 的模块注释）：

- **不引入 Leaflet/OpenLayers**：离线可用 + `script-src 'self'` 的约束下，为一张「看点」地图
  背上 200 KB 依赖不划算；自研部分只有 Web 墨卡托投影、平移缩放、打点与瓦片层。
- **高德瓦片做 GCJ-02 对齐**：高德是 GCJ-02 坐标系，与本站内部的 WGS-84 相差数百米；
  绘制瓦片层时按当前中心反算偏移量整体平移，标记才不会偏。
- **逆地理由浏览器直连**：高德/Nominatim 都是 `fetch`，**不经本站服务器转发** ——
  部署机不必有外网出口，Key 也只保存在用户浏览器里。
- **CSP 只放行必要域名**：`img-src` 放行两个瓦片域，`connect-src` 放行两个逆地理域，
  其余出口保持关闭。
- **不提供「获取当前位置」**：`Permissions-Policy` 明确禁用 geolocation，只处理用户手工输入/粘贴的坐标。

- **空值必须与 0 区分**：`Number(null) === 0` 曾让未定位状态显示成「0.000000, 0.000000」，
  故 `isValidLat/isValidLon` 显式排除 `null` / `''` / 布尔。

---

## 架构与设计决策

记录几个"为什么这样做"，避免后来者反复权衡：

**1. 用 ThreadPoolExecutor 而非 ProcessPoolExecutor**
重活全在外部子进程（soffice/ffmpeg/chromium）里，Python 侧只在等 IO，
不存在 GIL 争抢。进程池反而要求任务闭包可 pickle，带来 Windows spawn 重导入、
跨进程进度回传等三个真实代价。参见 `app/core/tasks.py` 模块 docstring。

**2. 配置不用 pydantic-settings 的 TomlConfigSettingsSource**
该能力依赖 pydantic-settings ≥ 2.2，版本漂移会让离线包在用户机器上直接起不来。
改用标准库 `tomllib` + 显式环境变量覆盖，零版本风险，校验仍由 Pydantic 保证。

**3. `/healthz` 绝不做阻塞工作**
它是前端启动探测入口（1.5 秒超时），决定界面走"直连"还是"演示"模式。
因此只读引擎探测缓存，未就绪时返回 `unknown`，而不是现场跑一遍探测。

**4. 静态挂载必须放在路由注册之后**
`app.mount("/", StaticFiles(...))` 挂在 `/` 上会捕获所有未匹配路径，
前置会让所有 API 请求被它截胡返回 404。

**5. 任务超时用协作式取消而非 `future.result(timeout=)`**
后者只是"不再等待"，任务线程仍在后台跑并占着 worker 槽位。
这里用置取消标记 + 任务函数在检查点自查退出。

**6. 产物文件名 UUID 化**
`storage/outputs/` 下以 `file_id` 命名，原始文件名只存在于登记表中。
避免用户可控文件名带来的路径穿越与覆盖风险（NFR-04）。

---

## 账号体系（邮箱注册 / 验证码）

项目原本是**单管理员模式**（`config.toml` 里一个账号），现在并存两种身份：

| 身份 | 存放位置 | 用途 |
|------|----------|------|
| **超级管理员** | `config.toml` 的 `[server] username/password_hash` | **带外救援**。不落库，数据库损坏、注册系统故障、唯一管理员被误删时仍能登录 |
| **注册用户** | `storage/toolkit.db`（SQLite） | 邮箱注册产生，角色 `user` / `admin` |

### 三种流程

- **注册**：填邮箱 → 收验证码 → 设密码 → 直接登录
- **登录**：邮箱 + 密码（可选叠加邮箱验证码二次验证）
- **忘记密码**：邮箱 → 验证码 → 重置

### 开启注册

```toml
[auth]
allow_registration = true          # 开放注册
require_email_verification = true  # 必须邮箱验证（生产务必保持）

[email]
enabled = true
host = "mail.example.com"          # 自建 BillionMail 或第三方 SMTP
port = 465
security = "ssl"                   # ssl | starttls | none
username = "noreply@example.com"
password = "邮箱密码或SMTP授权码"
from_name = "全能工具站"
site_url = "https://toolkit.example.com"
```

> ⚠️ **邮件未配置时开放注册 = 用户收不到验证码**。启动日志会明确告警，
> 前端也会在注册页提示「服务端尚未配置邮件服务」。
> `[email] enabled=false` 时验证码会写入日志（便于本地联调），**不会真正外发**。

### 忘记超级管理员密码

```bash
python scripts/reset_admin.py                    # 随机生成新密码并写回 config.toml
python scripts/reset_admin.py --password 'xxx'   # 指定新密码
python scripts/reset_admin.py --list-users       # 查看注册用户
python scripts/reset_admin.py --promote a@b.com  # 提升为管理员
python scripts/reset_admin.py --revoke a@b.com   # 强制下线
```

### 安全设计

- 验证码**只存哈希**（`HMAC-SHA256(应用密钥, 目标|用途|码)`），数据库泄露也读不出码
- 三重限流：同目标重发间隔 60s、同目标每小时 10 次、同 IP 每小时 20 次
- 验证码 5 分钟过期、最多 5 次尝试、**单次消费**（防重放）
- 改密 / 禁用后递增 `token_version`，**旧 Token 立即失效**（此前项目做不到吊销）
- 邮箱枚举防护：「忘记密码」对未注册邮箱返回与成功一致的响应

---

## 手机端（移动浏览器）

界面是同一套页面，手机访问时自动切到移动布局，**不需要单独的移动版站点**：

| 变化 | 说明 |
|------|------|
| 底部标签栏 | 主导航从顶栏下沉到屏幕底部（等分项 + 当前项指示条，列数按模块数量自适应），拇指可及 |
| 单列滚动 | 转换中心 / 音频工具改为整页滚动，参数区与结果区顺序排布；主操作按钮吸底 |
| SSH 终端 | 上方连接表单限高 42dvh 并可内部滚动，终端占满剩余空间（xterm 需要固定尺寸） |
| 弹窗 → 底部抽屉 | 登录、确认等弹窗贴底展开，键盘弹出时不会被顶出屏幕 |
| 触摸尺寸 | 按钮 40px、输入框 42px 且字号 16px（避免 iOS 聚焦时自动放大整页） |
| 安全区 | 刘海屏/底部手势条用 `env(safe-area-inset-*)` 留白 |

断点：`≤1024px` 平板改上下堆叠；`≤720px` 或「触摸设备且高度 ≤520px」（横屏手机）走移动布局；
`≤380px` 与横屏矮屏另有压缩。移动层集中在 `app/static/css/mobile.css`，
**必须排在其它样式表之后**（它整层覆盖前层布局），删掉这个 `<link>` 即回到桌面版。

移动层的样式由 `tests/check_css.py` 把关（括号配平、`var()` 是否有定义、
`#id` 选择器是否真实存在、`mobile.css` 是否最后加载）：

```bash
python tests/check_css.py
```

前端另有两条自检（纯 Node，无需浏览器）：

```bash
node tests/check_geo.mjs        # 坐标解析/换算/UTM/GCJ-02/TopoJSON 解码/点落国判定
node tests/check_geo_view.mjs   # 地图定位视图装配与交互（极简 DOM/Canvas 替身跑真实模块）
```

---

## 安全说明

- **主界面登录门禁（默认开启）**：`auth_required = true` 时，未登录访客只能看到登录页——
  1) 首页 `<html data-app-gate="locked">` 写死在 HTML 里，样式表在首帧就隐藏整个控制面板外壳
  （不会闪出导航/状态栏）；2) 前端在确认登录态之前不构建导航、不启动路由、不拉任何业务数据；
  3) 服务端所有 `/api/*` 业务接口对无 Token 请求一律 401，`/healthz` 只返回「在线 + 需要登录」，
  磁盘余量、引擎清单、任务计数、运行时长一概不下发；4) 登出或登录态失效时清空面板 DOM 与内存数据
  再回到登录页。把 `auth_required` 置 `false` 可退回免登录的本机离线自用形态。
- **局域网暴露强制鉴权**：`--lan` 与 `allow_lan=true` 都会强制 `auth_required=true`。
- **登录失败限流**：按 IP 滑动窗口，窗口内失败超限即拒绝，抵御默认口令爆破。
- **路径穿越防护**：`file_id` 走白名单正则；所有路径拼接经 `assert_within` 校验。
- **扩展名不可信**：落盘后按 magic bytes 复核真实类型，拒绝"改扩展名伪装"。
- **凭据不落盘**：SSH 密码/私钥仅存活于内存保险箱，使命结束即就地擦除；
  审计日志只记录认证方式，不记录凭据。
- **SSH 默认关闭**：避免误开成为公网跳板。
- **视频下载 SSRF 防护**：只接受白名单平台域名、强制 https，重定向**每跳重新校验**；
  字面内网/回环/链路本地/云元数据地址（`127.0.0.1`、`10.*`、`169.254.169.254` 等）
  与解析到内网的域名一律 `URL_BLOCKED`，`file/ftp/data` 等 scheme 直接拒绝。
- **Cookie 加密落盘**：B站/抖音/快手 Cookie 写入即加密（`storage/credentials.json`，权限 0600），
  接口只回显 `SESSDATA=****9f2a` 形式的脱敏值，日志与审计不记录 Cookie 与完整 URL。
- **视频模块默认公网关闭**：见「视频下载 → 公网部署默认关闭」。
- **严格 CSP**：默认下发 `script-src 'self'` 等策略，无内联脚本。

> 对外暴露前请务必：改掉初始密码、确认 `auth_required=true`、
> 配置 HTTPS（见 `deploy/nginx.conf`）、启用 SSH 白名单。

---

## 常见问题

**Q：转换失败，提示"缺少依赖 xxx"？**
对应引擎未安装。到「设置 → 引擎自检」可看到每个引擎的具体安装指引。

**Q：上传大文件报 413？**
先确认文件属于哪个模块：文档模块看 `config.toml` 的 `limits.max_upload_mb`（默认 100），
音频模块看 `limits.max_audio_upload_mb`（默认 200）；同时把 Nginx 的
`client_max_body_size` 调到**不小于两者中的较大值**。

**Q：SSH 终端连不上 / 页面提示模块未启用？**
SSH 默认关闭。在 `config.toml` 的 `[ssh]` 段设 `enabled = true` 后重启。

**Q：产物过一段时间就打不开了？**
产物默认保留 24 小时（`limits.output_ttl_hours`），到期由后台线程清理。

**Q：视频下载提示模块未启用 / 接口返回 503 `SERVICE_DISABLED`？**
`config.toml` 的 `[video] enabled = false`，或环境变量 `TOOLKIT_VIDEO_ENABLED=false` 覆盖了它。
公网部署**建议保持关闭**；本机自用时改为 `true` 并重启。

**Q：B站只能下到 720P？**
未配置 Cookie 时匿名最高 720P，1080P 需要登录态。解析结果里会标 `LOGIN_REQUIRED`，
到「视频下载 → 设置」粘贴浏览器里的 `SESSDATA` 后重试即可。

**Q：视频任务失败，提示缺 FFmpeg？**
B站等 DASH 分离音视频需要 FFmpeg 合并；把静态构建解压到 `runtime/ffmpeg/`（见「引擎依赖」）。
单流 mp4 与图文 zip 不需要 FFmpeg。

**Q：Windows 上 LibreOffice 转换报错找不到 soffice？**
`config.toml` 里把 `engines.libreoffice` 改为绝对路径，例如
`C:/Program Files/LibreOffice/program/soffice.exe`（正斜杠或双反斜杠）。

**Q：终端里中文显示成方块？**
缺中文字体。Linux 安装 `fonts-noto-cjk`；Windows 通常自带。

**Q：`pip install` 报 `Could not find a version ... (from versions: none)`？**
若指定了镜像源（清华 / 阿里）仍报此错，多半是 **pip ≥ 26 与镜像 API 格式不兼容** ——
新版 pip 优先请求 PEP 691 的 JSON 索引，部分镜像未提供该格式，pip 不会回退。
此时**去掉 `-i` 用默认源**即可：

```bash
pip install -r requirements-core.txt
```

**Q：`start.bat` 报 `process exited with code 9009`？**
9009 = 命令未找到。最常见成因是 Microsoft Store 的 `python.exe` 占位程序，
详见 [快速开始](#windows双击-startbat)。新版脚本会自动识破它并打印安装指引。

**Q：确实装了 Python，但 `start.bat` 仍说找不到？**
安装时可能漏勾 **Add python.exe to PATH**。最省事的绕法是在项目根目录建本地虚拟环境，
彻底不依赖系统 PATH：

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-core.txt
```

之后 `start.bat` 会优先使用 `.venv` 里的解释器。

---

## 许可

本项目自身代码为私有许可。注意其**第三方依赖**的许可差异：

- PyMuPDF：**AGPL-3.0**（对外提供 SaaS 或闭源分发前需评估）
- mutagen：**GPL-2.0**
- LibreOffice：**MPL-2.0**
- FFmpeg：**LGPL-2.1+ / GPL-2.0+**（取决于构建选项）
- yt-dlp：**Unlicense**（可选依赖，不装不影响视频模块使用）
- 其余（FastAPI/pypdf/pdfplumber/openpyxl/python-pptx/mammoth/paramiko/Playwright）为宽松许可

若需闭源商业分发，建议以 pypdf + pdfplumber 替换 PyMuPDF 光栅化路径。
