/**
 * utils.js — 通用纯函数工具集合
 * 约束：本文件不引入任何其他模块，保持零依赖，便于被任意视图复用。
 * 所有 DOM 查询结果一律先判空再访问属性（避免 null 引用崩溃）。
 */

/* ------------------------------------------------------------------ *
 * 1. DOM 简写
 * ------------------------------------------------------------------ */

/** 查询单个元素，未命中返回 null（调用方必须判空） */
export const $ = (sel, root = document) => root.querySelector(sel);

/** 查询多个元素，始终返回真数组（可安全 forEach / map） */
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/**
 * 创建元素。
 * @param {string} tag            标签名，支持 'div.cls#id' 简写
 * @param {object} [attrs]        属性；class / text / html / dataset / on 有特殊处理
 * @param {(Node|string)[]} [children]
 */
export function el(tag, attrs = {}, children = []) {
  const [name, ...rest] = tag.split(/(?=[.#])/);
  const node = document.createElement(name);

  for (const token of rest) {
    if (token.startsWith('.')) node.classList.add(token.slice(1));
    else if (token.startsWith('#')) node.id = token.slice(1);
  }

  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;

    if (key === 'text') {
      node.textContent = String(value);
    } else if (key === 'html') {
      // 仅在内容已由调用方保证安全（如经过 DOMPurify）时使用
      node.innerHTML = String(value);
    } else if (key === 'class') {
      String(value).split(/\s+/).filter(Boolean).forEach((c) => node.classList.add(c));
    } else if (key === 'dataset') {
      Object.assign(node.dataset, value);
    } else if (key === 'style' && typeof value === 'object') {
      Object.assign(node.style, value);
    } else if (key === 'on' && typeof value === 'object') {
      for (const [evt, handler] of Object.entries(value)) node.addEventListener(evt, handler);
    } else if (value === true) {
      node.setAttribute(key, '');
    } else {
      node.setAttribute(key, String(value));
    }
  }

  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** 清空节点全部子元素（比 innerHTML='' 更快且不触发解析） */
export function clear(node) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
}

/** 安全设置 innerHTML：若 DOMPurify 已就绪则强制净化 */
export function setHTML(node, html) {
  if (!node) return;
  const purified = window.DOMPurify ? window.DOMPurify.sanitize(html) : sanitizeFallback(html);
  node.innerHTML = purified;
}

/**
 * 兜底 HTML 净化：在 DOMPurify 尚未加载完成时的降级方案。
 * 仅作为最后一道防线，正式渲染路径必须走 DOMPurify（规格书 FR-MD-09）。
 */
export function sanitizeFallback(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = String(html ?? '');
  // 移除危险标签
  tpl.content.querySelectorAll('script,iframe,object,embed,link,meta,base,form').forEach((n) => n.remove());
  // 移除事件属性与 javascript: 协议
  tpl.content.querySelectorAll('*').forEach((node) => {
    for (const attr of Array.from(node.attributes)) {
      const an = attr.name.toLowerCase();
      const av = String(attr.value).replace(/[\u0000-\u0020]/g, '').toLowerCase();
      if (an.startsWith('on') || av.startsWith('javascript:') || av.startsWith('data:text/html')) {
        node.removeAttribute(attr.name);
      }
    }
  });
  return tpl.innerHTML;
}

/** HTML 转义（用于把用户输入插入到模板字符串中的场景） */
export function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ------------------------------------------------------------------ *
 * 2. 数值与字符串格式化
 * ------------------------------------------------------------------ */

/** 人类可读的文件体积，1024 进制 */
export function formatBytes(bytes, decimals = 1) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return '—';
  if (n === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  const value = n / 1024 ** i;
  return `${value.toFixed(i === 0 ? 0 : decimals)} ${units[i]}`;
}

/** 秒 → mm:ss（音频时长、任务耗时） */
export function formatDuration(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (x) => String(x).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

/** ISO 时间 → 本地可读时间 */
export function formatTime(input, withSeconds = true) {
  if (!input) return '—';
  const d = input instanceof Date ? input : new Date(input);
  if (Number.isNaN(d.getTime())) return '—';
  const pad = (x) => String(x).padStart(2, '0');
  const base = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return withSeconds ? `${base}:${pad(d.getSeconds())}` : base;
}

/** 相对时间："3 分钟前"、"2 小时后" */
export function formatRelative(input, now = Date.now()) {
  if (!input) return '—';
  const t = (input instanceof Date ? input : new Date(input)).getTime();
  if (Number.isNaN(t)) return '—';
  const diffSec = Math.round((t - now) / 1000);
  const abs = Math.abs(diffSec);
  const suffix = diffSec < 0 ? '前' : '后';
  if (abs < 45) return '刚刚';
  if (abs < 3600) return `${Math.round(abs / 60)} 分钟${suffix}`;
  if (abs < 86400) return `${Math.round(abs / 3600)} 小时${suffix}`;
  if (abs < 2592000) return `${Math.round(abs / 86400)} 天${suffix}`;
  return formatTime(input, false);
}

/** 任务耗时（毫秒或两个时间点） */
export function formatElapsed(startedAt, finishedAt) {
  if (!startedAt) return '—';
  const s = new Date(startedAt).getTime();
  const f = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  if (Number.isNaN(s) || Number.isNaN(f)) return '—';
  const ms = Math.max(0, f - s);
  if (ms < 1000) return `${ms} ms`;
  return formatDuration(ms / 1000);
}

/** 数字千分位 */
export const formatNumber = (n) => Number(n || 0).toLocaleString('zh-CN');

/* ------------------------------------------------------------------ *
 * 3. 文件相关
 * ------------------------------------------------------------------ */

/** 取扩展名（小写，不含点）；无扩展名返回 '' */
export function extOf(name) {
  const idx = String(name ?? '').lastIndexOf('.');
  return idx > 0 ? String(name).slice(idx + 1).toLowerCase() : '';
}

/** 去掉扩展名的基名 */
export function baseName(name) {
  const s = String(name ?? '');
  const idx = s.lastIndexOf('.');
  return idx > 0 ? s.slice(0, idx) : s;
}

/** 触发浏览器下载（同源 URL 或 Blob URL） */
export function triggerDownload(url, filename) {
  const a = el('a', { href: url, download: filename || '', rel: 'noopener' });
  a.style.display = 'none';
  document.body.append(a);
  a.click();
  a.remove();
}

/** 把 Blob 存为本地文件，返回创建的 objectURL（调用方负责 revoke） */
export function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  triggerDownload(url, filename);
  // 延迟回收，确保下载已开始
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  return url;
}

/** 读取 File 为文本（MD 本地预览用，FR-MD-10） */
export function readFileAsText(file, encoding = 'utf-8') {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(new Error(`读取文件失败：${file?.name ?? '未知文件'}`));
    reader.readAsText(file, encoding);
  });
}

/** 读取 File 为 DataURL（图片预览 / 封面） */
export function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(new Error(`读取文件失败：${file?.name ?? '未知文件'}`));
    reader.readAsDataURL(file);
  });
}

/**
 * 魔数嗅探（前端侧轻量版，仅用于即时给出提示）。
 * 正式判定以后端为准（规格书 FR-AU-01：按 magic bytes 而非扩展名）。
 */
export function sniffMagic(bytes) {
  if (!bytes || bytes.length < 12) return null;
  const b = bytes;
  const ascii = (start, len) => String.fromCharCode(...b.slice(start, start + len));
  const hex = (start, len) => Array.from(b.slice(start, start + len))
    .map((x) => x.toString(16).padStart(2, '0')).join('');

  if (ascii(0, 4) === 'fLaC') return { type: 'flac', label: 'FLAC 无损音频' };
  if (ascii(0, 3) === 'ID3') return { type: 'mp3', label: 'MP3（含 ID3 标签）' };
  if (b[0] === 0xff && (b[1] & 0xe0) === 0xe0) return { type: 'mp3', label: 'MP3' };
  if (ascii(0, 4) === 'OggS') return { type: 'ogg', label: 'Ogg 容器' };
  if (ascii(4, 4) === 'ftyp') return { type: 'm4a/mp4', label: 'MP4 / M4A 容器' };
  if (ascii(0, 4) === 'RIFF' && ascii(8, 4) === 'WAVE') return { type: 'wav', label: 'WAV 未压缩音频' };
  if (ascii(0, 4) === 'CTRL') return { type: 'ncm', label: '网易云音乐 .ncm（专有加密）' };
  if (ascii(0, 4) === 'QMcC' || ascii(0, 3) === 'QMC') return { type: 'qmc', label: 'QQ 音乐 .qmc（专有加密）' };
  if (hex(0, 4) === '7c287a4c' || hex(0, 4) === '7c287a4b') return { type: 'kgm', label: '酷狗音乐 .kgm（专有加密）' };
  if (ascii(0, 2) === 'ye' || hex(0, 4) === '79656b65') return { type: 'kwm', label: '酷我音乐 .kwm（专有加密）' };
  if (ascii(0, 4) === '%PDF') return { type: 'pdf', label: 'PDF 文档' };
  if (hex(0, 2) === '504b') return { type: 'zip/ooxml', label: 'ZIP 容器（Office OOXML / 压缩包）' };
  return null;
}

/* ------------------------------------------------------------------ *
 * 4. 函数控制
 * ------------------------------------------------------------------ */

/** 防抖：只在停止触发 wait 毫秒后执行（输入框实时预览用） */
export function debounce(fn, wait = 200) {
  let timer = null;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

/** 节流：每 wait 毫秒最多执行一次（滚动、resize 用） */
export function throttle(fn, wait = 100) {
  let last = 0;
  let timer = null;
  return (...args) => {
    const now = Date.now();
    const remain = wait - (now - last);
    if (remain <= 0) {
      last = now;
      fn(...args);
    } else if (!timer) {
      timer = setTimeout(() => {
        last = Date.now();
        timer = null;
        fn(...args);
      }, remain);
    }
  };
}

/* ------------------------------------------------------------------ *
 * 5. 其它
 * ------------------------------------------------------------------ */

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const clamp = (v, min, max) => Math.min(Math.max(v, min), max);

/** 生成短 ID（DOM id、会话 key；非加密用途） */
export function uid(prefix = 'id') {
  const rand = Math.random().toString(36).slice(2, 8);
  return `${prefix}_${Date.now().toString(36)}${rand}`;
}

/** 随机整数 [min, max] */
export const randInt = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;

/** 复制文本到剪贴板；非安全上下文（http + 非 localhost）下回退到 execCommand */
export async function copyText(text) {
  const str = String(text ?? '');
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(str);
      return true;
    } catch {
      /* 降级到下面的兜底方案 */
    }
  }
  try {
    const ta = el('textarea', { text: str });
    ta.style.cssText = 'position:fixed;top:-9999px;opacity:0';
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

/**
 * 上传校验：返回 { ok, error }
 * 前端先行拦截明显不合规的文件，减少无效请求（后端仍须二次校验，NFR-04）
 */
export function validateFile(file, { maxMb = 100, accept = null } = {}) {
  if (!file) return { ok: false, error: '文件为空' };
  const sizeMb = file.size / 1024 / 1024;
  if (sizeMb > maxMb) {
    return { ok: false, error: `体积 ${formatBytes(file.size)} 超出上限 ${maxMb} MB` };
  }
  if (file.size === 0) return { ok: false, error: '文件大小为 0，可能是空文件' };
  if (accept && accept.length > 0) {
    const ext = extOf(file.name);
    if (!accept.includes(ext)) {
      return { ok: false, error: `不支持的扩展名 .${ext || '(无)'}，允许：${accept.map((e) => '.' + e).join(' / ')}` };
    }
  }
  return { ok: true };
}

/** 从 SHA-256 派生短指纹（与后端 inputs.sha256[0:8] 对齐），仅用于展示 */
export async function shortHash(file) {
  if (!window.crypto?.subtle || !file) return null;
  try {
    const buf = await file.slice(0, Math.min(file.size, 1024 * 1024)).arrayBuffer();
    const digest = await crypto.subtle.digest('SHA-256', buf);
    return Array.from(new Uint8Array(digest)).slice(0, 4)
      .map((b) => b.toString(16).padStart(2, '0')).join('');
  } catch {
    return null;
  }
}

/** 状态 → 中文标签 / 语义变体 */
export const TASK_STATUS = {
  pending:   { label: '排队中', variant: 'pending' },
  running:   { label: '处理中', variant: 'running' },
  success:   { label: '已完成', variant: 'success' },
  failed:    { label: '已失败', variant: 'failed' },
  cancelled: { label: '已取消', variant: 'cancelled' },
};
