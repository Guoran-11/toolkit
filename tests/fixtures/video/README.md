# 视频下载模块 · 离线夹具

> 全部为**自造脱敏样本**：域名、作品 ID、作者名、图片/视频地址均为占位值，
> 不含任何真实用户信息。仅用于适配器解析测试，**不需要联网**，
> 测试中不会再对夹具里的 URL 发起请求。

## bilibili（B站）

| 文件 | 场景 | 关键点 |
|------|------|--------|
| `view_ok.json` | 普通稿件 | `code=0`，单分P，含 `owner/stat/pages` |
| `view_login_1080p.json` | 1080P 需登录 | 匿名只可拿 720P/480P，`warnings` 应含 `LOGIN_REQUIRED` |
| `view_multipart.json` | 多分P | `videos=2`，`pages` 两条 |
| `view_invalid.json` | 失效稿件 | `code=-404`、`data=null` |
| `playurl_ok.json` | DASH 播放地址 | `accept_quality=[80,64,32]`，video/audio 分离 |
| `playurl_login_1080p.json` | 匿名最高 720P | `accept_quality` 含 80 但 `dash.video` 只有 64/32 |
| `playurl_forbidden.json` | 无权限 | `code=-10403` |
| `playurl_duplicate_codecs.json` | 同 qn 多编码 | `dash.video` 里 qn=32 给 codecid 7/12/13 三条、qn=16 一条；不返回 720P/1080P 流（用于验证 format_id 唯一与占位项 `available=false`） |
| `page_initial_state.html` | 网页降级样本（保留） | `window.__INITIAL_STATE__` + `og:*`；当前适配器只走 JSON 接口，未被解析用例引用 |

## douyin（抖音）

| 文件 | 场景 | 关键点 |
|------|------|--------|
| `router_data_video.html` | 普通视频 | `loaderData["video_(id)/page"].videoInfoRes.item_list[0]`，含 `bit_rate[]` |
| `router_data_images.html` | 图文 | `images` 三条、`video.play_addr` 为空、`aweme_type=68` |
| `router_data_login.html` | 需登录 | `play_addr.url_list=[]`、`status.private_status=1` → `LOGIN_REQUIRED` |
| `router_data_no_title.html` | 缺字段 | 无标题字段，标题只能取 `desc`；作者名为空 |
| `router_data_empty.html` | 失效 | `item_list=[]` + `filter_list[].detail_msg` |
| `render_data.html` | 页面 JSON 降级 | `window.RENDER_DATA`（URL 编码 JSON 字符串，适配器先 unquote 再解析） |
| `share_page_video_info_res.html` | 需匿名访客 Cookie 的分享页 | `loaderData["video_(id)/page"].videoInfoRes.item_list[0]`；不含 `og:*`，用于锁定 ttwid 重试链路 |
| `og_only.html` | og 降级 | 只有 `og:title/og:image/og:video` |

## kuaishou（快手）

| 文件 | 场景 | 关键点 |
|------|------|--------|
| `apollo_video.html` | 普通视频 | `window.__APOLLO_STATE__.defaultClient["VisionVideoDetailPhoto:..."]` |
| `apollo_images.html` | 图文 | `imageUrls` 三条、`photoUrl` 为空、`workType=2` |
| `initial_state.html` | 页面 state 降级 | `window.__INITIAL_STATE__.videoDetail` |
| `apollo_login.html` | 需登录/会员 | `memberOnly=true`、`loginRequired=true`、`photoUrl` 为空 |
| `apollo_empty.html` | 失效 | `defaultClient={}` |
| `og_only.html` | og 降级 | 只有 `og:*` |

## 约定

- 夹具是**输入**，不是断言目标：适配器解析失败时应给出明确错误码
  （`ITEM_NOT_FOUND` / `LOGIN_REQUIRED` / `PARSE_FAILED`），而不是抛裸异常。
- 新增夹具请沿用上述命名前缀（`view_*` / `playurl_*` / `router_data_*` /
  `render_data_*` / `apollo_*` / `initial_state_*` / `og_only_*`），
  测试按前缀选择场景，避免因文件顺序而变得脆弱。
