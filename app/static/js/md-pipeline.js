/**
 * md-pipeline.js — Markdown → 安全 HTML 渲染管线
 *
 * 处理顺序（顺序不可调换，每一步都依赖前一步的结果）：
 *   1. marked 解析        → 原始 HTML 字符串
 *   2. DOMPurify 净化     → 移除 script / on* 事件 / javascript: 链接（FR-MD-09）
 *   3. 注入 DOM           → 此时才开始触碰真实文档
 *   4. 语言识别与懒加载    → 按需拉取 highlight.js / KaTeX / mermaid
 *   5. 后处理             → 标题锚点、代码块工具条、公式渲染、图表渲染
 *   6. 提取目录           → 返回 headings 供 TOC 使用
 *
 * 关键安全约束：第 2 步必须在第 3 步之前完成。绝不允许把未净化的
 * HTML 直接赋给 innerHTML —— 这是 XSS 最常见的入口。
 */

import { el, clear, escapeHtml } from './utils.js';
import { icon } from './icons.js';
import { createCopyButton } from './ui.js';
import {
  needMarked, needDOMPurify, needHighlight, needKatex, needMermaid,
} from './loader.js';

/* ------------------------------------------------------------------ *
 * 能力探测：决定需要加载哪些重资源
 * ------------------------------------------------------------------ */
export function detectFeatures(markdown) {
  const text = String(markdown || '');
  return {
    // 行内公式 $...$ 或块级公式 $$...$$（避免误判价格写法如 $10 与 $20，要求内部不含空格开头结尾）
    hasMath: /\$\$[\s\S]+?\$\$/.test(text) || /(^|[^\\$])\$[^\s$][^\n$]*?[^\s$]\$/.test(text),
    // ```mermaid 代码围栏（允许前导空白）
    hasMermaid: /^[ \t]*(`{3,}|~{3,})[ \t]*mermaid\b/m.test(text),
    // 任意代码围栏
    hasCode: /^[ \t]*(`{3,}|~{3,})/m.test(text) || /^ {4}\S/m.test(text),
  };
}

/* ------------------------------------------------------------------ *
 * 标题锚点
 * ------------------------------------------------------------------ */
/** 生成 URL 友好的锚点 id，保留中日韩字符（中文标题太多，全转拼音反而不可读） */
export function slugify(text, used = new Set()) {
  let base = String(text || '')
    .trim()
    .toLowerCase()
    .replace(/[\s\u3000]+/g, '-')          // 空白 → 连字符
    .replace(/[^\w\u4e00-\u9fa5\u3040-\u30ff-]/g, '') // 移除标点，保留字母数字下划线中日韩
    .replace(/-{2,}/g, '-')
    .replace(/^-|-$/g, '');

  if (!base) base = 'section';

  let id = base;
  let n = 2;
  while (used.has(id)) {
    id = `${base}-${n}`;
    n += 1;
  }
  used.add(id);
  return id;
}

/* ------------------------------------------------------------------ *
 * 主渲染函数
 * ------------------------------------------------------------------ */
/**
 * @param {string} markdown 源文本
 * @param {HTMLElement} target 挂载容器
 * @param {object} [opts]
 * @param {Map<string,string>} [opts.assets] 相对路径 → objectURL（同目录图片资源）
 * @param {(status:string)=>void} [opts.onStatus] 加载状态回调（"正在加载数学公式引擎…"）
 * @param {boolean} [opts.enableMermaid]
 * @param {boolean} [opts.enableKatex]
 * @returns {Promise<{headings:Array, warnings:string[], features:object}>}
 */
export async function renderMarkdown(markdown, target, opts = {}) {
  const features = detectFeatures(markdown);
  const warnings = [];
  const onStatus = opts.onStatus || (() => {});

  /* ---- 1. 依赖加载 ---- */
  onStatus('正在加载 Markdown 解析器…');
  try {
    await needMarked();
    await needDOMPurify();
  } catch (err) {
    throw new Error(`Markdown 渲染依赖加载失败：${err.message}`);
  }

  /* ---- 2. 解析 ---- */
  const marked = window.marked;
  // marked v12 的 UMD 导出可能是 { marked } 或直接是函数，两种形态都要兼容
  const parseFn = typeof marked === 'function' ? marked : marked?.parse;
  if (typeof parseFn !== 'function') {
    throw new Error('marked 未正确导出 parse 方法，请检查 vendor/marked.umd.js');
  }

  let rawHtml = '';
  try {
    rawHtml = parseFn.call(typeof marked === 'function' ? undefined : marked, String(markdown || ''), {
      gfm: true,             // GFM：表格、任务列表、删除线、自动链接
      breaks: false,         // 不把单个换行当作 <br>（更符合标准 Markdown 直觉）
      pedantic: false,
    });
  } catch (err) {
    throw new Error(`Markdown 解析失败：${err.message}`);
  }

  /* ---- 3. 净化（安全关键步骤） ---- */
  const clean = window.DOMPurify.sanitize(rawHtml, {
    ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|tel|blob|data:image\/):|[^a-z]|[a-z+.-]+(?:[^a-z+.\-:]|$))/i,
    ADD_ATTR: ['id', 'class', 'target', 'rel', 'colspan', 'rowspan', 'align', 'start', 'checked', 'disabled'],
    ADD_TAGS: ['input'],   // GFM 任务列表需要 <input type=checkbox disabled>
    FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed', 'form', 'base', 'link', 'meta'],
    // 允许 style：KaTeX 与 Mermaid 的排版依赖内联样式；DOMPurify 会过滤掉危险 CSS 表达式
    ALLOW_UNKNOWN_PROTOCOLS: false,
  });

  /* ---- 4. 在「惰性文档」中完成全部改写，再一次性注入真实 DOM ----
   *
   * 为什么不在注入后再改写：一旦把 HTML 赋给 innerHTML，浏览器会立即开始
   * 加载其中的 <img src="相对路径">，即使下一步就要把它替换成占位符 ——
   * 这会产生一堆必然 404 的无谓请求（离线环境下还会污染控制台）。
   * DOMParser 生成的文档是**没有浏览上下文的惰性文档**，其中的资源不会被加载，
   * 因此可以安全地先改写、再 importNode 注入。 */
  const inertDoc = new DOMParser().parseFromString(clean, 'text/html');
  const inertRoot = inertDoc.body;

  /* ---- 4.1 图片：相对路径解析 + 加载失败占位 ---- */
  const assets = opts.assets || new Map();
  for (const img of inertRoot.querySelectorAll('img')) {
    const src = img.getAttribute('src') || '';

    if (!/^(https?:|blob:|data:)/i.test(src)) {
      // 相对路径：优先从用户提供的本地资源映射中解析
      let key = src.replace(/^\.\//, '');
      try {
        key = decodeURIComponent(key);
      } catch {
        /* 非法百分号编码，保留原样查找 */
      }
      const resolved = assets.get(key) || assets.get(src);

      if (resolved) {
        img.setAttribute('src', resolved);
      } else {
        // 用占位块替换，明确告知原因并提供解法
        const placeholder = inertDoc.createElement('div');
        placeholder.className = 'md-image-missing';
        placeholder.textContent = `图片无法加载：${src}（相对路径资源未提供。可同时选中图片文件，或把整个文件夹打成 ZIP 一起拖入）`;
        img.replaceWith(placeholder);
        warnings.push(`相对路径图片未解析：${src}`);
        continue;
      }
    }

    // 明确声明懒加载与异步解码，避免长文档一次性解码全部图片阻塞主线程
    if (!img.hasAttribute('loading')) img.setAttribute('loading', 'lazy');
    if (!img.hasAttribute('decoding')) img.setAttribute('decoding', 'async');
  }

  /* ---- 4.2 链接：外链统一新窗口打开并加 noopener（防 tabnabbing） ---- */
  for (const a of inertRoot.querySelectorAll('a')) {
    const href = a.getAttribute('href') || '';
    if (/^https?:/i.test(href)) {
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
    }
  }

  /* ---- 4.3 任务列表：GFM 的复选框必须禁用，否则用户会被误导为可勾选 ---- */
  for (const box of inertRoot.querySelectorAll('input[type="checkbox"]')) {
    box.setAttribute('disabled', '');
    box.setAttribute('aria-hidden', 'true');
  }

  /* ---- 4.4 注入真实 DOM ---- */
  clear(target);
  const article = el('article.md-body');
  for (const node of Array.from(inertRoot.childNodes)) {
    article.append(document.importNode(node, true));
  }
  target.append(article);

  // 图片加载失败兜底（src 可解析但服务器返回错误时）
  for (const img of article.querySelectorAll('img')) {
    img.addEventListener('error', () => {
      img.classList.add('is-broken');
      img.alt = `图片加载失败：${img.getAttribute('src')}`;
    }, { once: true });
  }

  /* ---- 5. 标题锚点 + 目录提取 ---- */
  const headings = [];
  const usedIds = new Set();
  for (const node of article.querySelectorAll('h1, h2, h3, h4, h5, h6')) {
    const level = Number(node.tagName.slice(1));
    const text = node.textContent.trim();
    if (!text) continue;

    let id = node.getAttribute('id');
    if (!id) {
      id = slugify(text, usedIds);
      node.setAttribute('id', id);
    } else {
      usedIds.add(id);
    }
    headings.push({ id, level, text });
  }

  /* ---- 6. 代码块：高亮 + 工具条 ---- */
  const codeBlocks = [...article.querySelectorAll('pre > code')];

  if (codeBlocks.length > 0) {
    // 先分离 mermaid 块（它们不参与语法高亮）
    const mermaidBlocks = [];
    const highlightBlocks = [];

    for (const code of codeBlocks) {
      const cls = code.className || '';
      const isMermaid = /language-mermaid\b/.test(cls);
      if (isMermaid) mermaidBlocks.push(code);
      else highlightBlocks.push(code);
    }

    if (highlightBlocks.length > 0) {
      await enhanceCodeBlocks(article, highlightBlocks, onStatus);
    }
    if (mermaidBlocks.length > 0) {
      await renderMermaidBlocks(article, mermaidBlocks, warnings, onStatus, opts.enableMermaid !== false);
    }
  }

  /* ---- 7. 数学公式 ---- */
  if (features.hasMath && opts.enableKatex !== false) {
    try {
      onStatus('正在加载数学公式引擎…');
      const renderMath = await needKatex();
      renderMath(article, {
        delimiters: [
          { left: '$$', right: '$$', display: true },
          { left: '$', right: '$', display: false },
          { left: '\\[', right: '\\]', display: true },
          { left: '\\(', right: '\\)', display: false },
        ],
        // 公式渲染失败时不要抛错中断整篇渲染，只记录警告
        throwOnError: false,
        errorColor: 'var(--danger)',
        ignoredClasses: ['no-math'],
      });
    } catch (err) {
      warnings.push(`数学公式渲染失败：${err.message}`);
    }
  }

  onStatus('');
  return { headings, warnings, features };
}

/* ------------------------------------------------------------------ *
 * 代码块增强
 * ------------------------------------------------------------------ */
async function enhanceCodeBlocks(article, codeElements, onStatus) {
  let hljs = null;
  try {
    onStatus('正在加载代码高亮…');
    const theme = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
    hljs = await needHighlight(theme);
  } catch (err) {
    // 高亮失败不应阻断渲染：降级为无高亮的纯代码块
    console.warn('[md-pipeline] 代码高亮不可用：', err.message);
  }

  for (const code of codeElements) {
    const pre = code.parentElement; // <pre>
    const langClass = [...code.classList].find((c) => c.startsWith('language-'));
    const lang = langClass ? langClass.replace('language-', '') : '';

    if (hljs) {
      try {
        // 未知语言时 hljs.highlightElement 会抛错，这里先判断语言是否已注册
        if (lang && !hljs.getLanguage(lang)) {
          code.classList.add('hljs');
        } else {
          hljs.highlightElement(code);
        }
      } catch (err) {
        console.warn('[md-pipeline] 高亮失败：', lang, err.message);
      }
    }

    // 用带工具条的容器包裹原有 <pre>（DOM 结构：div.code-block > header + pre）
    const wrapper = el('div.code-block');
    const wrapLabel = el('span', { text: '换行' });
    const header = el('div.code-block-header', {}, [
      el('span', { text: lang || '纯文本' }),
      el('div.row.gap-2', {}, [
        el('button.copy-btn', {
          type: 'button',
          'aria-label': '切换代码块自动换行',
          'aria-pressed': 'false',
          on: {
            click: (e) => {
              const isWrapped = wrapper.classList.toggle('is-wrapped');
              e.currentTarget.setAttribute('aria-pressed', String(isWrapped));
              wrapLabel.textContent = isWrapped ? '不换行' : '换行';
            },
          },
        }, [icon('menu'), wrapLabel]),
        createCopyButton(() => code.textContent),
      ]),
    ]);

    pre.replaceWith(wrapper);
    wrapper.append(header, pre);
  }
}

/* ------------------------------------------------------------------ *
 * Mermaid 图表
 * ------------------------------------------------------------------ */
async function renderMermaidBlocks(article, mermaidCodes, warnings, onStatus, enabled) {
  for (const code of mermaidCodes) {
    const pre = code.parentElement;
    const source = code.textContent || '';

    if (!enabled) {
      pre.dataset.mermaidDisabled = 'true';
      continue;
    }

    const container = el('div.mermaid-wrap', { role: 'img', 'aria-label': 'Mermaid 图表' });
    const placeholder = el('div.t-sm.t-muted', { text: '正在渲染图表…' });
    container.append(placeholder);
    pre.replaceWith(container);

    try {
      onStatus('正在加载图表引擎…');
      const mermaid = await needMermaid();

      // 每次渲染前按当前主题初始化；mermaid 只允许初始化一次，重复调用会 warn
      const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',   // 禁止图表中的 HTML 与脚本，防止 XSS
        theme: isDark ? 'dark' : 'default',
        fontFamily: 'var(--font-sans)',
        suppressErrorRendering: true,
      });

      // 用 mermaid.render 手动渲染，避免 mermaid.run 对整个文档做全量扫描
      const renderId = `mermaid-${Math.random().toString(36).slice(2, 10)}`;
      const { svg } = await mermaid.render(renderId, source);
      placeholder.remove();
      container.innerHTML = svg;
    } catch (err) {
      // 图表语法错误很常见，降级为展示源码 + 错误说明，而不是让整篇文档渲染失败
      placeholder.remove();
      container.replaceChildren(el('div.mermaid-error', {
        text: `图表渲染失败：${err?.message || '语法错误'}\n\n原始代码：\n${source}`,
      }));
      warnings.push(`Mermaid 图表渲染失败：${String(err?.message || '').split('\n')[0]}`);
    }
  }
}

/* ------------------------------------------------------------------ *
 * 目录（TOC）构建与滚动高亮
 * ------------------------------------------------------------------ */
/**
 * 构建 TOC 列表。
 * @param {Array<{id,level,text}>} headings
 * @param {HTMLElement} listEl 列表容器
 * @param {HTMLElement} scrollRoot 滚动容器（用于滚动高亮）
 * @returns {{destroy:() => void, setActive:(id:string)=>void}}
 */
export function buildToc(headings, listEl, scrollRoot) {
  clear(listEl);
  listEl.classList.add('md-toc-list');

  if (headings.length === 0) {
    listEl.append(el('div.help', { text: '文档中没有标题' }));
    return { destroy() {}, setActive() {} };
  }

  const links = new Map();
  const minLevel = Math.min(...headings.map((h) => h.level));

  /**
   * 把标题滚到预览区顶部。
   *
   * 刻意**不用 scrollIntoView**：它会连带滚动所有可滚祖先（包括 document
   * 的滚动容器），在外壳 overflow:hidden 的布局下会把整页顶下去，而用户
   * 又没有任何滚动条能把页面拉回来。只改预览容器自己的 scrollTop，
   * 滚动范围才完全可控。
   * @param {HTMLElement} headingEl 目标标题
   */
  function scrollHeadingIntoView(headingEl) {
    const delta = headingEl.getBoundingClientRect().top - scrollRoot.getBoundingClientRect().top;
    // 留 8px 呼吸空间，避免标题紧贴容器上沿被裁掉半个字
    const target = Math.max(0, Math.round(scrollRoot.scrollTop + delta - 8));
    if (typeof scrollRoot.scrollTo === 'function') {
      scrollRoot.scrollTo({ top: target, behavior: 'smooth' });
    } else {
      scrollRoot.scrollTop = target;
    }
  }

  for (const h of headings) {
    const a = el('a.md-toc-link', {
      href: `#${encodeURIComponent(h.id)}`,
      'data-level': String(h.level),
      title: h.text,
      text: h.text,
    }, []);
    // 相对层级缩进，避免 h3 起首的文档出现无谓的深缩进
    a.dataset.level = String(Math.min(6, h.level - minLevel + 2));

    a.addEventListener('click', (e) => {
      e.preventDefault();
      const headingEl = document.getElementById(h.id);
      if (!headingEl) return;
      // 只滚预览容器：见 scrollHeadingIntoView 的说明（scrollIntoView 会带动整页）
      scrollHeadingIntoView(headingEl);
      setActive(h.id);
      // 把焦点交给标题，方便键盘用户继续 Tab 阅读（标题需可聚焦）
      headingEl.setAttribute('tabindex', '-1');
      headingEl.focus({ preventScroll: true });
    });

    links.set(h.id, a);
    listEl.append(a);
  }

  let activeId = headings[0].id;

  function setActive(id) {
    if (activeId === id) return;
    links.get(activeId)?.classList.remove('is-active');
    activeId = id;
    const link = links.get(id);
    if (link) {
      link.classList.add('is-active');
      // 目录跟随高亮：滚的必须是 **TOC 侧栏自己**。
      // 旧实现拿 TOC 链接的 offsetTop 去改预览区的 scrollTop（坐标系错位）：
      // 链接偏移量（几十到上千 px）永远小于正文滚动位置，条件恒成立，
      // 于是每次滚动高亮回调都把预览拽回 linkTop-clientHeight/2，
      // 表现为「点了目录整屏跳动」且「怎么滚都被拽回去」。
      const tocScroller = listEl.closest('.md-toc') || listEl.parentElement;
      if (tocScroller && tocScroller.clientHeight > 0) {
        const linkTop = link.offsetTop;
        const viewTop = tocScroller.scrollTop;
        const viewBottom = viewTop + tocScroller.clientHeight;
        if (linkTop < viewTop + 40 || linkTop > viewBottom - 60) {
          tocScroller.scrollTop = Math.max(0, linkTop - tocScroller.clientHeight / 2);
        }
      }
    }
  }

  // 滚动高亮：用 IntersectionObserver 比 scroll 事件监听更省性能
  const observer = new IntersectionObserver((entries) => {
    // 取当前视口内最靠上的标题为活动项
    const visible = entries
      .filter((e) => e.isIntersecting)
      .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
    if (visible.length > 0) setActive(visible[0].target.id);
  }, {
    root: scrollRoot,
    rootMargin: '-8% 0px -70% 0px',
    threshold: 0,
  });

  for (const h of headings) {
    const node = document.getElementById(h.id);
    if (node) observer.observe(node);
  }

  // 首屏默认高亮第一个标题
  links.get(activeId)?.classList.add('is-active');

  return {
    setActive,
    destroy() { observer.disconnect(); },
  };
}

/* ------------------------------------------------------------------ *
 * 导出用：构造独立完整 HTML（供后端 Playwright 渲染为 PDF）
 * ------------------------------------------------------------------ */
/**
 * 生成自包含的 HTML 文档。所有样式内联，便于后端无头浏览器直接渲染。
 * 注意：KaTeX/Mermaid 已在页面上渲染完成，这里直接抓取渲染后的 DOM，
 * 因此导出结果与页面所见完全一致（含公式与图表）。
 * @param {string} title
 * @param {HTMLElement} renderedArticle 已渲染完成的 .md-body 元素
 * @returns {string}
 */
export function buildExportHtml(title, renderedArticle) {
  const cloned = renderedArticle.cloneNode(true);

  // 移除交互件（复制按钮、换行开关），它们对静态输出无意义
  cloned.querySelectorAll('.code-block-header .copy-btn').forEach((n) => n.remove());
  cloned.querySelectorAll('.code-block-header').forEach((n) => n.remove());
  cloned.querySelectorAll('audio, video').forEach((n) => n.remove());

  // 内联样式：从当前页面收集计算后的关键字号/行高，保证导出样式接近
  const styles = `
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 40px 48px; background: #fff; color: #111827;
      font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      font-size: 16px; line-height: 1.75; max-width: 820px; margin: 0 auto;
    }
    h1,h2,h3,h4,h5,h6 { line-height: 1.25; font-weight: 600; margin: 1.6em 0 .6em; }
    h1 { font-size: 1.9em; border-bottom: 1px solid #e5e7eb; padding-bottom: .3em; }
    h2 { font-size: 1.45em; border-bottom: 1px solid #e5e7eb; padding-bottom: .25em; }
    h3 { font-size: 1.2em; }
    p { margin: 1em 0; }
    a { color: #2563eb; }
    ul, ol { padding-left: 1.8em; }
    li { margin: .25em 0; }
    blockquote { margin: 1em 0; padding: .2em 1.2em; border-left: 3px solid #d1d5db; color: #4b5563; }
    code { font-family: ui-monospace, Consolas, monospace; font-size: .9em;
           background: #f6f8fb; border: 1px solid #e5e7eb; border-radius: 4px; padding: .15em .4em; }
    pre { overflow: auto; background: #f6f8fb; border: 1px solid #e5e7eb; border-radius: 8px;
          padding: 16px; font-size: .875em; line-height: 1.6; }
    pre code { background: none; border: 0; padding: 0; }
    table { border-collapse: collapse; width: 100%; margin: 1.2em 0; font-size: .95em; }
    th, td { border: 1px solid #d1d5db; padding: 8px 12px; text-align: left; }
    th { background: #f3f4f6; font-weight: 600; }
    img { max-width: 100%; height: auto; }
    hr { border: 0; border-top: 1px solid #e5e7eb; margin: 2em 0; }
    .mermaid-wrap { text-align: center; margin: 1.4em 0; }
    .mermaid-wrap svg { max-width: 100%; height: auto; }
    .katex-display { overflow-x: auto; }
    li.task-list-item { list-style: none; margin-left: -1.5em; }
    @page { size: A4; margin: 16mm; }
    @media print { body { padding: 0; max-width: none; } pre, table { break-inside: avoid; } }
  `;

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>${styles}</style>
</head>
<body>
${cloned.outerHTML}
</body>
</html>`;
}
