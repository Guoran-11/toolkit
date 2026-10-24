/**
 * ui.js — 通用 UI 组件工厂
 *
 * 所有组件遵循三条无障碍基线：
 * 1. 弹窗打开时焦点移入、关闭时归还给触发元素（焦点不得丢失到 body）
 * 2. 动态变化通过 aria-live 区域播报，读屏用户能感知
 * 3. 键盘可完成全部操作（拖拽上传有对应点选按钮，分隔条可用方向键调整）
 */

import { el, $, $$, clear, formatBytes, extOf, clamp, debounce, copyText, uid } from './utils.js';
import { icon } from './icons.js';

/* ================================================================== *
 * 1. Toast 通知
 * ================================================================== */
let toastRegion = null;

function ensureToastRegion() {
  if (!toastRegion) {
    toastRegion = $('#toast-region');
  }
  if (!toastRegion) {
    toastRegion = el('div.toast-region#toast-region', {
      role: 'status',
      'aria-live': 'polite',
      'aria-atomic': 'false',
    });
    document.body.append(toastRegion);
  }
  return toastRegion;
}

const TOAST_ICONS = {
  info: 'info',
  success: 'checkCircle',
  warning: 'alertTriangle',
  danger: 'alertCircle',
};

/**
 * 弹出通知。
 * @param {'info'|'success'|'warning'|'danger'} type
 * @param {string} title 主标题（简短）
 * @param {string} [message] 详细说明
 * @param {{duration?:number, action?:{label:string, onClick:Function}}} [opts]
 */
export function toast(type, title, message = '', opts = {}) {
  const region = ensureToastRegion();
  const duration = opts.duration ?? (type === 'danger' ? 8000 : 4200);

  const node = el(`div.toast.toast--${type}`, { role: type === 'danger' ? 'alert' : undefined }, [
    icon(TOAST_ICONS[type] || 'info', 'icon toast-icon'),
    el('div.toast-content', {}, [
      el('div.toast-title', { text: title }),
      message ? el('div.toast-msg', { text: message }) : null,
    ]),
  ]);

  if (opts.action?.label) {
    node.append(el('button.btn.btn--sm.btn--ghost', {
      type: 'button',
      text: opts.action.label,
      on: { click: () => { opts.action.onClick?.(); dismiss(); } },
    }));
  }

  node.append(el('button.btn.btn--sm.btn--icon.btn--ghost.toast-close', {
    type: 'button',
    'aria-label': '关闭通知',
    on: { click: () => dismiss() },
  }, [icon('close')]));

  let timer = null;
  let closed = false;
  function dismiss() {
    if (closed) return;
    closed = true;
    clearTimeout(timer);
    node.classList.add('is-leaving');
    node.addEventListener('animationend', () => node.remove(), { once: true });
    // 兜底：动画被 reduced-motion 禁用时 animationend 可能不触发
    setTimeout(() => node.remove(), 400);
  }

  region.append(node);
  if (duration > 0) timer = setTimeout(dismiss, duration);

  // 指针悬停时暂停自动关闭，方便用户阅读长错误信息
  node.addEventListener('mouseenter', () => clearTimeout(timer));
  node.addEventListener('mouseleave', () => { if (!closed && duration > 0) timer = setTimeout(dismiss, 1500); });

  return dismiss;
}

/* ================================================================== *
 * 2. 弹窗（Modal / Confirm / Prompt）
 * ================================================================== */

/** 记录弹窗打开前的焦点元素，关闭后归还 */
let lastFocused = null;
let openModals = 0;

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

function trapFocus(container, event) {
  const items = $$(FOCUSABLE, container).filter((n) => n.offsetParent !== null);
  if (items.length === 0) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/**
 * 打开弹窗。
 * @param {object} cfg
 * @param {string} cfg.title
 * @param {Node|Node[]|string} cfg.body
 * @param {Array<{label:string, variant?:string, value?:any, autofocus?:boolean, closeOnClick?:boolean, onClick?:Function}>} cfg.actions
 * @param {boolean} [cfg.dismissible] 是否允许 ESC / 点击遮罩关闭
 * @param {'normal'|'wide'} [cfg.size]
 * @param {(value:any)=>void} [cfg.onClose]
 * @returns {{close:(value?:any)=>void, root:HTMLElement, body:HTMLElement}}
 */
export function modal(cfg) {
  const dismissible = cfg.dismissible !== false;
  const bodyNode = el('div.modal-body');

  if (typeof cfg.body === 'string') bodyNode.innerHTML = cfg.body;
  else if (cfg.body) for (const n of [].concat(cfg.body)) if (n) bodyNode.append(n);

  const modalNode = el(`div.modal${cfg.size === 'wide' ? '.modal--wide' : ''}`, {
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': 'modal-title',
  }, [
    el('div.modal-header', {}, [
      el('h2.modal-title#modal-title', { text: cfg.title }),
      dismissible
        ? el('button.btn.btn--sm.btn--icon.btn--ghost', {
            type: 'button', 'aria-label': '关闭', on: { click: () => close(null) },
          }, [icon('close')])
        : null,
    ]),
    bodyNode,
    cfg.actions?.length
      ? el('div.modal-footer', {}, cfg.actions.map((action) => el(
          `button.btn.btn--${action.variant || 'secondary'}`,
          {
            type: 'button',
            text: action.label,
            'data-autofocus': action.autofocus ? '' : null,
            on: {
              click: () => {
                const result = action.onClick ? action.onClick() : action.value;
                if (action.closeOnClick !== false) close(result === undefined ? action.value : result);
              },
            },
          },
        )))
      : null,
  ]);

  const scrim = el('div.scrim', { 'data-scrim': '' }, [modalNode]);
  const previousFocus = document.activeElement;

  let closed = false;
  function close(value) {
    if (closed) return;
    closed = true;
    openModals = Math.max(0, openModals - 1);
    // 归还焦点：优先还给触发元素，若已从 DOM 移除则退回 body
    if (previousFocus && document.contains(previousFocus)) previousFocus.focus();
    else if (lastFocused && document.contains(lastFocused)) lastFocused.focus();
    scrim.remove();
    if (openModals === 0) document.body.style.removeProperty('overflow');
    cfg.onClose?.(value);
  }

  // 遮罩点击关闭（仅当点击的是遮罩本身，而非弹窗内部）
  if (dismissible) {
    scrim.addEventListener('mousedown', (e) => {
      if (e.target === scrim) close(null);
    });
  }

  scrim.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && dismissible) {
      e.stopPropagation();
      close(null);
      return;
    }
    if (e.key === 'Tab') trapFocus(modalNode, e);
  });

  lastFocused = previousFocus;
  openModals += 1;
  document.body.style.overflow = 'hidden';
  document.body.append(scrim);

  // 焦点移入：优先 autofocus 标记的元素，否则第一个可聚焦项
  requestAnimationFrame(() => {
    const target = $('[data-autofocus]', modalNode) || $$(FOCUSABLE, modalNode)[0] || modalNode;
    target.focus?.();
  });

  return { close, root: scrim, body: bodyNode };
}

/**
 * 确认对话框。危险的破坏性操作必须用 danger 变体（视觉警示 + 默认焦点在取消按钮）。
 * @returns {Promise<boolean>}
 */
export function confirmDialog({
  title = '确认操作',
  message = '',
  confirmLabel = '确认',
  cancelLabel = '取消',
  danger = false,
  details = null,
} = {}) {
  return new Promise((resolve) => {
    const body = el('div.col.gap-3');
    if (message) body.append(el('p', { text: message }));
    if (details) {
      body.append(el('div.alert.alert--warning', {}, [
        icon('alertTriangle'),
        el('div.alert-body', {}, [el('div', { text: details })]),
      ]));
    }

    modal({
      title,
      body,
      actions: [
        { label: cancelLabel, value: false, autofocus: danger, closeOnClick: true },
        { label: confirmLabel, variant: danger ? 'danger' : 'primary', value: true },
      ],
      onClose: (v) => resolve(Boolean(v)),
    });
  });
}

/**
 * 输入对话框（重命名、新建连接等）。
 * @returns {Promise<string|null>}
 */
export function promptDialog({
  title = '请输入',
  label = '',
  value = '',
  placeholder = '',
  multiline = false,
  confirmLabel = '确定',
} = {}) {
  return new Promise((resolve) => {
    const inputId = uid('prompt');
    const input = multiline
      ? el('textarea.textarea', { id: inputId, placeholder, rows: 4 })
      : el('input.input', { id: inputId, type: 'text', placeholder });
    input.value = value;

    const body = el('div.field', {}, [
      label ? el('label.label', { for: inputId, text: label }) : null,
      input,
    ]);

    const dlg = modal({
      title,
      body,
      actions: [
        { label: '取消', value: null },
        { label: confirmLabel, variant: 'primary', onClick: () => input.value },
      ],
      onClose: (v) => resolve(v ?? null),
    });

    input.focus();
    input.select?.();
    // Enter 提交（多行输入除外，避免与换行冲突）
    if (!multiline) {
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          dlg.close(input.value);
        }
      });
    }
  });
}

/* ================================================================== *
 * 3. 状态胶囊 / 徽标
 * ================================================================== */

/** 任务状态胶囊 */
export function statusPill(status, labelOverride) {
  const map = {
    pending:   ['pending', '排队中'],
    running:   ['running', '处理中'],
    success:   ['success', '已完成'],
    failed:    ['failed', '已失败'],
    cancelled: ['cancelled', '已取消'],
  };
  const [variant, label] = map[status] || ['pending', status || '未知'];
  return el(`span.status.status--${variant}`, { text: labelOverride || label });
}

/** 质量等级徽标（A/B/C/X）
 *  aria-hidden：等级已由相邻标题文本（如"预期质量：A 类"）播报，
 *  徽标本身只是视觉强调，重复播报会让读屏用户听到"A 类 A"。 */
export function gradeBadge(grade, title) {
  const g = String(grade || 'x').toLowerCase();
  return el(`span.grade.grade--${g}`, { text: g.toUpperCase(), title: title || '', 'aria-hidden': 'true' });
}

/** 通用标签 */
export function tag(text, variant = '', extra = {}) {
  return el(`span.tag${variant ? `.tag--${variant}` : ''}`, { text, ...extra });
}

/* ================================================================== *
 * 4. 空状态 / 加载态
 * ================================================================== */
export function emptyState({ iconName = 'file', title = '暂无内容', desc = '', action = null } = {}) {
  return el('div.empty', {}, [
    el('div.empty-icon', {}, [icon(iconName, 'icon icon--xl')]),
    el('div.empty-title', { text: title }),
    desc ? el('div.empty-desc', { text: desc }) : null,
    action,
  ]);
}

export function skeletonLines(count = 3) {
  const wrap = el('div');
  for (let i = 0; i < count; i += 1) {
    wrap.append(el('div.skeleton.skeleton-line', {
      style: { width: i === count - 1 ? '62%' : '100%' },
    }));
  }
  return wrap;
}

/* ================================================================== *
 * 5. 上传区（Dropzone）
 * ================================================================== */

/**
 * 挂载拖拽上传区。键盘用户可通过内部隐藏 input 点选，等价于拖拽。
 * @param {object} cfg
 * @param {string} cfg.title
 * @param {string} cfg.hint
 * @param {string} [cfg.iconName]
 * @param {string[]} [cfg.accept]    允许的扩展名（不带点）
 * @param {boolean} [cfg.multiple]
 * @param {string} [cfg.inputAccept] input accept 属性（含点，如 '.pdf,.docx'）
 * @param {(files: File[]) => void} cfg.onFiles
 * @param {() => boolean} [cfg.isDisabled]
 */
export function createDropzone(cfg) {
  const inputId = uid('file');
  const input = el('input', {
    id: inputId,
    type: 'file',
    multiple: cfg.multiple !== false,
    accept: cfg.inputAccept || undefined,
    tabindex: '-1',
    // 用 hidden 而非「透明 1px」：hidden 会让输入框彻底退出无障碍树，
    // 避免读屏用户遇到一个没有标签的重复控件；程序化 .click() 在现代浏览器中依然有效。
    hidden: true,
  });

  const zone = el('div.dropzone', {
    role: 'button',
    tabindex: '0',
    'aria-describedby': `${inputId}-hint`,
    on: { click: () => input.click() },
  }, [
    input,
    el('div.dropzone-icon', {}, [icon(cfg.iconName || 'upload', 'icon icon--lg')]),
    el('div.dropzone-title', { text: cfg.title || '拖拽文件到此处，或点击选择' }),
    el('div.dropzone-hint', { id: `${inputId}-hint`, text: cfg.hint || '' }),
  ]);

  // 键盘等价操作：回车 / 空格触发文件选择
  zone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      input.click();
    }
  });

  input.addEventListener('change', () => {
    const files = Array.from(input.files || []);
    // 立即清空 value，保证同名文件可重复选择（否则第二次 change 不触发）
    input.value = '';
    if (files.length > 0) cfg.onFiles?.(files);
  });

  // 拖拽：拖入计数法解决"经过子元素触发 dragleave"导致的闪烁
  let dragDepth = 0;
  const setDragging = (on) => zone.classList.toggle('is-dragover', on);

  zone.addEventListener('dragenter', (e) => {
    e.preventDefault();
    if (cfg.isDisabled?.()) return;
    dragDepth += 1;
    setDragging(true);
  });
  zone.addEventListener('dragover', (e) => {
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  });
  zone.addEventListener('dragleave', () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) setDragging(false);
  });
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    dragDepth = 0;
    setDragging(false);
    if (cfg.isDisabled?.()) return;
    const files = Array.from(e.dataTransfer?.files || []);
    if (files.length > 0) cfg.onFiles?.(files);
  });

  // 防止拖到页面其它位置时浏览器直接打开文件（丢失用户操作）
  const preventWindowDrop = (e) => e.preventDefault();
  window.addEventListener('dragover', preventWindowDrop);
  window.addEventListener('drop', preventWindowDrop);

  zone.setDisabled = (disabled) => zone.classList.toggle('is-disabled', Boolean(disabled));
  zone.inputEl = input;
  return zone;
}

/**
 * 渲染文件清单条目。
 * @param {File} file
 * @param {{status?:'pending'|'ok'|'error', message?:string, onRemove?:Function}} [state]
 */
export function fileItem(file, state = {}) {
  const ext = extOf(file.name) || '?';
  const node = el(`div.file-item${state.status === 'error' ? '.is-error' : state.status === 'ok' ? '.is-ok' : ''}`, {}, [
    el('div.file-icon', { text: ext.slice(0, 4) }),
    el('div.file-meta', {}, [
      el('div.file-name.t-clamp-1', { text: file.name, title: file.name }),
      el('div.file-sub', {}, [
        el('span', { text: formatBytes(file.size) }),
        state.message ? el('span.t-danger', { text: `· ${state.message}` }) : null,
      ]),
    ]),
    state.onRemove
      ? el('button.btn.btn--sm.btn--icon.btn--ghost', {
          type: 'button', 'aria-label': `移除 ${file.name}`, on: { click: state.onRemove },
        }, [icon('close')])
      : null,
  ]);
  return node;
}

/* ================================================================== *
 * 6. 进度条
 * ================================================================== */
export function createProgress({ size = '' } = {}) {
  const bar = el(`div.progress${size ? `.progress--${size}` : ''}`, {
    role: 'progressbar',
    'aria-valuemin': '0',
    'aria-valuemax': '100',
    'aria-valuenow': '0',
  }, [el('div.progress-bar')]);

  bar.setValue = (percent, stateName) => {
    const p = clamp(Math.round(Number(percent) || 0), 0, 100);
    bar.classList.remove('is-indeterminate');
    bar.firstChild.style.width = `${p}%`;
    bar.setAttribute('aria-valuenow', String(p));
    if (stateName) bar.firstChild.dataset.state = stateName;
  };

  /** 无确定进度时使用（如 SSH 连接中、引擎启动中） */
  bar.setIndeterminate = () => {
    bar.classList.add('is-indeterminate');
    bar.removeAttribute('aria-valuenow');
  };

  bar.setLabel = (text) => bar.setAttribute('aria-label', text);

  return bar;
}

/* ================================================================== *
 * 7. 可拖拽分隔条（SplitPane）
 * ================================================================== */

/**
 * 让分隔条可拖动调整左面板宽度。
 * 键盘支持：左右方向键 ±16px，Home/End 到极值（无障碍要求）。
 * @param {HTMLElement} splitter
 * @param {HTMLElement} view       .view--split 容器
 * @param {{min?:number, max?:number, initial?:number, onChange?:(w:number)=>void}} [opts]
 */
export function attachSplitter(splitter, view, opts = {}) {
  const min = opts.min ?? 320;
  const max = opts.max ?? 620;

  const apply = (px) => {
    const w = clamp(px, min, max);
    view.style.setProperty('--panel-w', `${w}px`);
    splitter.setAttribute('aria-valuenow', String(w));
    opts.onChange?.(w);
    return w;
  };

  if (opts.initial) apply(opts.initial);

  let dragging = false;
  let startX = 0;
  let startW = 0;

  const onMove = (e) => {
    if (!dragging) return;
    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    apply(startW + (clientX - startX));
  };

  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove('is-dragging');
    document.body.style.removeProperty('cursor');
    document.body.style.removeProperty('user-select');
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    window.removeEventListener('pointercancel', onUp);
  };

  splitter.addEventListener('pointerdown', (e) => {
    // 窄屏下布局变为上下堆叠，此时分隔条改为行方向（由 CSS 处理），禁用指针拖拽
    if (window.matchMedia('(max-width: 1024px)').matches) return;
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    startW = splitter.previousElementSibling?.getBoundingClientRect().width
      ?? parseInt(getComputedStyle(view).getPropertyValue('--panel-w'), 10) ?? 400;
    splitter.classList.add('is-dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
  });

  splitter.addEventListener('keydown', (e) => {
    const current = parseInt(splitter.getAttribute('aria-valuenow') || '400', 10);
    let next = null;
    if (e.key === 'ArrowLeft') next = current - 16;
    else if (e.key === 'ArrowRight') next = current + 16;
    else if (e.key === 'Home') next = min;
    else if (e.key === 'End') next = max;
    if (next === null) return;
    e.preventDefault();
    apply(next);
    opts.onChange?.(clamp(next, min, max));
  });

  return { apply, get width() { return parseInt(splitter.getAttribute('aria-valuenow') || '400', 10); } };
}

/* ================================================================== *
 * 8. 复制按钮（代码块用）
 * ================================================================== */
export function createCopyButton(getText, { label = '复制' } = {}) {
  const btn = el('button.copy-btn', { type: 'button', 'aria-label': `复制${label}` }, [
    icon('copy', 'icon'),
    el('span', { text: label }),
  ]);

  let resetTimer = null;
  btn.addEventListener('click', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    const ok = await copyText(typeof getText === 'function' ? getText() : getText);
    clear(btn);
    btn.classList.toggle('is-copied', ok);
    btn.append(icon(ok ? 'check' : 'alertCircle', 'icon'), el('span', { text: ok ? '已复制' : '复制失败' }));
    clearTimeout(resetTimer);
    resetTimer = setTimeout(() => {
      clear(btn);
      btn.classList.remove('is-copied');
      btn.append(icon('copy', 'icon'), el('span', { text: label }));
    }, 2000);
  });

  return btn;
}

/* ================================================================== *
 * 9. 引擎状态指示（顶栏）
 * ================================================================== */
export const ENGINE_LABELS = {
  libreoffice: { name: 'LibreOffice', short: 'LO' },
  chromium: { name: 'Chromium', short: 'CR' },
  ffmpeg: { name: 'FFmpeg', short: 'FF' },
};

export function engineDot(key, info) {
  const label = ENGINE_LABELS[key]?.name || key;
  const stateMap = { ok: '可用', warn: '降级', fail: '不可用', unknown: '未知' };
  const tip = `${label}：${stateMap[info?.state] || '未知'}${info?.version ? `（v${info.version}）` : ''}`;
  return el(`span.engine-dot`, {
    'data-state': info?.state || 'unknown',
    'data-tip': tip,
    title: tip,
  }, [el('span', { text: ENGINE_LABELS[key]?.short || key })]);
}

/* ================================================================== *
 * 10. 工具：把节点滚动到可见区域
 * ================================================================== */
export function scrollIntoViewIfNeeded(node, container) {
  if (!node || !container) return;
  const nRect = node.getBoundingClientRect();
  const cRect = container.getBoundingClientRect();
  if (nRect.top < cRect.top || nRect.bottom > cRect.bottom) {
    node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
}

/** 在容器上显示覆盖式加载遮罩 */
export function showLoadingOverlay(container, text = '处理中…') {
  const overlay = el('div.loading-overlay', {}, [
    el('div.col.center.gap-3', {}, [
      el('div.spinner', { style: { width: '26px', height: '26px', border: '3px solid var(--border-default)', borderTopColor: 'var(--accent)', borderRadius: '999px', animation: 'spin 700ms linear infinite' } }),
      el('div.t-sm.t-muted', { text }),
    ]),
  ]);
  container.style.position = 'relative';
  container.append(overlay);
  return () => overlay.remove();
}

export { debounce };
