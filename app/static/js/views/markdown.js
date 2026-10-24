/**
 * views/markdown.js — 模块四：Markdown 预览
 *
 * 覆盖需求：FR-MD-01 ~ FR-MD-10
 *   - 本地 FileReader 即时解析，文件不上传（FR-MD-10，体验关键）
 *   - GFM、代码高亮 + 一键复制、TOC 跳转与滚动高亮
 *   - KaTeX 公式、Mermaid 图表（均按需懒加载）
 *   - 相对路径图片：可同时选中图片，或上传包含图片的 ZIP 自动解压
 *   - 三种视图：双栏编辑 / 只读预览；明暗主题跟随全局设置
 *   - 导出 PDF（走后端 Playwright）与导出 HTML（前端直接生成）
 *   - HTML 安全过滤由 md-pipeline.js 保证（FR-MD-09）
 */

import { el, clear, $, $$, formatBytes, readFileAsText, saveBlob, debounce } from '../utils.js';
import { icon } from '../icons.js';
import { runtime } from '../config.js';
import { mdApi, api, ApiError, pollTask } from '../api.js';
import { toast, modal, emptyState, tag } from '../ui.js';
import { renderMarkdown, buildToc, buildExportHtml } from '../md-pipeline.js';
import { readZip, extractImages, listEntries, supportsDeflate } from '../zip-reader.js';
import { upsertTask } from '../store.js';

const SAMPLE = `# 欢迎使用全能工具站 · Markdown 预览

这是一个**完全本地运行**的 Markdown 查看器：文件在浏览器内解析，不会上传到任何服务器。

## 支持的能力

- [x] GFM 语法：表格、任务列表、删除线、自动链接
- [x] 代码块语法高亮 + 一键复制
- [x] 自动生成目录，点击跳转并跟随滚动高亮
- [x] 数学公式（KaTeX）
- [x] 流程图与图表（Mermaid）

## 表格

| 模块 | 引擎 | 状态 |
|---|---|---|
| 文档转换 | LibreOffice / Playwright | 可用 |
| SSH 终端 | Paramiko + xterm.js | 可用 |
| 音频转换 | FFmpeg | 可用 |
| Markdown | marked + KaTeX | 可用 |

## 代码

\`\`\`javascript
// 本地解析：不动用任何网络请求
const text = await file.text();
const { headings } = await renderMarkdown(text, previewEl);
console.log('提取到标题数：', headings.length);
\`\`\`

## 数学公式

行内公式：质能方程 $E = mc^2$。

块级公式：

$$
\\int_{-\\infty}^{\\infty} e^{-x^2} \\, dx = \\sqrt{\\pi}
$$

## 图表

\`\`\`mermaid
flowchart LR
    A[上传文件] --> B{格式嗅探}
    B -->|已知格式| C[加入任务队列]
    B -->|未知格式| D[返回不支持]
    C --> E[子进程转换]
    E --> F[写入 outputs]
    F --> G[通知前端可下载]
\`\`\`

## 引用与分隔线

> 所有文件处理都在本机闭环完成，隐私可控。

---

### 小提示

把整个包含图片的文件夹打成 ZIP 一起拖进来，文档中的相对路径图片就能正常显示。
`;

/* ------------------------------------------------------------------ *
 * 视图工厂
 * ------------------------------------------------------------------ */
export default async function createMarkdownView(container) {
  /* ---------------- 状态 ---------------- */
  const state = {
    source: '',            // Markdown 源文本
    fileName: '未命名.md',
    mode: 'split',         // 'split' | 'preview'
    showToc: true,
    assets: null,          // Map<相对路径, objectURL>
    renderedHtml: null,    // 最近一次渲染的 HTML（导出用）
    headings: [],
    features: { hasMath: false, hasMermaid: false, hasCode: false },
    rendering: false,
    exporting: false,
    stats: { chars: 0, words: 0, lines: 0, readMinutes: 0 },
    lastExportTask: null,
    /** 记录上次渲染时的主题，用于 onShow 判断是否需要重渲染（Mermaid/高亮皮肤依赖主题） */
    __lastTheme: document.documentElement.getAttribute('data-theme'),
  };

  let tocController = null;
  let renderSeq = 0;            // 渲染序号：丢弃过期的异步渲染结果
  const disposers = [];

  // 用 classList 增删而非赋值 className：路由依赖容器上的 `is-active` 控制可见性
  container.classList.remove('view--split');
  container.classList.add('view', 'view--single');
  container.innerHTML = '';

  /* ================= 结构 ================= */

  // ---------- 工具栏 ----------
  const modeSegmented = el('div.segmented', { role: 'group', 'aria-label': '视图模式' });
  for (const opt of [
    { id: 'split', label: '双栏' },
    { id: 'preview', label: '仅预览' },
  ]) {
    modeSegmented.append(el('button.segmented-item', {
      type: 'button',
      text: opt.label,
      'aria-pressed': String(state.mode === opt.id),
      on: { click: () => setMode(opt.id) },
    }));
  }

  const tocToggle = el('button.btn.btn--sm.btn--ghost', {
    type: 'button',
    'aria-pressed': 'true',
    on: {
      click: () => {
        state.showToc = !state.showToc;
        tocToggle.setAttribute('aria-pressed', String(state.showToc));
        tocPane.hidden = !state.showToc;
      },
    },
  }, [icon('listTree'), el('span', { text: '目录' })]);

  const fileInput = el('input', {
    type: 'file',
    accept: '.md,.markdown,.mdown,.txt',
    multiple: false,
    style: { display: 'none' },
  });

  const assetInput = el('input', {
    type: 'file',
    accept: 'image/*,.zip',
    multiple: true,
    style: { display: 'none' },
  });

  const statsEl = el('div.row.gap-3.t-xs.t-muted');

  const toolbar = el('div.md-toolbar', {}, [
    modeSegmented,
    tocToggle,
    el('div.grow', {}, [statsEl]),
    el('button.btn.btn--sm.btn--ghost', {
      type: 'button',
      'data-tip': '打开本地 Markdown 文件（也可直接拖拽到页面）',
      on: { click: () => fileInput.click() },
    }, [icon('folder'), el('span', { text: '打开文件' })]),
    el('button.btn.btn--sm.btn--ghost', {
      type: 'button',
      'data-tip': '添加图片资源或包含图片的 ZIP',
      on: { click: () => assetInput.click() },
    }, [icon('image'), el('span', { text: '图片资源' })]),
    el('div.sep-v'),
    el('button.btn.btn--sm', {
      type: 'button',
      'data-tip': '导出为 PDF（复用后端 Chromium 渲染通道）',
      on: { click: () => exportAs('pdf') },
    }, [icon('download'), el('span', { text: '导出 PDF' })]),
    el('button.btn.btn--sm', {
      type: 'button',
      'data-tip': '导出为独立 HTML 文件',
      on: { click: () => exportAs('html') },
    }, [icon('code'), el('span', { text: '导出 HTML' })]),
    fileInput,
    assetInput,
  ]);

  // ---------- 编辑区 ----------
  const editor = el('textarea.md-editor', {
    spellcheck: 'false',
    autocapitalize: 'off',
    autocomplete: 'off',
    'aria-label': 'Markdown 源码编辑区',
    placeholder: '在此粘贴 Markdown 内容，或把 .md 文件拖到页面任意位置…',
  });

  const editorPane = el('div.md-pane.md-pane--editor', {}, [
    el('div.md-toolbar', { style: { borderBottom: '1px solid var(--border-subtle)' } }, [
      el('span.t-xs.t-muted', { text: '源码' }),
      el('div.grow'),
      el('button.btn.btn--sm.btn--icon.btn--ghost', {
        type: 'button',
        'aria-label': '插入示例文档',
        'data-tip': '载入示例',
        on: {
          click: () => {
            editor.value = SAMPLE;
            onSourceChanged();
            toast('info', '已载入示例文档', '包含表格、代码、公式与 Mermaid 图表');
          },
        },
      }, [icon('zap')]),
      el('button.btn.btn--sm.btn--icon.btn--ghost', {
        type: 'button',
        'aria-label': '清空编辑器',
        on: {
          click: () => {
            editor.value = '';
            onSourceChanged();
          },
        },
      }, [icon('trash')]),
    ]),
    editor,
  ]);

  // ---------- 预览区 ----------
  const previewScroll = el('div.panel-body', { style: { padding: '0' } });
  const renderStatus = el('div', {
    role: 'status',
    'aria-live': 'polite',
    style: { padding: '0 var(--sp-4)' },
  });

  const previewPane = el('div.md-pane.md-pane--preview', {}, [
    renderStatus,
    previewScroll,
  ]);

  // ---------- TOC ----------
  const tocList = el('div');
  const tocPane = el('aside.md-toc', { 'aria-label': '文档目录' }, [
    el('div.md-toc-title', { text: '目录' }),
    tocList,
  ]);

  // ---------- 整页拖拽提示层 ----------
  // 不占用常驻布局：仅在拖拽文件经过窗口时淡入，给用户明确的落点反馈
  const dropOverlay = el('div', {
    'aria-hidden': 'true',
    style: {
      position: 'absolute', inset: '0', zIndex: 'var(--z-drawer)',
      display: 'none', placeItems: 'center',
      background: 'color-mix(in srgb, var(--bg-canvas) 82%, transparent)',
      backdropFilter: 'blur(3px)',
      border: '2px dashed var(--accent)', borderRadius: 'var(--r-lg)',
      pointerEvents: 'none',
    },
  }, [
    el('div.col.center.gap-3', {}, [
      el('div.dropzone-icon', { style: { width: '56px', height: '56px' } }, [icon('upload', 'icon icon--xl')]),
      el('div.t-lg.t-bold', { text: '松开即可载入' }),
      el('div.t-sm.t-muted', { text: '支持 .md 文档、图片，以及包含图片与文档的 .zip 压缩包' }),
    ]),
  ]);

  const workbench = el('div.md-workbench', { 'data-mode': state.mode }, [
    toolbar,
    el('div.md-panes', {}, [editorPane, previewPane, tocPane]),
  ]);

  container.style.position = 'relative';
  container.append(workbench, dropOverlay);

  /* ================= 初始化 ================= */
  setMode('split');
  editor.value = SAMPLE;
  onSourceChanged();

  editor.addEventListener('input', debounce(() => onSourceChanged(), 260));

  // ---------- 文件拖拽（整页捕获，无需先点上传区）----------
  const setDropVisible = (on) => { dropOverlay.style.display = on ? 'grid' : 'none'; };

  const onWindowDragOver = (e) => {
    if (!e.dataTransfer?.types?.includes('Files')) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    setDropVisible(true);
  };
  const onWindowDragLeave = (e) => {
    // 拖拽离开窗口时才隐藏（relatedTarget 为 null 表示真正离开了文档）
    if (e.relatedTarget === null) setDropVisible(false);
  };
  const onWindowDrop = (e) => {
    const files = Array.from(e.dataTransfer?.files || []);
    setDropVisible(false);
    if (files.length === 0) return;
    e.preventDefault();
    handleDroppedFiles(files);
  };

  window.addEventListener('dragover', onWindowDragOver);
  window.addEventListener('dragleave', onWindowDragLeave);
  window.addEventListener('drop', onWindowDrop);
  disposers.push(() => {
    window.removeEventListener('dragover', onWindowDragOver);
    window.removeEventListener('dragleave', onWindowDragLeave);
    window.removeEventListener('drop', onWindowDrop);
  });

  // ---------- 编辑器快捷键 ----------
  editor.addEventListener('keydown', (e) => {
    // Tab 插入两个空格而不是切换焦点（编辑 Markdown 列表时的刚需）
    if (e.key === 'Tab') {
      e.preventDefault();
      const { selectionStart: s, selectionEnd: en, value } = editor;
      editor.value = `${value.slice(0, s)}  ${value.slice(en)}`;
      editor.selectionStart = s + 2;
      editor.selectionEnd = s + 2;
      onSourceChanged();
    }
    // Ctrl/Cmd + S 导出 PDF，Ctrl/Cmd + B 加粗
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      exportAs('pdf');
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') {
      e.preventDefault();
      wrapSelection('**');
    }
  });

  // ---------- 预览区快捷键：打印 ----------
  previewScroll.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'p') {
      e.preventDefault();
      window.print();
    }
  });

  /* ================= 视图模式 ================= */
  function setMode(mode) {
    state.mode = mode;
    workbench.dataset.mode = mode;
    for (const btn of $$('.segmented-item', modeSegmented)) {
      const isActive = (mode === 'split' && btn.textContent === '双栏') || (mode === 'preview' && btn.textContent === '仅预览');
      btn.setAttribute('aria-pressed', String(isActive));
    }
    editorPane.hidden = mode === 'preview';
    requestAnimationFrame(() => tocController?.setActive(state.headings[0]?.id));
  }

  /* ================= 编辑器操作 ================= */
  function wrapSelection(marker) {
    const { selectionStart: s, selectionEnd: e, value } = editor;
    const selected = value.slice(s, e);
    const wrapped = `${marker}${selected}${marker}`;
    editor.value = `${value.slice(0, s)}${wrapped}${value.slice(e)}`;
    editor.focus();
    if (selected) {
      editor.selectionStart = s + marker.length;
      editor.selectionEnd = e + marker.length;
    } else {
      editor.selectionStart = s + marker.length;
      editor.selectionEnd = s + marker.length;
    }
    onSourceChanged();
  }

  /* ================= 文件载入 ================= */
  function handleDroppedFiles(files) {
    setDropVisible(false);
    const mdFiles = files.filter((f) => /\.(md|markdown|mdown|txt)$/i.test(f.name));
    const zipFiles = files.filter((f) => /\.zip$/i.test(f.name));
    const imgFiles = files.filter((f) => /^image\//.test(f.type));

    if (mdFiles.length > 0) loadMarkdownFile(mdFiles[0]);
    if (zipFiles.length > 0) loadZipAssets(zipFiles[0]);
    if (imgFiles.length > 0) loadImageAssets(imgFiles);

    if (mdFiles.length === 0 && zipFiles.length === 0 && imgFiles.length === 0) {
      toast('warning', '没有可用的文件', '支持 .md / .markdown / .txt、图片，以及包含图片的 .zip');
    }
  }

  async function loadMarkdownFile(file) {
    try {
      const text = await readFileAsText(file);
      state.fileName = file.name;
      editor.value = text;
      onSourceChanged();
      toast('success', '文件已加载', `${file.name}（${formatBytes(file.size)}），本地解析，未上传`);
      editor.scrollTop = 0;
    } catch (err) {
      toast('danger', '读取文件失败', err?.message || '请确认文件未损坏且有读取权限');
    }
  }

  async function loadImageAssets(files) {
    if (!state.assets) state.assets = new Map();
    for (const file of files) {
      const url = URL.createObjectURL(file);
      state.assets.set(file.name, url);
      state.assets.set(`./${file.name}`, url);
    }
    toast('success', `已加入 ${files.length} 张图片`, '文档中的相对路径引用将优先从这些资源解析');
    onSourceChanged();
  }

  async function loadZipAssets(file) {
    if (!supportsDeflate) {
      toast('warning', '浏览器兼容性提示', '当前浏览器缺少原生解压能力，ZIP 中已压缩的图片可能无法读取');
    }
    const closeOverlay = showBusy('正在解压 ZIP…');

    try {
      const entries = await readZip(file, { maxEntries: 500 });
      const failed = listEntries(entries).filter((e) => e.error);

      const images = extractImages(entries);
      const imageList = [...entries.values()].filter((e) => e.blob && /\.(png|jpe?g|gif|webp|svg|avif)$/i.test(e.name));

      // 释放上一批资源，避免 objectURL 泄漏
      state.assets?.revokeAll?.();
      state.assets = images;

      // ZIP 里也可能直接包含 .md 文件
      const mdEntry = [...entries.values()].find((e) => e.blob && /\.(md|markdown|txt)$/i.test(e.name));
      if (mdEntry) {
        const text = await mdEntry.blob.text();
        state.fileName = mdEntry.name;
        editor.value = text;
        toast('success', '已从 ZIP 载入文档', `${mdEntry.name}，同时解析出 ${imageList.length} 张图片`);
      } else if (imageList.length > 0) {
        toast('success', 'ZIP 解压完成', `解析出 ${imageList.length} 张图片资源`);
      } else {
        toast('warning', 'ZIP 中没有可用资源', '未找到 Markdown 文档或图片文件');
      }

      if (failed.length > 0) {
        toast('warning', `${failed.length} 个条目未能解压`, failed.slice(0, 2).map((f) => `${f.name}：${f.error}`).join('；'));
      }

      onSourceChanged();
    } catch (err) {
      toast('danger', 'ZIP 解析失败', err?.message || '请确认是标准 ZIP 格式');
    } finally {
      closeOverlay();
    }
  }

  fileInput.addEventListener('change', () => {
    const file = fileInput.files?.[0];
    fileInput.value = '';
    if (file) loadMarkdownFile(file);
  });

  assetInput.addEventListener('change', () => {
    const files = Array.from(assetInput.files || []);
    assetInput.value = '';
    const zips = files.filter((f) => /\.zip$/i.test(f.name));
    const imgs = files.filter((f) => /^image\//.test(f.type));
    if (zips.length > 0) loadZipAssets(zips[0]);
    if (imgs.length > 0) loadImageAssets(imgs);
  });

  /* ================= 渲染 ================= */
  function onSourceChanged() {
    state.source = editor.value;
    updateStats();

    const seq = ++renderSeq;
    state.rendering = true;

    renderMarkdown(state.source, previewScroll, {
      assets: state.assets || new Map(),
      onStatus: (status) => {
        clear(renderStatus);
        if (status) {
          renderStatus.append(el('div.alert.alert--info', { style: { marginTop: 'var(--sp-3)' } }, [
            icon('activity'),
            el('div.alert-body', {}, [el('div', { text: status })]),
          ]));
        }
      },
    }).then(({ headings, warnings, features }) => {
      // 渲染是异步的，前一次的结果可能后到 —— 用序号丢弃过期结果
      if (seq !== renderSeq) return;

      state.rendering = false;
      state.headings = headings;
      state.features = features;
      const article = $('.md-body', previewScroll);
      state.renderedHtml = article?.outerHTML || null;

      clear(renderStatus);
      if (warnings.length > 0) {
        renderStatus.append(el('div.alert.alert--warning', { style: { marginTop: 'var(--sp-3)' } }, [
          icon('alertTriangle'),
          el('div.alert-body', {}, [
            el('div.alert-title', { text: `${warnings.length} 项内容渲染降级` }),
            el('ul', { style: { margin: 0, paddingLeft: 'var(--sp-5)' } },
              warnings.slice(0, 4).map((w) => el('li', { text: w }))),
          ]),
        ]));
      }

      // 重建 TOC
      tocController?.destroy();
      tocController = buildToc(headings, tocList, previewScroll);

      renderFeatureBadges();
      updateStats();
    }).catch((err) => {
      if (seq !== renderSeq) return;
      state.rendering = false;
      clear(renderStatus);
      previewScroll.replaceChildren(emptyState({
        iconName: 'alertCircle',
        title: 'Markdown 渲染失败',
        desc: err?.message || '未知错误，请检查内容是否包含异常字符',
      }));
    });
  }

  function renderFeatureBadges() {
    const f = state.features;
    clear(statsEl);
    statsEl.append(
      el('span', { text: `${state.stats.lines} 行 · ${state.stats.chars} 字符 · 约 ${state.stats.readMinutes} 分钟读完` }),
      f.hasCode ? tag('代码高亮') : null,
      f.hasMath ? tag('公式', 'accent') : null,
      f.hasMermaid ? tag('图表', 'accent') : null,
      state.assets?.size ? tag(`图片资源 ${Math.floor(state.assets.size / 2)}`) : null,
      state.source.length === 0 ? tag('空文档', 'warning') : null,
    );
  }

  function updateStats() {
    const text = state.source;
    // 中英混排的粗略字数统计：CJK 每字计 1，拉丁词按空格切分
    const cjk = (text.match(/[\u4e00-\u9fa5\u3040-\u30ff]/g) || []).length;
    const latin = (text.replace(/[\u4e00-\u9fa5\u3040-\u30ff]/g, ' ').match(/[A-Za-z0-9]+/g) || []).length;
    const words = cjk + latin;
    state.stats = {
      chars: text.length,
      words,
      lines: text ? text.split('\n').length : 0,
      // 中文阅读速度约 350 字/分钟，英文约 200 词/分钟，混合取中间值
      readMinutes: Math.max(1, Math.round(words / 300)),
    };
    renderFeatureBadges();
  }

  /* ================= 导出 ================= */
  async function exportAs(format) {
    if (state.exporting) return;

    if (!state.source.trim()) {
      toast('warning', '文档为空', '请先输入或打开一个 Markdown 文件');
      return;
    }

    const article = $('.md-body', previewScroll);
    if (!article) {
      toast('warning', '尚未渲染完成', '请稍候片刻再试');
      return;
    }

    const baseName = state.fileName.replace(/\.(md|markdown|mdown|txt)$/i, '') || 'document';
    const html = buildExportHtml(baseName, article);

    // ---------- HTML 导出：纯前端，立即完成 ----------
    if (format === 'html') {
      try {
        saveBlob(new Blob([html], { type: 'text/html;charset=utf-8' }), `${baseName}.html`);
        toast('success', 'HTML 已导出', '样式与公式已内联，可直接分享或归档');
      } catch (err) {
        toast('danger', '导出失败', err?.message || '未知错误');
      }
      return;
    }

    // ---------- PDF 导出：走后端 Playwright 渲染通道 ----------
    if (runtime.demo) {
      const proceed = await confirmDemoExport();
      if (!proceed) return;
    }

    state.exporting = true;
    const overlay = showBusy('正在生成 PDF…（含公式与图表的文档需要多等待几秒）');

    try {
      const task = await mdApi.export({
        html,
        filename: `${baseName}.md`,
        format: 'pdf',
      });

      upsertTask(task);
      state.lastExportTask = task;

      if (task.status === 'success' && task.output) {
        await api.download(task.output.file_id, task.output.name);
        toast('success', 'PDF 已导出', task.output.name);
        return;
      }

      // 排队中 → 轮询
      const finalTask = await pollTask(task.task_id, {
        onUpdate: (t) => upsertTask(t),
      });

      if (finalTask.status === 'success' && finalTask.output) {
        await api.download(finalTask.output.file_id, finalTask.output.name);
        toast('success', 'PDF 已导出', finalTask.output.name);
      } else {
        toast('danger', 'PDF 导出失败', finalTask.error || '未知原因');
      }
    } catch (err) {
      if (err?.code === 'POLL_TIMEOUT') {
        toast('warning', '导出仍在进行中', err.message, {
          duration: 10000,
          action: { label: '去任务中心', onClick: () => { window.location.hash = '#/tasks'; } },
        });
      } else {
        toast('danger', 'PDF 导出失败', err instanceof ApiError ? err.message : String(err?.message || err));
      }
    } finally {
      state.exporting = false;
      overlay();
    }
  }

  function confirmDemoExport() {
    return new Promise((resolve) => {
      modal({
        title: '演示模式导出',
        body: el('div.col.gap-3', {}, [
          el('div.alert.alert--info', {}, [
            icon('info'),
            el('div.alert-body', {}, [
              el('div.alert-title', { text: '当前未连接 Python 后端' }),
              el('div', {
                text: '真实的 PDF 导出需要后端的 Chromium（Playwright）渲染通道，才能完整保留表格、代码高亮、'
                  + '数学公式与 Mermaid 图表。演示模式将生成一个可打开的占位 PDF，用于验证导出与下载链路。',
              }),
            ]),
          ]),
          el('div.help', { text: '如需真实效果，请改用「导出 HTML」——该路径完全在前端完成，效果与页面所见一致。' }),
        ]),
        actions: [
          { label: '改用导出 HTML', value: 'html' },
          { label: '继续生成占位 PDF', variant: 'primary', value: 'pdf' },
        ],
        onClose: (v) => {
          if (v === 'html') { exportAs('html'); resolve(false); return; }
          resolve(v === 'pdf');
        },
      });
    });
  }

  /* ================= 忙碌遮罩 ================= */
  function showBusy(text) {
    const node = el('div.loading-overlay', { style: { position: 'fixed', zIndex: 'var(--z-modal)' } }, [
      el('div.col.center.gap-3', { style: { background: 'var(--bg-elevated)', padding: 'var(--sp-5) var(--sp-7)', borderRadius: 'var(--r-lg)', boxShadow: 'var(--shadow-3)' } }, [
        el('div', {
          style: {
            width: '26px', height: '26px', border: '3px solid var(--border-default)',
            borderTopColor: 'var(--accent)', borderRadius: '999px',
            animation: 'spin 700ms linear infinite',
          },
        }),
        el('div.t-sm', { text }),
      ]),
    ]);
    document.body.append(node);
    return () => node.remove();
  }

  /* ================= 生命周期 ================= */
  return {
    onShow() {
      // 主题变化会影响 Mermaid / 高亮皮肤，重新渲染一次保证视觉一致
      const currentResolved = document.documentElement.getAttribute('data-theme');
      if (currentResolved !== state.__lastTheme) {
        state.__lastTheme = currentResolved;
        onSourceChanged();
      }
    },
    async unmount() {
      renderSeq += 1;                  // 让进行中的渲染结果失效
      tocController?.destroy();
      state.assets?.revokeAll?.();
      state.assets = null;
      for (const dispose of disposers) {
        try { dispose(); } catch { /* 忽略 */ }
      }
      disposers.length = 0;
    },
  };
}
