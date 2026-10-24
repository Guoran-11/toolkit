# vendor/ 第三方前端库清单与许可

本目录为**离线本地化**的前端依赖，运行期由同源静态服务提供，**不含任何 CDN 或外网请求**（对应需求文档 NFR-08）。

## 文件对照表

| 文件 | 库 | 版本 | 全局变量 | 许可 | 加载策略 |
|---|---|---|---|---|---|
| `marked.umd.js` | marked | 12.0.2 | `window.marked` | MIT | 进入 MD 预览视图时 |
| `purify.min.js` | DOMPurify | 3.1.6 | `window.DOMPurify` | Apache-2.0 / MPL-2.0 | 进入 MD 预览视图时 |
| `highlight.min.js` | highlight.js（common 语言集） | 11.9.0 | `window.hljs` | BSD-3-Clause | 首次出现代码块时 |
| `hljs-github.min.css` | highlight.js 浅色主题 | 11.9.0 | — | BSD-3-Clause | 随 highlight.js |
| `hljs-github-dark.min.css` | highlight.js 深色主题 | 11.9.0 | — | BSD-3-Clause | 随 highlight.js |
| `katex.min.js` + `katex.min.css` + `fonts/` | KaTeX | 0.16.11 | `window.katex` | MIT | 文档含 `$...$` 时 |
| `auto-render.min.js` | KaTeX auto-render 扩展 | 0.16.11 | `window.renderMathInElement` | MIT | 随 KaTeX |
| `mermaid.min.js` | Mermaid | 10.9.1 | `window.mermaid` | MIT | 文档含代码围栏 `mermaid` 时 |
| `xterm.js` + `xterm.css` | xterm.js | 5.3.0 | `window.Terminal` | MIT | 进入 SSH 终端视图时 |
| `xterm-addon-fit.js` | xterm-addon-fit | 0.8.0 | `window.FitAddon.FitAddon` | MIT | 随 xterm.js |
| `xterm-addon-web-links.js` | xterm-addon-web-links | 0.9.0 | `window.WebLinksAddon.WebLinksAddon` | MIT | 随 xterm.js |
| `geo/countries-110m.json` | world-atlas（Natural Earth 1:110m） | 2.0.2 | —（由 `js/geo/topojson.js` 解码） | Natural Earth 公有领域 / TopoJSON ISC | 进入地图定位视图时 |

## 地图底图数据说明

`geo/countries-110m.json` 是 TopoJSON 格式的国界数据（177 个国家/地区，107 KB），
用于地图定位模块的**离线底图**：不需要任何外网请求即可显示国界、经纬网与参考城市。

- 数据源：<https://www.naturalearthdata.com/>（1:110m Cultural Vectors，**公有领域**）
- 打包格式：<https://github.com/topojson/world-atlas>（TopoJSON 由 Mike Bostock 维护，ISC 许可）
- 获取方式：`https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json`（构建期一次性下载后随仓库分发）

在线瓦片（OpenStreetMap）是**用户显式开启**的可选项，默认关闭；
开启后由浏览器直接请求 `tile.openstreetmap.org`，数据与版权归 © OpenStreetMap 贡献者（ODbL）。

## 体积说明

| 资源 | 体积 | 说明 |
|---|---|---|
| `mermaid.min.js` | 3.3 MB | 占总量的 63%，**必须懒加载**（见 `js/loader.js`） |
| `katex.min.js` + 字体（60 个文件） | ≈ 1.1 MB | 仅含公式时才加载 |
| `xterm.js` | 277 KB | 仅 SSH 视图加载 |
| highlight.js | 119 KB | 仅含代码块时加载 |
| marked + DOMPurify | 121 KB | MD 视图基础依赖 |

## 重新生成方式

本目录由构建期脚本从 npm 拉取并复制生成，**不手工维护**。升级版本时：

```bash
# 1. 修改 toolkit/tests/fetch-vendor.sh 顶部的版本号
# 2. 重新生成
bash toolkit/tests/fetch-vendor.sh
# 3. 同步更新本文件《文件对照表》中的版本号
```

> 复制清单与版本号集中在 `toolkit/tests/fetch-vendor.sh`，本文件不再重复命令，
> 避免两处版本描述漂移。KaTeX 字体必须与 `katex.min.css` 同层落地到 `fonts/`，
> 脚本已包含该步骤。

## 许可合规提示

本目录所列库均为 MIT / BSD / Apache 类宽松许可，**可随产品闭源分发**。
体积较大的 `mermaid` 与 `markdown 渲染链` 不涉及 AGPL/GPL 传染性风险。

> 注意：需求文档 §8.1 提示的后端依赖 `PyMuPDF`（AGPL-3.0）与 `mutagen`（GPL-2）属于**服务端**组件，
> 与本目录无关，但其分发合规问题需在产品立项阶段单独确认（对应文档待确认事项 Q1）。
