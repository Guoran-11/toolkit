/**
 * config.js — 前端全局配置
 *
 * 设计原则：
 * 1. 所有可调参数集中在唯一入口，禁止在业务代码里散落魔数。
 * 2. 敏感信息（密码、私钥、Token）绝不写入此文件；本文件会随静态资源下发到浏览器。
 * 3. `mode` 支持 'auto' | 'live' | 'demo'：
 *    - auto：启动时探测 /healthz，探测失败自动降级为 demo（后端未就绪也能跑通 UI）
 *    - live：强制走后端 API
 *    - demo：强制走内置模拟数据（用于纯前端评审 / 离线演示）
 *
 *    运行时可覆盖：在地址后加 `?mode=demo` 即可免探测直接进入演示模式
 *    （纯静态托管或 file:// 直接打开时很有用，可避免一次必然失败的 /healthz 请求）。
 *    另外 file:// 协议下 fetch 受同源策略限制，这里自动判定为 demo。
 */

/**
 * 解析最终运行模式。
 * 优先级：URL 查询参数 > file:// 自动判定 > 构建期默认值
 * @param {'auto'|'live'|'demo'} fallback
 */
function resolveMode(fallback) {
  try {
    const q = new URLSearchParams(window.location.search).get('mode');
    if (q === 'demo' || q === 'live' || q === 'auto') return q;
    // 以文件方式直接打开：没有 HTTP 服务，任何接口探测都注定失败
    if (window.location.protocol === 'file:') return 'demo';
  } catch {
    /* 非浏览器环境（如静态分析 / 单测）忽略，退回默认值 */
  }
  return fallback;
}

export const CFG = Object.freeze({
  /** 应用元信息 */
  app: {
    name: '全能工具站',
    codename: 'ToolKit',
    version: '1.0.0',
    build: '2026.09.16',
  },

  /** API 根路径。同源部署留空字符串；前后端分离时改为完整地址（需后端配 CORS） */
  apiBase: '',

  /** 运行模式（'auto' | 'live' | 'demo'，可被 ?mode= 覆盖） */
  mode: resolveMode('auto'),

  /** 后端探测：健康检查端点与超时（毫秒） */
  probe: {
    path: '/healthz',
    timeoutMs: 1500,
  },

  /** 任务轮询间隔（规格书 §7.2：前端轮询间隔 1 s） */
  pollIntervalMs: 1000,

  /** 与后端 config.toml [limits] 保持一致的默认值，仅用于前端先行校验 */
  limits: {
    maxUploadMb: 100,
    /** 音频模块单独放宽的上限（后端 limits.max_audio_upload_mb） */
    maxAudioUploadMb: 200,
    maxBatchFiles: 20,
    pollTimeoutMs: 300000, // 单任务前端轮询上限 5 分钟，超时提示用户去任务中心
    /** 视频下载：单次最多解析的链接数（与后端 [video] max_urls 对齐，前端先行拦截） */
    maxVideoUrls: 20,
    /** 视频下载：单任务前端轮询上限 10 分钟（后端 task_timeout_sec_video 默认 600s） */
    videoPollTimeoutMs: 600000,
    /** 视频下载：单文件体积上限（MB），仅用于前端提示，最终以后端 max_file_mb 为准 */
    videoMaxFileMb: 2048,
  },

  /** localStorage 键名（统一前缀，避免与同域其他应用冲突） */
  storageKeys: {
    theme: 'toolkit.theme',
    token: 'toolkit.token',
    audioGateAccepted: 'toolkit.audio.gate',
    savedHosts: 'toolkit.ssh.hosts',
    panelWidth: 'toolkit.ui.panelWidth',
    geoHistory: 'toolkit.geo.history',
  },

  /** 本地 vendor 资源路径（相对 static/ 目录） */
  vendor: {
    marked: 'vendor/marked.umd.js',
    dompurify: 'vendor/purify.min.js',
    hljs: 'vendor/highlight.min.js',
    hljsCssLight: 'vendor/hljs-github.min.css',
    hljsCssDark: 'vendor/hljs-github-dark.min.css',
    katexJs: 'vendor/katex.min.js',
    katexCss: 'vendor/katex.min.css',
    katexAutoRender: 'vendor/auto-render.min.js',
    mermaid: 'vendor/mermaid.min.js',
    xtermJs: 'vendor/xterm.js',
    xtermCss: 'vendor/xterm.css',
    xtermFit: 'vendor/xterm-addon-fit.js',
    xtermWebLinks: 'vendor/xterm-addon-web-links.js',
  },

  /** 主题：'auto' 跟随系统，或显式 'light' / 'dark' */
  defaultTheme: 'auto',
});

/** 是否处于演示模式（由 api.js 在启动探测后写入） */
export const runtime = {
  demo: false,
  engines: { libreoffice: 'unknown', chromium: 'unknown', ffmpeg: 'unknown' },
  serverVersion: null,
};
