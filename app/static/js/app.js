/**
 * app.js — 应用入口
 *
 * 职责边界（保持单一）：
 *   1. 装配：注册路由、构建导航、绑定外壳级交互
 *   2. 启动探测：后端可用性 → 决定 live / demo 模式
 *   3. 全局反馈：任务进度、引擎状态、磁盘余量、Toast 落地
 *   4. 全局异常兜底：把未捕获错误转成用户可理解的提示，而不是白屏
 *
 * 不负责：任何具体业务逻辑（那些在 views/*.js 中）。
 */

import { CFG, runtime } from './config.js';
import { $, el, clear, formatBytes, clamp } from './utils.js';
import { icon } from './icons.js';
import { toast, modal, engineDot, confirmDialog, tag } from './ui.js';
import {
  applyTheme, setTheme, loadToken, saveToken, loadSshHosts, loadAudioGate, setAuth,
  subscribe, getTasks, getActiveTasks, getState, setState, clearTasks,
} from './store.js';
import { probeBackend, api, onAuthExpired, ApiError } from './api.js';
import { defineRoute, startRouter, navigate, currentRoute, teardownCurrent } from './router.js';
import { switchHighlightTheme } from './loader.js';

/* ================================================================== *
 * 1. 路由与导航定义
 * ================================================================== */
const ROUTES = [
  { id: 'converter', title: '转换中心', iconName: 'convert',  desc: 'HTML / Office / PDF 互转', load: () => import('./views/converter.js') },
  { id: 'ssh',       title: 'SSH 终端', iconName: 'terminal', desc: '浏览器内远程运维',        load: () => import('./views/ssh.js') },
  { id: 'audio',     title: '音频工具', iconName: 'music',    desc: '本地音频格式转换',        load: () => import('./views/audio.js') },
  { id: 'video',     title: '视频下载', iconName: 'video',    desc: '抖音 / B站 / 快手 解析下载', load: () => import('./views/video.js') },
  { id: 'markdown',  title: 'MD 预览',  iconName: 'markdown', desc: 'Markdown 渲染与导出',     load: () => import('./views/markdown.js') },
  { id: 'geo',       title: '地图定位', iconName: 'map',      desc: '经纬度查询与地图打点',    load: () => import('./views/geo.js') },
  { id: 'tasks',     title: '任务中心', iconName: 'tasks',    desc: '任务进度与结果管理',      load: () => import('./views/tasks.js') },
];

for (const route of ROUTES) {
  defineRoute({
    id: route.id,
    title: route.title,
    load: route.load,
    // 任务中心与 Markdown 使用单栏布局，其余为左右分栏
    className: route.id === 'tasks' || route.id === 'markdown' ? 'view view--single' : 'view view--split',
  });
}

/* ================================================================== *
 * 2. 导航构建
 * ================================================================== */
const navEl = $('#app-nav');
const navLinks = new Map();
const taskBadge = el('span.nav-badge', { hidden: true, 'aria-hidden': 'true' });

function buildNav() {
  if (!navEl) return;
  clear(navEl);

  // 手机端的底部标签栏在 mobile.css 里写死了 5 列。模块数量是可变的
  // （新增模块时不该去改样式层），这里用内联样式按实际数量均分，
  // 内联优先级高于样式表，无需 !important。
  navEl.style.gridTemplateColumns = `repeat(${ROUTES.length}, minmax(0, 1fr))`;

  for (const route of ROUTES) {
    const link = el('a.nav-item', {
      href: `#/${route.id}`,
      title: route.desc,
      'data-route': route.id,
    }, [
      icon(route.iconName),
      el('span.nav-label', { text: route.title }),
      route.id === 'tasks' ? taskBadge : null,
    ]);
    // 屏幕阅读器需要明确的当前项标记（不依赖视觉样式）
    link.setAttribute('aria-current', 'false');
    navLinks.set(route.id, link);
    navEl.append(link);
  }
}

/** 路由变化时更新导航高亮、aria-current 与页面标题 */
function syncNav(activeId) {
  for (const [id, link] of navLinks) {
    const isActive = id === activeId;
    link.setAttribute('aria-current', isActive ? 'page' : 'false');
    // 当前项去掉 tooltip（冗余信息），其余项保留功能说明
    const desc = ROUTES.find((r) => r.id === id)?.desc;
    if (isActive || !desc) link.removeAttribute('data-tip');
    else link.setAttribute('data-tip', desc);
  }
  // 焦点回到主内容区，读屏用户能感知"页面已切换"
  $('#main')?.focus({ preventScroll: true });
}

/* ================================================================== *
 * 3. 主题切换
 * ================================================================== */
const THEME_CYCLE = ['auto', 'light', 'dark'];
const THEME_META = {
  auto:  { label: '跟随系统', iconName: 'monitor' },
  light: { label: '浅色',     iconName: 'sun' },
  dark:  { label: '深色',     iconName: 'moon' },
};

function renderThemeToggle() {
  const btn = $('#theme-toggle');
  const iconHost = $('#theme-toggle-icon');
  if (!btn || !iconHost) return;

  const meta = THEME_META[getState().theme] || THEME_META.auto;
  clear(iconHost);
  iconHost.append(icon(meta.iconName, 'icon'));
  btn.setAttribute('aria-label', `当前主题：${meta.label}，点击切换`);
  btn.setAttribute('data-tip', `主题：${meta.label}`);

  // 同步代码高亮皮肤（若已加载）
  switchHighlightTheme(document.documentElement.getAttribute('data-theme'));
}

function initThemeToggle() {
  $('#theme-toggle')?.addEventListener('click', () => {
    const current = getState().theme;
    const next = THEME_CYCLE[(THEME_CYCLE.indexOf(current) + 1) % THEME_CYCLE.length];
    setTheme(next);
    renderThemeToggle();
    toast('info', `主题已切换为「${THEME_META[next].label}」`, '', { duration: 1600 });
  });

  // 系统主题变化时（auto 模式）同步按钮文案
  subscribe(['theme', 'systemDark'], () => renderThemeToggle());
}

/* ================================================================== *
 * 4. 引擎状态与状态栏
 * ================================================================== */
function renderEngineDots() {
  const host = $('#engine-dots');
  if (!host) return;
  clear(host);

  const engines = getState().engines || {};
  let worstState = 'ok';

  for (const key of ['libreoffice', 'chromium', 'ffmpeg']) {
    const info = engines[key] || { state: 'unknown' };
    host.append(engineDot(key, info));
    if (info.state === 'fail') worstState = 'fail';
    else if (info.state === 'warn' && worstState !== 'fail') worstState = 'warn';
    else if (info.state === 'unknown' && worstState === 'ok') worstState = 'unknown';
  }

  host.setAttribute('data-worst', worstState);
}

function renderStatusBar() {
  const state = getState();
  const modeEl = $('#status-mode');
  const modeDot = $('#status-mode-dot');
  const diskEl = $('#status-disk');

  if (modeEl && modeDot) {
    if (!state.backend.checked) {
      modeEl.textContent = '正在检测服务端…';
      modeDot.dataset.state = 'unknown';
    } else if (runtime.demo) {
      modeEl.textContent = '演示模式（未连接后端）';
      modeDot.dataset.state = 'warn';
    } else {
      modeEl.textContent = `已连接服务端${state.backend.version ? ` v${state.backend.version}` : ''}`;
      modeDot.dataset.state = 'ok';
    }
  }

  if (diskEl) {
    const free = state.backend.diskFreeBytes;
    if (typeof free !== 'number') {
      diskEl.textContent = runtime.demo ? '磁盘 —（演示）' : '磁盘 —';
    } else {
      diskEl.textContent = `磁盘可用 ${formatBytes(free)}`;
      diskEl.classList.toggle('t-warning', state.backend.diskWarning);
    }
  }

  const versionEl = $('#brand-version');
  if (versionEl) {
    versionEl.textContent = `v${CFG.app.version}${runtime.demo ? ' · demo' : ''}`;
  }
}

/** 全局任务进度（状态栏常驻） */
function renderGlobalProgress() {
  const host = $('#statusbar-progress');
  const bar = $('#statusbar-progress-bar');
  const pctEl = $('#statusbar-progress-pct');
  const labelEl = $('#statusbar-progress-label');
  if (!host || !bar || !pctEl || !labelEl) return;

  const tasks = getTasks();
  const active = getActiveTasks();

  if (active.length === 0) {
    host.hidden = true;
    taskBadge.hidden = true;
    return;
  }

  const done = tasks.length - active.length;
  const overall = tasks.length > 0
    ? Math.round(((done + active.reduce((s, t) => s + (Number(t.progress) || 0) / 100, 0)) / tasks.length) * 100)
    : 0;

  host.hidden = false;
  labelEl.textContent = `任务进行中 ${done}/${tasks.length}`;
  bar.setAttribute('aria-valuenow', String(overall));
  bar.setAttribute('aria-label', `全局任务进度 ${overall}%`);
  bar.firstElementChild.style.width = `${overall}%`;
  pctEl.textContent = `${overall}%`;

  // 导航徽标
  taskBadge.hidden = false;
  taskBadge.textContent = String(active.length);
  taskBadge.dataset.variant = active.some((t) => t.status === 'running') ? 'default' : 'default';
}

/* ================================================================== *
 * 5. 演示模式提示
 * ================================================================== */
let demoBannerShown = false;

function showDemoBanner() {
  if (demoBannerShown || !runtime.demo) return;
  demoBannerShown = true;

  toast('warning', '后端未连接，已进入演示模式', '界面功能可完整体验，但转换结果为占位文件。启动 Python 后端后刷新即可接入真实引擎。', {
    duration: 9000,
    action: {
      label: '如何启动后端',
      onClick: () => modal({
        title: '如何连接真实后端',
        body: el('div.col.gap-3', {}, [
          el('div.alert.alert--info', {}, [
            icon('info'),
            el('div.alert-body', {}, [
              el('div.alert-title', { text: '当前状态：演示模式' }),
              el('div', {
                text: '前端已按 FastAPI 的接口约定实现（见 js/api.js）。启动后端后，'
                  + '启动探测会自动从演示模式切换到直连模式，无需改动任何前端代码。',
              }),
            ]),
          ]),
          el('div.form-section-title', { text: '启动步骤' }),
          el('ol', { style: { margin: 0, paddingLeft: 'var(--sp-6)', lineHeight: '2' } }, [
            el('li', { text: '在 toolkit/ 目录安装依赖：pip install -r requirements.txt' }),
            el('li', { text: '启动服务：uvicorn app.main:app --host 127.0.0.1 --port 5000' }),
            el('li', { text: '确认健康检查：浏览器访问 http://127.0.0.1:5000/healthz' }),
            el('li', { text: '刷新本页面，状态栏将显示「已连接服务端」' }),
          ]),
          el('div.alert.alert--accent', {}, [
            icon('zap'),
            el('div.alert-body', {}, [
              el('div.alert-title', { text: '强制切换模式' }),
              el('div', {
                text: '若需要跳过自动探测，可修改 js/config.js 中的 mode：'
                  + "'live' 强制直连（后端不可用时报错），'demo' 强制演示模式。",
              }),
            ]),
          ]),
        ]),
        size: 'wide',
        actions: [{ label: '知道了', variant: 'primary' }],
      }),
    },
  });
}

/* ================================================================== *
 * 6. 鉴权
 * ================================================================== */
function renderAuthButton() {
  const btn = $('#auth-button');
  const label = $('#auth-button-label');
  const state = getState();
  if (!btn || !label) return;

  // 仅在后端明确要求鉴权时显示（离线单机版 auth_required=false）
  if (!state.auth.required && !state.auth.loggedIn) {
    btn.hidden = true;
    return;
  }

  btn.hidden = false;
  label.textContent = state.auth.loggedIn
    ? (state.auth.user?.display_name || state.auth.user?.username || '已登录')
    : '登录';
  btn.setAttribute('data-tip', state.auth.loggedIn ? '点击退出登录' : '点击登录');
}

/* ------------------------------------------------------------------ *
 * 账号体系：登录 / 注册 / 忘记密码
 * ------------------------------------------------------------------ */

/** 公开的鉴权配置（进程内缓存一次；拿不到时按最保守策略渲染） */
let authConfigCache = null;

async function getAuthConfig() {
  if (authConfigCache) return authConfigCache;
  try {
    authConfigCache = await api.authConfig();
  } catch {
    authConfigCache = {
      allow_registration: false,
      require_email_verification: true,
      login_require_code: false,
      min_password_length: 8,
      code_resend_sec: 60,
      email_configured: false,
    };
  }
  return authConfigCache;
}

/**
 * 创建「发送验证码」按钮，带倒计时与失败恢复。
 * @param {{target:()=>string, purpose:string, onError:(m:string)=>void, onOk:(m:string)=>void}} cfg
 */
function createCodeSender(cfg) {
  const btn = el('button.btn.btn--secondary.btn--sm', { type: 'button', text: '发送验证码' });
  let timer = null;

  const stop = () => {
    if (timer) { clearInterval(timer); timer = null; }
  };

  btn.addEventListener('click', async () => {
    const value = (cfg.target() || '').trim();
    if (!value) { cfg.onError('请先填写邮箱地址'); return; }
    btn.disabled = true;
    btn.textContent = '发送中…';
    try {
      const data = await api.sendCode({ target: value, channel: 'email', purpose: cfg.purpose });
      cfg.onOk('验证码已发送，请查收邮件（若未收到请检查垃圾邮件）。');
      let left = Number(data?.resend_after) || 60;
      const tick = () => {
        if (left <= 0) { stop(); btn.disabled = false; btn.textContent = '重新发送'; return; }
        btn.textContent = `${left} 秒后可重发`;
        left -= 1;
      };
      tick();
      timer = setInterval(tick, 1000);
    } catch (err) {
      btn.disabled = false;
      btn.textContent = '发送验证码';
      cfg.onError(err instanceof ApiError ? err.message : String(err?.message || err));
    }
  });

  return { btn, stop };
}

/**
 * 登录 / 注册 / 忘记密码三合一表单。
 *
 * 有两种形态：
 *   · 普通弹窗（默认）—— 顶栏「登录」按钮弹出，可关闭；
 *   · 登录门禁（``opts.gate=true``）—— 未登录时占据整个界面，不可关闭、
 *     不可 Esc 跳过，登录成功后由调用方放行进入控制面板。
 *     这是「不输入账号密码就什么都看不到」的落点。
 *
 * @param {string} [reason] 顶部提示（如「登录状态已失效」）
 * @param {'login'|'register'|'reset'} [initialMode]
 * @param {{gate?:boolean, retry?:boolean, onClosed?:Function, onRetry?:Function}} [opts]
 * @returns {{close:()=>void, setReason:(text:string)=>void}}
 */
function showLoginDialog(reason = '', initialMode = 'login', opts = {}) {
  const gate = Boolean(opts.gate);
  let mode = initialMode;
  let currentReason = reason;
  let codeSender = null;
  let config = {
    allow_registration: false,
    require_email_verification: true,
    login_require_code: false,
    min_password_length: 8,
    code_resend_sec: 60,
    email_configured: false,
  };

  const tabsHost = el('div');
  const formHost = el('div.col.gap-4');
  const noticeHost = el('div');
  const hintHost = el('div.help');

  // 门禁形态不提供关闭入口：关掉它只会留下既看不到面板、也没有登录页的死界面
  const actions = gate
    ? (opts.retry
      ? [{ label: '重试连接', value: null, closeOnClick: true, onClick: () => opts.onRetry?.() }]
      : [])
    : [{ label: '关闭', value: null, closeOnClick: true }];

  const dlg = modal({
    title: gate ? '登录后才能进入控制面板' : '登录',
    body: el('div.col.gap-4', {}, [tabsHost, formHost, noticeHost, hintHost]),
    dismissible: gate ? false : !getState().auth.required,
    actions,
    onClose: () => {
      codeSender?.stop();
      opts.onClosed?.();
    },
  });
  const titleNode = $('.modal-title', dlg.root);

  const clearNotice = () => clear(noticeHost);

  function showError(message) {
    clearNotice();
    noticeHost.append(el('div.alert.alert--danger', { role: 'alert' }, [
      icon('alertCircle'),
      el('div.alert-body', {}, [
        el('div.alert-title', { text: '操作失败' }),
        el('div', { text: message }),
      ]),
    ]));
  }

  function showInfo(message) {
    clearNotice();
    noticeHost.append(el('div.alert.alert--success', {}, [
      icon('checkCircle'),
      el('div.alert-body', {}, [el('div', { text: message })]),
    ]));
  }

  function labeled(text, control) {
    return el('div.field', {}, [el('label.label', { text }), control]);
  }

  function codeRow(targetGetter, purpose, labelText) {
    const input = el('input.input', { type: 'text', inputmode: 'numeric', maxlength: '6', placeholder: '6 位验证码', autocomplete: 'one-time-code' });
    codeSender?.stop();
    codeSender = createCodeSender({
      target: targetGetter,
      purpose,
      onError: showError,
      onOk: showInfo,
    });
    return el('div.field', {}, [
      el('label.label', { text: labelText }),
      el('div.row.gap-2', {}, [input, codeSender.btn]),
    ]);
  }

  function buildTabs() {
    const items = [['login', '登录']];
    if (config.allow_registration) items.push(['register', '注册']);
    items.push(['reset', '忘记密码']);
    if (items.length < 2) return null;
    const bar = el('div.segmented', { role: 'group', 'aria-label': '账号操作' });
    for (const [id, label] of items) {
      bar.append(el('button.segmented-item', {
        type: 'button',
        text: label,
        'aria-pressed': mode === id ? 'true' : 'false',
        on: { click: () => { mode = id; clearNotice(); render(); } },
      }));
    }
    return bar;
  }

  function submitButton(label, handler, form) {
    const btn = el('button.btn.btn--primary.btn--block', { type: 'submit', text: label });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      clearNotice();
      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = '处理中…';
      try {
        await handler();
      } catch (err) {
        showError(err instanceof ApiError ? err.message : String(err?.message || err));
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    });
    return btn;
  }

  function onAuthSuccess(user, greeting) {
    codeSender?.stop();
    dlg.close(true);
    toast('success', greeting, `欢迎，${user?.display_name || user?.username || ''}`);
    // 登录成功即放行：解除门禁并渲染控制面板
    enterShell();
  }

  /* ---------------- 三种模式的表单 ---------------- */

  function renderLogin() {
    const userInput = el('input.input', { type: 'text', placeholder: '用户名或邮箱', autocomplete: 'username' });
    const passInput = el('input.input', { type: 'password', placeholder: '密码', autocomplete: 'current-password' });
    const form = el('form.col.gap-4');
    form.append(labeled('用户名 / 邮箱', userInput));
    form.append(labeled('密码', passInput));

    let codeInput = null;
    if (config.login_require_code) {
      const row = codeRow(() => userInput.value, 'login', '邮箱验证码');
      codeInput = $('input', row);
      form.append(row);
    }

    form.append(submitButton('登录', async () => {
      const username = userInput.value.trim();
      const password = passInput.value;
      if (!username || !password) { showError('用户名与密码不能为空'); return; }
      const data = await api.login({ username, password, code: codeInput ? codeInput.value.trim() : '' });
      onAuthSuccess(data.user ?? { username }, '登录成功');
    }, form));

    formHost.append(form);
    requestAnimationFrame(() => userInput.focus());
  }

  function renderRegister() {
    const emailInput = el('input.input', { type: 'email', placeholder: 'you@example.com', autocomplete: 'email' });
    const passInput = el('input.input', { type: 'password', placeholder: `至少 ${config.min_password_length} 位`, autocomplete: 'new-password' });
    const nameInput = el('input.input', { type: 'text', placeholder: '显示名（可选）', autocomplete: 'nickname' });

    const form = el('form.col.gap-4');
    form.append(labeled('邮箱', emailInput));
    if (config.require_email_verification) {
      form.append(codeRow(() => emailInput.value, 'register', '邮箱验证码'));
    }
    form.append(labeled('设置密码', passInput));
    form.append(labeled('显示名', nameInput));

    const readCode = () => form.querySelector('input[inputmode="numeric"]')?.value.trim() || '';

    form.append(submitButton('注册并登录', async () => {
      const email = emailInput.value.trim();
      if (!email) { showError('请填写邮箱'); return; }
      const data = await api.register({
        email,
        password: passInput.value,
        code: config.require_email_verification ? readCode() : '',
        displayName: nameInput.value.trim(),
      });
      if (data?.pending_approval) {
        if (!gate) {
          dlg.close(true);
          toast('info', '注册已提交', '需管理员审批通过后才能登录。');
          return;
        }
        // 门禁形态下不能关闭弹窗（关掉就没有登录入口了）：回到登录页并说明
        mode = 'login';
        render();
        showInfo('注册已提交，需管理员审批通过后才能登录。');
        return;
      }
      onAuthSuccess(data.user ?? { username: email }, '注册成功');
    }, form));

    formHost.append(form);
    requestAnimationFrame(() => emailInput.focus());
  }

  function renderReset() {
    const emailInput = el('input.input', { type: 'email', placeholder: '注册时使用的邮箱', autocomplete: 'email' });
    const passInput = el('input.input', { type: 'password', placeholder: `新密码，至少 ${config.min_password_length} 位`, autocomplete: 'new-password' });

    const form = el('form.col.gap-4');
    form.append(labeled('邮箱', emailInput));
    form.append(codeRow(() => emailInput.value, 'reset', '邮箱验证码'));
    form.append(labeled('新密码', passInput));

    form.append(submitButton('重置密码', async () => {
      const email = emailInput.value.trim();
      const code = form.querySelector('input[inputmode="numeric"]')?.value.trim() || '';
      const newPassword = passInput.value;
      if (!email || !code) { showError('请填写邮箱与验证码'); return; }
      await api.resetPassword({ email, code, newPassword });
      toast('success', '密码已重置', '请使用新密码登录。');
      mode = 'login';
      render();
    }, form));

    formHost.append(form);
    requestAnimationFrame(() => emailInput.focus());
  }

  function render() {
    clear(formHost);
    clearNotice();

    const tabs = buildTabs();
    clear(tabsHost);
    if (tabs) tabsHost.append(tabs);

    if (titleNode) {
      // 门禁形态下标题点明「登录后才能进入控制面板」，与面板内偶尔弹出的登录框区分开
      titleNode.textContent = mode === 'register'
        ? '注册账号'
        : (mode === 'reset' ? '重置密码' : (gate ? '登录后才能进入控制面板' : '登录'));
    }

    if (currentReason) {
      noticeHost.append(el('div.alert.alert--warning', {}, [
        icon('lock'),
        el('div.alert-body', {}, [el('div', { text: currentReason })]),
      ]));
    }

    if (mode === 'register') {
      if (!config.email_configured) {
        hintHost.textContent = '⚠️ 服务端尚未配置邮件服务（SMTP），验证码无法送达，请联系管理员。';
      } else {
        hintHost.textContent = '注册需要邮箱验证码。密码仅保存哈希值，服务端无法还原。';
      }
      renderRegister();
    } else if (mode === 'reset') {
      hintHost.textContent = '通过注册邮箱接收验证码来重置密码。';
      renderReset();
    } else {
      hintHost.textContent = '初始密码在服务端首次启动时生成并打印到控制台；也可在 config.toml 的 password_hash 中配置。';
      renderLogin();
    }
  }

  // 先按默认配置渲染，再异步补上真实配置后重绘（避免弹窗出现前长时间空白）
  render();
  getAuthConfig().then((cfg) => {
    config = cfg;
    if (mode === 'register' && !cfg.allow_registration) mode = 'login';
    render();
  });

  return {
    close: () => dlg.close(null),
    /** 更新顶部提示；不重建表单，避免把用户已输入的内容清掉 */
    setReason: (text) => { currentReason = text; render(); },
  };
}

function initAuth() {
  $('#auth-button')?.addEventListener('click', async () => {
    if (getState().auth.loggedIn) {
      const ok = await confirmDialog({
        title: '退出登录',
        message: '退出后需要重新输入账号和密码才能进入控制面板。',
        confirmLabel: '退出',
      });
      if (!ok) return;
      try { await api.logout(); } catch { /* 服务端登出失败不阻塞本地清理 */ }
      showLoginGate('已退出登录。');
      toast('info', '已退出登录');
      return;
    }
    showLoginGate('请输入账号和密码后进入控制面板。');
  });

  // Token 失效由 api 层广播（仅当原本确实持有会话时才会触发），这里退回登录页
  onAuthExpired(() => {
    showLoginGate('登录状态已失效，请重新登录后继续操作。');
  });

  subscribe(['auth'], renderAuthButton);
}

/* ------------------------------------------------------------------ *
 * 6.2 登录门禁：控制面板的入口开关
 * ------------------------------------------------------------------ */

/**
 * 门禁状态挂在 <html data-app-gate> 上（index.html 默认 "locked"）：
 *   · locked —— layout.css 隐藏 .app 整个外壳，界面上只剩登录页；
 *   · open   —— 正常显示控制面板。
 *
 * 「默认锁定」是刻意的：即使后续脚本抛错、网络请求长时间挂起，
 * 未登录状态下也绝不会漏出面板内容。
 *
 * @param {'locked'|'open'} state 目标状态。
 */
function setGate(state) {
  document.documentElement.setAttribute('data-app-gate', state === 'open' ? 'open' : 'locked');
}

/** 当前是否处于锁定（未登录）状态 */
const isGateLocked = () => document.documentElement.getAttribute('data-app-gate') !== 'open';

/** 更新登录页的提示文案（脚本未就绪 / 连不上服务端时唯一可读的信息） */
function setGateSplash(text) {
  const node = $('#auth-gate-sub');
  if (node && text) node.textContent = text;
}

let shellReady = false;   // 外壳（导航 / 路由 / 订阅）是否已初始化
let gateHandle = null;    // 当前登录页句柄

/**
 * 清空面板痕迹：DOM、任务列表与运行态数据一并清掉。
 *
 * 只隐藏外壳是不够的——残留的任务列表、引擎状态会在下次登录时直接显示出来，
 * 等于把上一个会话的数据留在页面上。
 */
function leaveShell() {
  teardownCurrent().catch(() => {});

  const main = $('#main');
  if (main) clear(main);
  if (navEl) clear(navEl);
  navLinks.clear();
  clearTasks();

  setState({
    engines: {
      libreoffice: { state: 'unknown', version: null, path: null, hint: null },
      chromium:    { state: 'unknown', version: null, path: null, hint: null },
      ffmpeg:      { state: 'unknown', version: null, path: null, hint: null },
    },
    backend: { online: false, checked: false, version: null, diskFreeBytes: null, diskWarning: false },
  });

  const modeEl = $('#status-mode');
  const modeDot = $('#status-mode-dot');
  const diskEl = $('#status-disk');
  if (modeEl) modeEl.textContent = '未登录';
  if (modeDot) modeDot.dataset.state = 'unknown';
  if (diskEl) diskEl.textContent = '磁盘 —';

  renderEngineDots();
  renderGlobalProgress();
}

/**
 * 进入控制面板：解除门禁并渲染外壳。
 *
 * 首次进入做完整初始化（导航、路由、订阅）；登出后再次登录只需重建导航与当前视图
 * ——重复调用 startRouter 会叠加 hashchange 监听，重复 subscribe 会让渲染回调跑两遍。
 */
function enterShell() {
  setGate('open');
  gateHandle?.close();
  gateHandle = null;

  if (!shellReady) {
    shellReady = true;

    buildNav();
    renderThemeToggle();
    initThemeToggle();
    initAuth();
    initShortcuts();

    // ---- 订阅状态变化 → 更新外壳 ----
    subscribe(['tasks'], renderGlobalProgress);
    subscribe(['engines'], () => { renderEngineDots(); renderStatusBar(); });
    subscribe(['backend'], () => { renderStatusBar(); renderEngineDots(); });

    // ---- 启动路由（视图按 hash 懒加载） ----
    startRouter({
      outlet: $('#main'),
      defaultId: 'converter',
      onChange: (id) => syncNav(id),
      onNotFound: (id) => {
        toast('warning', '页面不存在', `未找到「${id}」，已返回转换中心`);
        navigate('converter', { replace: true });
      },
    });

    // ---- 任务中心徽标点击 → 跳转任务中心 ----
    taskBadge.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      navigate('tasks');
    });
  } else {
    // 二次进入：导航与视图容器都在 leaveShell 里清空了，需要重建
    buildNav();
    const id = currentRoute() || 'converter';
    syncNav(id);
    navigate(id, { replace: true });
  }

  renderAuthButton();
  renderStatusBar();
  renderEngineDots();
  renderGlobalProgress();

  // 登录前 /healthz 只返回最小事实，这里补拉一次带 Token 的完整状态
  if (getState().auth.required) {
    refreshRuntimeStatus();
  } else if (runtime.demo) {
    // 显式演示模式（?mode=demo / file://）：提醒当前结果均为占位数据
    showDemoBanner();
  }
}

/** 补拉运行时状态（磁盘余量、引擎指示灯）；失败只影响状态栏文案 */
async function refreshRuntimeStatus() {
  try {
    await probeBackend();
  } catch { /* 忽略 */ }
  renderStatusBar();
  renderEngineDots();
}

/**
 * 锁定主界面并展示登录页。未登录、登录态失效、主动登出都汇聚到这里。
 *
 * @param {string} [reason] 顶部提示文案。
 * @param {{retry?:boolean}} [opts] retry=true 时额外提供「重试连接」按钮。
 */
function showLoginGate(reason = '', opts = {}) {
  setGate('locked');
  leaveShell();
  setGateSplash(reason || '请登录后使用');
  // 登录抽屉已经接管界面：把底下的静态门禁页藏掉。
  // 否则它那颗大号品牌方块会从抽屉上方露出来（手机底部抽屉尤其明显）。
  $('#auth-gate')?.setAttribute('hidden', '');

  if (gateHandle) {
    // 已在登录页：只更新提示，不重建表单（否则会清掉用户已输入的内容）
    gateHandle.setReason(reason);
    return;
  }

  gateHandle = showLoginDialog(reason, 'login', {
    gate: true,
    retry: Boolean(opts.retry),
    onClosed: () => { gateHandle = null; },
    onRetry: () => { decideEntry(); },
  });
}

/**
 * 首屏准入判定：进入控制面板，还是停在登录页。
 *
 * 判断顺序（只要有一环不确定就停在登录页，即 fail closed）：
 *   1. 探测后端 —— 决定 live / demo，并拿到「服务端是否要求鉴权」；
 *   2. 在线且明确不要求鉴权 → 免登录进入（本机离线自用形态）；
 *   3. 不可达且非显式 demo 模式 → 停在登录页并提示重试（不能确认身份就不放行）；
 *   4. 其余情况 → 先尝试用本地 Token 恢复会话，失败则展示登录表单。
 */
async function decideEntry() {
  setGateSplash('正在检查登录状态…');

  let online = false;
  try {
    online = Boolean((await probeBackend()).online);
  } catch (err) {
    // CFG.mode='live' 且后端不可达：探测逻辑会抛错，停在登录页并说明原因
    showLoginGate(`服务端不可达：${err?.message || err}`, { retry: true });
    return;
  }

  // 2) 服务端明确不需要鉴权（本机离线自用形态）
  if (online && !getState().auth.required && CFG.mode !== 'demo') {
    // 残留的旧 Token 在这种形态下没有意义，清掉以免顶栏显示上一个会话的身份
    if (getState().auth.loggedIn) {
      saveToken(null);
      setAuth({ loggedIn: false, token: null, user: null });
    }
    enterShell();
    toast('success', '已连接服务端', `版本 v${getState().backend.version}`, { duration: 3000 });
    return;
  }

  // 3) 连不上服务端：无法确认身份，停在登录页
  if (!online && CFG.mode !== 'demo') {
    showLoginGate('无法连接服务端，暂时无法登录。请确认服务已启动，然后点击「重试连接」。', { retry: true });
    return;
  }

  // 4) 需要登录：先用本地 Token 尝试恢复会话（避免每次刷新都重新输入密码）
  if (getState().auth.loggedIn) {
    setGateSplash('正在恢复登录状态…');
    try {
      const data = await api.me();
      if (data?.user) {
        setAuth({ loggedIn: true, required: true, user: data.user });
        setState({ auth: { ...getState().auth, superadmin: Boolean(data.is_superadmin) } });
        enterShell();
        return;
      }
    } catch {
      // 401 已由 api 层清理 Token 并广播；其余错误一并退回登录页
    }
  }

  showLoginGate(
    getState().auth.required || online
      ? '本系统需要登录后才能使用，请输入账号和密码。'
      : '当前为演示模式（未连接后端），请输入任意账号密码体验界面。',
  );
}

/* ================================================================== *
 * 7. 快捷键
 * ================================================================== */
const SHORTCUTS = [
  { keys: ['Alt', '1 – 6'], desc: '在六个模块之间快速切换' },
  { keys: ['?'], desc: '打开本快捷键面板' },
  { keys: ['Esc'], desc: '关闭弹窗 / 取消当前操作' },
  { section: '转换中心' },
  { keys: ['Enter'], desc: '在上传区按回车打开文件选择框' },
  { section: 'SSH 终端' },
  { keys: ['Ctrl', 'C'], desc: '无选中内容时发送中断信号（SIGINT）' },
  { keys: ['Ctrl', 'Shift', 'C'], desc: '复制选中的终端内容' },
  { keys: ['Ctrl', 'Shift', 'V'], desc: '从剪贴板粘贴到终端' },
  { keys: ['Ctrl', 'Shift', 'F'], desc: '浏览器原生查找（终端内）' },
  { section: 'Markdown 预览' },
  { keys: ['Ctrl', 'S'], desc: '导出为 PDF' },
  { keys: ['Ctrl', 'B'], desc: '加粗选中文本' },
  { keys: ['Tab'], desc: '在编辑器中插入两个空格（不切换焦点）' },
  { keys: ['Ctrl', 'P'], desc: '调用浏览器打印' },
  { section: '地图定位' },
  { keys: ['Enter'], desc: '在坐标输入框中按回车立即定位' },
  { keys: ['单击地图'], desc: '在地图上取点，坐标回填到输入框' },
  { keys: ['拖动 / 滚轮'], desc: '平移与缩放地图（双指可捏合缩放）' },
  { section: '任务中心' },
  { keys: ['Ctrl', 'F'], desc: '聚焦搜索框' },
];

function showShortcutsDialog() {
  const body = el('div.col.gap-4');
  const list = el('div.col.gap-1');

  for (const item of SHORTCUTS) {
    if (item.section) {
      list.append(el('div.form-section-title', { text: item.section, style: { marginTop: 'var(--sp-3)' } }));
      continue;
    }
    list.append(el('div.row.between.gap-4', { style: { padding: 'var(--sp-2) 0', borderBottom: '1px solid var(--border-subtle)' } }, [
      el('span.t-sm.t-secondary', { text: item.desc }),
      el('span.row.gap-1.shrink-0', {}, item.keys.map((k) => el('kbd', { text: k }))),
    ]));
  }

  body.append(
    el('div.help', { text: '在输入框、编辑器或终端获得焦点时，字母类快捷键不会触发，避免误操作。' }),
    list,
  );

  modal({ title: '键盘快捷键', size: 'wide', body, actions: [{ label: '关闭', variant: 'secondary' }] });
}

function initShortcuts() {
  window.addEventListener('keydown', (e) => {
    const target = e.target;
    const isTyping = target instanceof HTMLElement
      && (target.matches('input, textarea, select, [contenteditable="true"]')
          || target.classList.contains('xterm-helper-textarea'));

    // Alt + 1..9 切换视图（不受输入状态影响，用 Alt 修饰避免误触）
    if (e.altKey && !e.ctrlKey && !e.metaKey && /^[1-9]$/.test(e.key)) {
      const index = Number(e.key) - 1;
      if (ROUTES[index]) {
        e.preventDefault();
        navigate(ROUTES[index].id);
      }
      return;
    }

    if (isTyping) return;

    // ? 打开快捷键面板
    if (e.key === '?' || (e.shiftKey && e.key === '/')) {
      e.preventDefault();
      showShortcutsDialog();
      return;
    }

    // Ctrl/Cmd + F 在任务中心聚焦搜索
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f' && currentRoute() === 'tasks') {
      const search = $('input[type="search"]');
      if (search) {
        e.preventDefault();
        search.focus();
        search.select();
      }
    }
  });
}

/* ================================================================== *
 * 8. 全局错误兜底
 * ================================================================== */
let lastErrorAt = 0;

function reportError(kind, error) {
  const now = Date.now();
  // 同一时间的重复错误只提示一次（例如循环中反复抛错的场景）
  if (now - lastErrorAt < 800) return;
  lastErrorAt = now;

  const message = error instanceof ApiError
    ? error.message
    : String(error?.message || error || '未知错误');

  console.error(`[${kind}]`, error);
  toast('danger', '出现了未预期的问题', `${message}。如果影响正常使用，请刷新页面重试。`);
}

function initErrorHandling() {
  window.addEventListener('error', (event) => {
    // 资源加载失败（img/script）不弹全局提示，交由各自的降级逻辑处理
    if (event.target && event.target !== window && event.target.tagName) return;
    reportError('uncaught', event.error || event.message);
  });

  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    // 主动取消的请求不算错误
    if (reason?.code === 'ABORTED') {
      event.preventDefault();
      return;
    }
    reportError('unhandledrejection', reason);
  });
}

/* ================================================================== *
 * 9. 启动流程
 * ================================================================== */
async function boot() {
  // ---- 9.1 恢复本地偏好（同步，先于任何渲染） ----
  applyTheme(getState().theme);
  loadSshHosts();
  loadAudioGate();
  // Token 先读出来：恢复会话要用，且失败时清理逻辑依赖它判断「原本是否有会话」
  loadToken();

  // ---- 9.2 门禁默认锁定 ----
  // index.html 已写死 data-app-gate="locked"，这里再确认一次：
  // 「先登录才能进入控制面板」在脚本执行前后都成立，首帧不会闪出面板。
  setGate('locked');

  // 错误兜底必须先装好：门禁期间的异常同样要给用户可读提示，而不是白屏
  initErrorHandling();
  setGateSplash('正在检查登录状态…');

  // 应用从这一刻起已接管界面（哪怕当前显示的是登录页），
  // 启动守卫不必再叠加通用报错面板。
  window.__TOOLKIT_READY__ = true;

  // ---- 9.3 准入判定：进入控制面板 或 停在登录页 ----
  await decideEntry();

  // ---- 9.4 首屏完成标记（便于性能测量） ----
  try {
    performance.mark('toolkit:ready');
    const navEntry = performance.getEntriesByType('navigation')[0];
    if (navEntry) {
      const readyMs = Math.round(performance.now());
      console.info(`%c全能工具站 %c已就绪 ${readyMs} ms${runtime.demo ? '（演示模式）' : ''}`,
        'color:#2563eb;font-weight:600', 'color:inherit');
    }
  } catch { /* 性能 API 不可用时忽略 */ }
}

// DOM 已就绪（module 脚本天然 defer），直接启动
boot().catch((err) => {
  console.error('[boot] 启动失败：', err);

  // 应用已接管错误展示（下面的提示比启动守卫的通用文案更具体），
  // 置位后守卫不再叠加显示，避免出现两套错误界面。
  window.__TOOLKIT_READY__ = true;

  // 门禁锁定中（未登录）时主内容区不可见，错误写到登录页上才有意义
  if (isGateLocked()) {
    // 登录抽屉可能已经把它藏起来了，出错时重新露出来，避免只剩控制台有信息
    $('#auth-gate')?.removeAttribute('hidden');
    setGateSplash(`应用启动失败：${String(err?.message || err)}。请刷新页面重试，或查看浏览器控制台。`);
    return;
  }

  const main = $('#main');
  if (main) {
    clear(main);
    main.append(el('div.empty', {}, [
      el('div.empty-title', { text: '应用启动失败' }),
      el('div.empty-desc', { text: String(err?.message || err) }),
      el('div.help', { text: '请打开浏览器控制台查看详细堆栈；若为资源加载失败，请确认 static 目录完整。' }),
    ]));
  }
});
