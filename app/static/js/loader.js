/**
 * loader.js — 本地 vendor 资源按需加载器
 *
 * 动机（性能）：mermaid ≈ 2.5 MB、KaTeX + 字体 ≈ 1 MB、xterm ≈ 0.4 MB，
 * 若在 index.html 里全量 <script> 同步引入，首屏 LCP 会明显恶化。
 * 因此只在真正用到时注入，且同一个资源只加载一次（Promise 缓存）。
 *
 * 约束：只允许加载同源本地路径（CFG.vendor.*），不接受外部 URL ——
 * 这是 NFR-08「运行期零外网请求」的技术保障。
 */

import { CFG } from './config.js';

/** 已加载 / 加载中的资源缓存：url → Promise */
const cache = new Map();

/**
 * 注入 <script>，加载完成后 resolve。
 * @param {string} url 相对 static/ 的路径
 * @param {string} [globalName] 加载后用于校验的全局变量名（可选）
 */
export function loadScript(url, globalName) {
  // 已存在同名全局对象 → 直接命中，避免重复注入
  if (globalName && window[globalName]) return Promise.resolve();

  const key = `js:${url}`;
  if (cache.has(key)) return cache.get(key);

  const task = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = url;
    script.async = true;
    script.dataset.vendor = url;

    script.addEventListener('load', () => {
      if (globalName && !window[globalName]) {
        reject(new Error(`资源已加载但未导出全局变量 ${globalName}：${url}`));
        return;
      }
      resolve();
    });
    script.addEventListener('error', () => {
      cache.delete(key); // 失败不缓存，允许后续重试
      reject(new Error(`本地资源加载失败：${url}（请确认 vendor 目录已随包分发）`));
    });

    document.head.append(script);
  });

  cache.set(key, task);
  return task;
}

/** 注入 <link rel=stylesheet> */
export function loadStyle(url) {
  if (document.querySelector(`link[data-vendor="${url}"]`)) return Promise.resolve();

  const key = `css:${url}`;
  if (cache.has(key)) return cache.get(key);

  const task = new Promise((resolve, reject) => {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = url;
    link.dataset.vendor = url;
    link.addEventListener('load', () => resolve());
    link.addEventListener('error', () => {
      cache.delete(key);
      reject(new Error(`本地样式加载失败：${url}`));
    });
    document.head.append(link);
  });

  cache.set(key, task);
  return task;
}

/* ------------------------------------------------------------------ *
 * 具体库的加载入口（全部幂等，可放心在任意时机调用多次）
 * ------------------------------------------------------------------ */

const V = () => CFG.vendor;

/** Markdown 解析（体积小，首屏即可加载） */
export const needMarked = () => loadScript(V().marked, 'marked');

/** HTML 净化 —— 渲染任何富文本前必须就绪（FR-MD-09） */
export const needDOMPurify = () => loadScript(V().dompurify, 'DOMPurify');

/** 代码高亮：同时注入基础样式，并按当前主题选择皮肤 */
export async function needHighlight(theme = 'light') {
  await Promise.all([
    loadScript(V().hljs, 'hljs'),
    loadStyle(theme === 'dark' ? V().hljsCssDark : V().hljsCssLight),
  ]);
  return window.hljs;
}

/** 按主题切换 highlight.js 皮肤（两个皮肤都注入后靠禁用/启用切换，避免闪烁） */
export function switchHighlightTheme(theme) {
  const light = V().hljsCssLight;
  const dark = V().hljsCssDark;
  const lightLink = document.querySelector(`link[data-vendor="${light}"]`);
  const darkLink = document.querySelector(`link[data-vendor="${dark}"]`);
  if (!lightLink && !darkLink) return; // 高亮尚未启用，无需处理
  const wantDark = theme === 'dark';
  if (darkLink) darkLink.disabled = !wantDark;
  if (lightLink) lightLink.disabled = wantDark;
}

/** 数学公式（KaTeX + auto-render） */
export async function needKatex() {
  await Promise.all([
    loadScript(V().katexJs, 'katex'),
    loadStyle(V().katexCss),
  ]);
  // auto-render 依赖 katex 已就绪，必须串行
  await loadScript(V().katexAutoRender, 'renderMathInElement');
  return window.renderMathInElement;
}

/** Mermaid 图表（体积最大，仅在文档确实含图表时才拉取） */
export async function needMermaid() {
  await loadScript(V().mermaid, 'mermaid');
  // 注意：必须显式返回全局对象。loadScript 只负责等待加载完成，
  // 其 resolve 值是无意义的 undefined，直接 `await needMermaid()` 会拿到 undefined。
  return window.mermaid;
}

/** xterm.js 终端（仅 SSH 视图需要） */
export async function needXterm() {
  await Promise.all([
    loadScript(V().xtermJs, 'Terminal'),
    loadStyle(V().xtermCss),
  ]);
  await Promise.all([
    loadScript(V().xtermFit, null),
    loadScript(V().xtermWebLinks, null),
  ]);
  return {
    Terminal: window.Terminal,
    FitAddon: window.FitAddon?.FitAddon,
    WebLinksAddon: window.WebLinksAddon?.WebLinksAddon,
  };
}

/** 预加载提示：给 UI 显示"正在准备渲染引擎…"用 */
export const vendorPaths = V;
