/**
 * views/ssh.js — 模块二：SSH 远程终端
 *
 * 覆盖需求：FR-SSH-01 ~ FR-SSH-09
 *   - 密码 / 私钥（含 passphrase）两种认证
 *   - xterm.js 全功能终端：vim / top 等全屏程序、UTF-8 中文、256 色
 *   - 组合键映射：Ctrl+C 无选区时透传 SIGINT，有选区时复制；Ctrl+Shift+C/V 显式复制粘贴
 *   - 会话标签管理、重连、空闲超时自动断开
 *   - 窗口尺寸变化同步 pty size（ResizeObserver + FitAddon）
 *
 * 安全约束（NFR-SSH-01）：密码与私钥只存在于本视图的内存变量中，
 * 绝不写入 localStorage / sessionStorage / URL，也绝不打印到控制台。
 */

import { el, clear, $, uid, debounce, clamp } from '../utils.js';
import { icon } from '../icons.js';
import { sshApi, ApiError } from '../api.js';
import { toast, modal, confirmDialog, emptyState } from '../ui.js';
import { loadSshHosts, saveSshHosts, state as storeState, subscribe } from '../store.js';
import { needXterm } from '../loader.js';

/* ------------------------------------------------------------------ *
 * 终端配色主题（终端语义色不可用 CSS 变量，需显式给出）
 * ------------------------------------------------------------------ */
const TERM_THEMES = {
  dark: {
    background: '#0b1017',
    foreground: '#d3dde8',
    cursor: '#679eff',
    cursorAccent: '#0b1017',
    selectionBackground: '#1e3a5f',
    black: '#2b3542', red: '#f87171', green: '#4ade80', yellow: '#fbbf24',
    blue: '#679eff', magenta: '#c084fc', cyan: '#38d0e8', white: '#d3dde8',
    brightBlack: '#6f7a8a', brightRed: '#fca5a5', brightGreen: '#86efac', brightYellow: '#fde68a',
    brightBlue: '#93bbfd', brightMagenta: '#d8b4fe', brightCyan: '#7dd3fc', brightWhite: '#ffffff',
  },
  light: {
    background: '#ffffff',
    foreground: '#1f2733',
    cursor: '#2563eb',
    cursorAccent: '#ffffff',
    selectionBackground: '#cfe0ff',
    black: '#3b4553', red: '#b91c1c', green: '#15803d', yellow: '#a16207',
    blue: '#1d4ed8', magenta: '#7e22ce', cyan: '#0e7490', white: '#e5e7eb',
    brightBlack: '#6f7a8a', brightRed: '#dc2626', brightGreen: '#16a34a', brightYellow: '#ca8a04',
    brightBlue: '#2563eb', brightMagenta: '#8b5cf6', brightCyan: '#0891b2', brightWhite: '#ffffff',
  },
};

const TERM_FONT_SIZES = [11, 12, 13, 14, 15, 16, 18, 20];

/* ------------------------------------------------------------------ *
 * 视图工厂
 * ------------------------------------------------------------------ */
export default async function createSshView(container) {
  /* ---------------- 状态 ---------------- */
  const state = {
    /** @type {Map<string, object>} sessionId → session */
    sessions: new Map(),
    activeId: null,
    fontSizes: {},        // sessionId → fontSize
    termTheme: 'dark',    // 'dark' | 'light'
    connecting: false,
    hosts: [],
    idleLimitMin: 30,     // 空闲断开阈值（分钟），与 config.toml [ssh].idle_timeout_min 对齐
  };

  const disposers = [];
  let XtermLib = null;
  let shellCssLoaded = false;
  /** 移动端 Ctrl 粘滞键状态（点一次点亮，下一个字符转控制键） */
  let ctrlArmed = false;
  /** @type {HTMLButtonElement|null} */
  let ctrlBtn = null;

  /* ================= 骨架 ================= */
  // 用 classList 增删而非赋值 className：路由依赖容器上的 `is-active` 控制可见性
  container.classList.remove('view--single');
  container.classList.add('view', 'view--split');
  container.innerHTML = '';

  /* ---------- 左：连接表单 ---------- */
  const hostsListEl = el('div.list');

  const hostInput = el('input.input', { type: 'text', placeholder: '192.168.1.10 或 example.com', autocomplete: 'off', spellcheck: 'false' });
  const portInput = el('input.input', { type: 'number', value: '22', min: '1', max: '65535' });
  const userInput = el('input.input', { type: 'text', placeholder: 'root', autocomplete: 'off', spellcheck: 'false' });
  const passInput = el('input.input', { type: 'password', placeholder: '仅保存在内存，不会落盘', autocomplete: 'new-password' });

  const authSelect = el('select.select');
  authSelect.append(
    el('option', { value: 'password', text: '密码认证' }),
    el('option', { value: 'key', text: '私钥认证（PEM / OpenSSH）' }),
  );

  const keyInput = el('textarea.textarea.key-input', {
    placeholder: '-----BEGIN OPENSSH PRIVATE KEY-----\n…\n-----END OPENSSH PRIVATE KEY-----',
    spellcheck: 'false',
    autocomplete: 'off',
  });
  const passphraseInput = el('input.input', { type: 'password', placeholder: '私钥口令（无则留空）', autocomplete: 'new-password' });

  const keyField = el('div.field.hidden', {}, [
    el('label.label', { text: '私钥内容' }),
    keyInput,
    el('div.help', { text: '直接粘贴私钥文本。私钥仅用于本次连接，不会保存到任何位置。' }),
  ]);
  const passphraseField = el('div.field.hidden', {}, [
    el('label.label', { text: '私钥口令' }),
    passphraseInput,
  ]);
  const passwordField = el('div.field', {}, [
    el('label.label', { text: '密码' }),
    passInput,
  ]);

  authSelect.addEventListener('change', () => {
    const isKey = authSelect.value === 'key';
    keyField.classList.toggle('hidden', !isKey);
    passphraseField.classList.toggle('hidden', !isKey);
    passwordField.classList.toggle('hidden', isKey);
  });

  const saveHostCheck = el('input', { type: 'checkbox', id: 'ssh-save-host' });
  const strictCheck = el('input', { type: 'checkbox', id: 'ssh-strict-key' });

  const connectBtn = el('button.btn.btn--primary.btn--block.btn--lg', {
    type: 'button',
    on: { click: () => connect() },
  }, [icon('terminal'), el('span', { text: '连接' })]);

  const formStatus = el('div');

  const leftPanel = el('div.panel.panel--left', {}, [
    el('div.panel-header', {}, [
      el('h2', {}, [icon('terminal'), el('span', { text: 'SSH 连接' })]),
      el('div.panel-actions', {}, [
        el('button.btn.btn--sm.btn--ghost', {
          type: 'button',
          on: { click: () => showSecurityDialog() },
        }, [icon('shield'), el('span', { text: '安全说明' })]),
      ]),
    ]),
    el('div.panel-body.panel-body--grid', {}, [
      // 已保存主机
      el('details.disclosure', { open: storeState.sshHosts.length > 0 }, [
        el('summary', {}, [el('span', { text: `已保存主机（${storeState.sshHosts.length}）` })]),
        el('div.disclosure-body', {}, [hostsListEl]),
      ]),

      el('div.form-section', {}, [
        el('div.form-section-title', {}, [icon('server'), el('span', { text: '目标主机' })]),
        el('div.field-row', {}, [
          el('div.field', {}, [
            el('label.label', {}, [el('span', { text: '主机地址' }), el('span.required', { text: '*' })]),
            hostInput,
          ]),
          el('div.field', {}, [
            el('label.label', {}, [el('span', { text: '端口' })]),
            portInput,
          ]),
        ]),
        el('div.field', {}, [
          el('label.label', {}, [el('span', { text: '用户名' }), el('span.required', { text: '*' })]),
          userInput,
        ]),
      ]),

      el('div.form-section', {}, [
        el('div.form-section-title', {}, [icon('key'), el('span', { text: '认证方式' })]),
        el('div.field', {}, [authSelect]),
        passwordField,
        keyField,
        passphraseField,
      ]),

      el('div.form-section', {}, [
        el('div.form-section-title', {}, [icon('settings'), el('span', { text: '选项' })]),
        el('label.check', { for: 'ssh-save-host' }, [
          saveHostCheck,
          el('span', { text: '保存该主机到本地列表（仅保存地址、端口、用户名与认证方式，不含任何凭据）' }),
        ]),
        el('label.check', { for: 'ssh-strict-key' }, [
          strictCheck,
          el('span', { text: '严格校验主机密钥指纹（默认首次连接自动信任）' }),
        ]),
      ]),

      formStatus,
    ]),
    el('div.panel-footer.col.gap-3', {}, [
      connectBtn,
      el('div.help.t-center', { text: '凭据仅存于服务端内存，会话结束即销毁，不写入磁盘与日志' }),
    ]),
  ]);

  /* ---------- 分隔条 ---------- */
  const splitter = el('div.splitter', {
    role: 'separator',
    tabindex: '0',
    'aria-orientation': 'vertical',
    'aria-label': '调整面板宽度（使用左右方向键）',
    'aria-valuemin': '320',
    'aria-valuemax': '620',
    'aria-valuenow': '380',
  });

  /* ---------- 右：终端区 ---------- */
  const sessionTabs = el('div.session-tabs', { role: 'tablist', 'aria-label': 'SSH 会话' });
  const termHost = el('div', { style: { flex: '1 1 auto', minHeight: '0', position: 'relative' } });
  const mobileKeys = el('div.mobile-keys', { 'aria-label': '终端辅助按键' });
  const termInfo = el('div.row.gap-3.wrap');
  const toolbarExtra = el('div.row.gap-1');

  const termToolbar = el('div.term-toolbar', {}, [
    termInfo,
    el('div.grow'),
    toolbarExtra,
  ]);

  const rightPanel = el('div.panel.panel--right.col', {}, [
    el('div.panel-header', {}, [
      el('h2', {}, [icon('activity'), el('span', { text: '终端' })]),
      el('div.panel-actions', {}, [
        el('button.btn.btn--sm.btn--ghost', {
          type: 'button',
          'data-tip': '清屏（Ctrl+L）',
          on: { click: () => sendToActive('\x0c') },
        }, [icon('refresh'), el('span', { text: '清屏' })]),
        el('button.btn.btn--sm.btn--ghost', {
          type: 'button',
          'data-tip': '满屏 / 退出全屏',
          on: { click: () => toggleFullscreen() },
        }, [icon('maximize'), el('span', { text: '全屏' })]),
      ]),
    ]),
    sessionTabs,
    termHost,
    termToolbar,
    mobileKeys,
  ]);

  container.append(leftPanel, splitter, rightPanel);

  /* ================= 初始化 ================= */
  renderHosts();
  renderTerminalPlaceholder();
  renderMobileKeys();
  renderToolbarExtra();

  const { attachSplitter } = await import('../ui.js');
  const { loadPanelWidth, savePanelWidth } = await import('../store.js');
  const savedWidth = loadPanelWidth();
  if (savedWidth) container.style.setProperty('--panel-w', `${savedWidth}px`);
  attachSplitter(splitter, container, {
    min: 320, max: 620, initial: savedWidth || 380, onChange: (w) => savePanelWidth(w),
  });

  state.hosts = loadSshHosts();

  // 预加载 xterm：用户进入本视图说明大概率要连接，提前拉取可消除首连延迟
  needXterm().then((lib) => { XtermLib = lib; }).catch((err) => {
    formStatus.append(el('div.alert.alert--danger', {}, [
      icon('alertCircle'),
      el('div.alert-body', {}, [
        el('div.alert-title', { text: '终端组件加载失败' }),
        el('div', { text: err.message }),
        el('div.t-xs', { text: '请确认 app/static/vendor/ 目录下的 xterm.js 与 xterm.css 已随包分发。' }),
      ]),
    ]));
  });

  /* ================= 主机列表 ================= */
  function renderHosts() {
    clear(hostsListEl);
    const hosts = state.hosts;

    if (hosts.length === 0) {
      hostsListEl.append(el('div.help', { text: '暂无已保存主机。填写表单后勾选「保存该主机」即可加入列表。' }));
      return;
    }

    for (const host of hosts) {
      hostsListEl.append(el('button.list-item', {
        type: 'button',
        on: {
          click: () => {
            hostInput.value = host.host;
            portInput.value = host.port || 22;
            userInput.value = host.username || '';
            authSelect.value = host.authMethod || 'password';
            authSelect.dispatchEvent(new Event('change'));
            // 焦点移到密码框，减少一次 Tab（这是最常见的下一步操作）
            (host.authMethod === 'key' ? keyInput : passInput).focus();
            toast('info', '已载入主机信息', `${host.username}@${host.host}:${host.port}，请补充凭据后连接`);
          },
        },
      }, [
        el('span.list-item-main', {}, [
          el('span.list-item-title', { text: host.name || host.host }),
          el('span.list-item-sub', { text: `${host.username}@${host.host}:${host.port} · ${host.authMethod === 'key' ? '私钥' : '密码'}` }),
        ]),
        el('button.btn.btn--sm.btn--icon.btn--ghost', {
          type: 'button',
          'aria-label': `删除主机 ${host.name || host.host}`,
          on: {
            click: async (e) => {
              e.stopPropagation();
              if (!(await confirmDialog({
                title: '删除已保存主机',
                message: `确定要移除 ${host.name || host.host} 吗？此操作仅删除本地记录，不影响服务器。`,
                confirmLabel: '删除',
                danger: true,
              }))) return;
              state.hosts = state.hosts.filter((h) => h.id !== host.id);
              saveSshHosts(state.hosts);
              renderHosts();
              toast('success', '已移除主机记录');
            },
          },
        }, [icon('trash')]),
      ]));
    }
  }

  /* ================= 终端占位 ================= */
  function renderTerminalPlaceholder() {
    clear(termHost);
    termHost.append(el('div.term-shell', { id: 'term-empty' }, [
      emptyState({
        iconName: 'terminal',
        title: '尚未建立连接',
        desc: '在左侧填写主机地址与凭据后点击「连接」。终端支持 vim、top 等全屏交互程序，以及中文 UTF-8 显示。',
      }),
    ]));
  }

  /* ================= 工具栏 ================= */
  function renderToolbarExtra() {
    clear(toolbarExtra);

    // 字号
    toolbarExtra.append(
      el('button.btn.btn--sm.btn--icon.btn--ghost', {
        type: 'button', 'aria-label': '减小字号', 'data-tip': '减小字号',
        on: { click: () => changeFont(-1) },
      }, [icon('chevronDown')]),
      el('button.btn.btn--sm.btn--icon.btn--ghost', {
        type: 'button', 'aria-label': '增大字号', 'data-tip': '增大字号',
        on: { click: () => changeFont(1) },
      }, [icon('chevronRight')]),
      el('div.sep-v'),
      el('button.btn.btn--sm.btn--ghost', {
        type: 'button', 'data-tip': '切换终端配色（明/暗）',
        on: {
          click: () => {
            state.termTheme = state.termTheme === 'dark' ? 'light' : 'dark';
            for (const session of state.sessions.values()) applyTermTheme(session);
            toast('info', `终端配色已切换为${state.termTheme === 'dark' ? '深色' : '浅色'}`);
          },
        },
      }, [icon(state.termTheme === 'dark' ? 'moon' : 'sun'), el('span', { text: state.termTheme === 'dark' ? '深色' : '浅色' })]),
      el('div.sep-v'),
      el('button.btn.btn--sm.btn--ghost', {
        type: 'button', 'data-tip': '复制选中内容（Ctrl+Shift+C）',
        on: { click: () => copySelection() },
      }, [icon('copy'), el('span', { text: '复制' })]),
      el('button.btn.btn--sm.btn--ghost', {
        type: 'button', 'data-tip': '从剪贴板粘贴（Ctrl+Shift+V）',
        on: { click: () => pasteFromClipboard() },
      }, [icon('copy'), el('span', { text: '粘贴' })]),
    );
  }

  /* ================= 连接 ================= */
  async function connect() {
    if (state.connecting) return;

    const config = {
      host: hostInput.value.trim(),
      port: Number(portInput.value) || 22,
      username: userInput.value.trim(),
      authMethod: authSelect.value,
    };

    // 校验：错误就地反馈到表单，而不是弹全局提示（就近原则）
    clear(formStatus);
    const errors = [];
    if (!config.host) errors.push('主机地址不能为空');
    if (!config.username) errors.push('用户名不能为空');
    if (config.port < 1 || config.port > 65535) errors.push('端口需在 1–65535 之间');
    if (config.authMethod === 'password' && !passInput.value) errors.push('请输入登录密码');
    if (config.authMethod === 'key' && !keyInput.value.trim()) errors.push('请粘贴私钥内容');

    if (errors.length > 0) {
      formStatus.append(el('div.alert.alert--danger', { role: 'alert' }, [
        icon('alertCircle'),
        el('div.alert-body', {}, [
          el('div.alert-title', { text: '请先修正以下问题' }),
          el('ul', { style: { margin: '0', paddingLeft: 'var(--sp-5)' } },
            errors.map((e) => el('li', { text: e }))),
        ]),
      ]));
      // 焦点移到第一个出错的输入框
      if (!config.host) hostInput.focus();
      else if (!config.username) userInput.focus();
      else if (config.authMethod === 'password' && !passInput.value) passInput.focus();
      else keyInput.focus();
      return;
    }

    state.connecting = true;
    connectBtn.classList.add('is-loading');
    connectBtn.disabled = true;
    const label = connectBtn.querySelector('span');
    if (label) label.textContent = '连接中…';
    clear(formStatus);
    formStatus.append(el('div.row.gap-2.t-sm.t-muted', {}, [
      icon('activity'),
      el('span', { text: `正在连接 ${config.username}@${config.host}:${config.port} …` }),
    ]));

    // 凭据单独持有，绝不放进会被序列化的对象
    const credential = config.authMethod === 'password'
      ? { password: passInput.value }
      : { privateKey: keyInput.value.trim(), passphrase: passphraseInput.value };

    try {
      const info = await sshApi.connect({
        ...config,
        ...credential,
        strictHostKey: strictCheck.checked,
      });

      openSession(info, config);
      clear(formStatus);

      if (saveHostCheck.checked) saveCurrentHost(config);

      // 连接成功后清空密码框，降低肩窥风险（私钥保留以便快速重连同名主机）
      passInput.value = '';
      passphraseInput.value = '';
    } catch (err) {
      const message = err instanceof ApiError ? err.message : String(err?.message || err);
      formStatus.append(el('div.alert.alert--danger', { role: 'alert' }, [
        icon('alertCircle'),
        el('div.alert-body', {}, [
          el('div.alert-title', { text: '连接失败' }),
          el('div', { text: message }),
          el('div.t-xs', {
            text: '排查建议：确认主机可达（ping / telnet 端口）、用户名与凭据正确、防火墙放行 22 端口；若为私钥认证，请确认私钥格式与口令。',
          }),
        ]),
      ]));
      toast('danger', 'SSH 连接失败', message);
    } finally {
      state.connecting = false;
      connectBtn.classList.remove('is-loading');
      connectBtn.disabled = false;
      if (label) label.textContent = '连接';
    }
  }

  function saveCurrentHost(config) {
    const name = `${config.username}@${config.host}`;
    // 同名同地址替换而非追加，避免列表膨胀
    state.hosts = state.hosts.filter((h) => !(h.host === config.host && h.username === config.username));
    state.hosts.unshift({
      id: uid('host'),
      name,
      host: config.host,
      port: config.port,
      username: config.username,
      authMethod: config.authMethod,
    });
    saveSshHosts(state.hosts);
    renderHosts();
    toast('success', '主机已保存', '仅保存地址与用户名，未保存任何凭据');
  }

  /* ================= 会话管理 ================= */
  async function openSession(info, config) {
    // 确保 xterm 就绪（首次连接时可能仍在加载）
    if (!XtermLib) {
      try {
        XtermLib = await needXterm();
      } catch (err) {
        toast('danger', '终端组件加载失败', err.message);
        return;
      }
    }

    if (!shellCssLoaded) {
      await ensureShellCss();
      shellCssLoaded = true;
    }

    clear(termHost);

    const session = {
      id: info.session_id,
      title: `${config.username}@${config.host}`,
      host: config.host,
      username: config.username,
      state: 'connected',
      cols: 80,
      rows: 24,
      startedAt: Date.now(),
      lastActivity: Date.now(),
      channel: null,
      fontSize: state.fontSizes[info.session_id] || 14,
      shellEl: el('div.term-shell', { id: `term-${info.session_id}`, 'data-term-theme': state.termTheme }),
    };

    termHost.append(session.shellEl);

    // --- 创建 xterm 实例 ---
    const term = new XtermLib.Terminal({
      fontFamily: 'ui-monospace, "Cascadia Mono", Consolas, "JetBrains Mono", monospace',
      fontSize: session.fontSize,
      lineHeight: 1.25,
      letterSpacing: 0,
      cursorBlink: true,
      cursorStyle: 'bar',
      scrollback: 8000,
      allowProposedApi: true,
      theme: TERM_THEMES[state.termTheme],
      // 中文等宽字符需要明确宽度，否则会出现列错位
      allowTransparency: true,
      convertEol: false,
    });
    session.term = term;

    const fitAddon = new XtermLib.FitAddon ? new XtermLib.FitAddon() : null;
    if (fitAddon) term.loadAddon(fitAddon);
    if (XtermLib.WebLinksAddon) term.loadAddon(new XtermLib.WebLinksAddon());
    session.fitAddon = fitAddon;

    term.open(session.shellEl);
    // xterm 的辅助 textarea 需要明确的无障碍标签，读屏用户才能识别输入区
    const helper = $('.xterm-helper-textarea', session.shellEl);
    if (helper) {
      helper.setAttribute('aria-label', `SSH 终端输入区：${session.title}`);
      helper.setAttribute('autocapitalize', 'off');
      helper.setAttribute('autocorrect', 'off');
      helper.setAttribute('spellcheck', 'false');
    }
    term.focus();

    // --- 组合键映射（FR-SSH-04）---
    term.attachCustomKeyEventHandler((event) => {
      if (event.type !== 'keydown') return true;

      const isMac = navigator.platform.toLowerCase().includes('mac');
      const mod = isMac ? event.metaKey : event.ctrlKey;

      // Ctrl+Shift+C / Ctrl+Shift+V：显式复制粘贴（不发送给远端）
      if (mod && event.shiftKey && (event.key === 'C' || event.key === 'c')) {
        copySelection();
        return false;
      }
      if (mod && event.shiftKey && (event.key === 'V' || event.key === 'v')) {
        pasteFromClipboard(session);
        return false;
      }
      // Ctrl+C：有选区时复制；无选区时放行，作为 SIGINT 透传给远端
      if (mod && !event.shiftKey && (event.key === 'c' || event.key === 'C')) {
        const selection = term.getSelection();
        if (selection) {
          copySelection();
          return false;
        }
        return true;
      }
      // Ctrl+V：交给 xterm 原生粘贴事件处理
      return true;
    });

    // --- 建立数据通道 ---
    const channel = sshApi.openChannel(info.session_id, {
      onData: (text) => {
        term.write(text);
        session.lastActivity = Date.now();
      },
      onClose: (closeInfo) => {
        session.state = 'closed';
        renderTabs();
        showDisconnectedOverlay(session, closeInfo);
      },
      onError: (err) => {
        toast('danger', '终端通道异常', err?.message || '连接中断');
      },
    });
    session.channel = channel;

    // 键盘输入 → 远端（含移动端 Ctrl 粘滞键转换）
    term.onData((data) => {
      session.lastActivity = Date.now();

      // 移动端辅助键：Ctrl 处于"粘滞"状态时，把下一个单字符转成控制字符
      // 例：Ctrl + c → 0x03（SIGINT），Ctrl + l → 0x0c（清屏）
      if (ctrlArmed) {
        ctrlArmed = false;
        ctrlBtn?.classList.remove('is-armed');
        ctrlBtn?.setAttribute('aria-pressed', 'false');
        if (data.length === 1) {
          const code = data.toUpperCase().charCodeAt(0);
          if (code >= 64 && code <= 95) {
            channel.send(String.fromCharCode(code - 64));
            return;
          }
        }
      }

      channel.send(data);
    });

    // --- 尺寸同步（FR-SSH-06）---
    const doFit = () => {
      if (!session.fitAddon || !document.contains(session.shellEl)) return;
      // 元素隐藏时（视图切换中）尺寸为 0，强行 fit 会导致 cols/rows 归零
      const rect = session.shellEl.getBoundingClientRect();
      if (rect.width < 40 || rect.height < 40) return;
      try {
        session.fitAddon.fit();
        channel.resize(term.cols, term.rows);
      } catch {
        /* 布局尚未稳定时忽略 */
      }
    };
    session.doFit = doFit;

    const ro = new ResizeObserver(debounce(doFit, 120));
    ro.observe(session.shellEl);
    session.resizeObserver = ro;

    requestAnimationFrame(doFit);

    state.sessions.set(info.session_id, session);
    state.activeId = info.session_id;
    renderTabs();
    renderTermInfo();
    activateSession(info.session_id);

    toast('success', '已连接', `${session.title}（主机指纹 ${String(info.host_key || '').slice(0, 32)}…）`);

    if (info.server_banner) {
      term.writeln(`\x1b[90m${info.server_banner}\x1b[0m`);
    }
  }

  function activateSession(sessionId) {
    state.activeId = sessionId;
    for (const [id, session] of state.sessions) {
      const isActive = id === sessionId;
      session.shellEl.style.display = isActive ? '' : 'none';
      if (isActive) requestAnimationFrame(() => { session.doFit?.(); session.term?.focus(); });
    }
    renderTabs();
    renderTermInfo();
  }

  function closeSession(sessionId, { silent = false } = {}) {
    const session = state.sessions.get(sessionId);
    if (!session) return;

    session.resizeObserver?.disconnect();
    session.channel?.close();
    session.term?.dispose();
    session.shellEl.remove();
    state.sessions.delete(sessionId);

    if (state.activeId === sessionId) {
      const remaining = [...state.sessions.keys()];
      state.activeId = remaining[0] || null;
      if (state.activeId) activateSession(state.activeId);
    }

    renderTabs();
    renderTermInfo();

    if (state.sessions.size === 0) renderTerminalPlaceholder();
    if (!silent) toast('info', '会话已断开', session.title);
  }

  function renderTabs() {
    clear(sessionTabs);
    if (state.sessions.size === 0) {
      sessionTabs.classList.add('hidden');
      return;
    }
    sessionTabs.classList.remove('hidden');

    for (const [id, session] of state.sessions) {
      const tab = el('div.session-tab', {
        role: 'tab',
        tabindex: state.activeId === id ? '0' : '-1',
        'aria-selected': String(state.activeId === id),
        'data-state': session.state,
        on: {
          click: () => activateSession(id),
          keydown: (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              activateSession(id);
            }
            // 左右方向键在标签间移动（WAI-ARIA tablist 模式）
            if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
              e.preventDefault();
              const keys = [...state.sessions.keys()];
              const idx = keys.indexOf(id);
              const next = keys[(idx + (e.key === 'ArrowRight' ? 1 : keys.length - 1)) % keys.length];
              activateSession(next);
              $(`[aria-selected="true"]`, sessionTabs)?.focus();
            }
          },
        },
      }, [
        el('span.session-state', { 'aria-hidden': 'true' }),
        el('span.t-clamp-1', { text: session.title, style: { maxWidth: '150px' } }),
        el('button.btn.btn--sm.btn--icon.btn--ghost.session-tab-close', {
          type: 'button',
          'aria-label': `断开 ${session.title}`,
          on: {
            click: async (e) => {
              e.stopPropagation();
              await sshApi.disconnect(session.id).catch(() => { /* 服务端已断开时忽略 */ });
              closeSession(id);
            },
          },
        }, [icon('close')]),
      ]);
      sessionTabs.append(tab);
    }

    sessionTabs.append(el('button.btn.btn--sm.btn--icon.btn--ghost', {
      type: 'button',
      'aria-label': '新建连接',
      'data-tip': '新建连接',
      style: { marginLeft: 'auto', alignSelf: 'center' },
      on: { click: () => hostInput.focus() },
    }, [icon('plus')]));
  }

  function renderTermInfo() {
    clear(termInfo);
    const session = state.sessions.get(state.activeId);
    if (!session) {
      termInfo.append(el('span', { text: '无活动会话' }));
      return;
    }
    termInfo.append(
      el('span', {}, [icon('server'), el('span', { text: `${session.host}` })]),
      el('span', {}, [icon('user'), el('span', { text: session.username })]),
      el('span', {}, [icon('clock'), el('span', {
        text: `已连接 ${Math.round((Date.now() - session.startedAt) / 60000)} 分钟`,
      })]),
      el('span', {}, [icon('hash'), el('span', { text: `${session.term?.cols || 80} × ${session.term?.rows || 24}` })]),
    );
  }

  /* ================= 断开遮罩 + 重连 ================= */
  function showDisconnectedOverlay(session, closeInfo) {
    if (!document.contains(session.shellEl)) return;

    const overlay = el('div.term-overlay', {}, [
      el('div.col.center.gap-3', {}, [
        icon('alertCircle', 'icon icon--xl'),
        el('div', { style: { fontWeight: 'var(--fw-medium)' }, text: '连接已断开' }),
        el('div.t-xs', {
          text: closeInfo?.reason
            ? `${closeInfo.reason}${closeInfo.code ? `（代码 ${closeInfo.code}）` : ''}`
            : '远端或网络中断了连接',
        }),
        el('div.row.gap-2.mt-2', {}, [
          el('button.btn.btn--sm', {
            type: 'button',
            on: { click: () => { overlay.remove(); recconnect(session); } },
          }, [icon('refresh'), el('span', { text: '重新连接' })]),
          el('button.btn.btn--sm.btn--ghost', {
            type: 'button',
            on: { click: () => { overlay.remove(); closeSession(session.id, { silent: true }); } },
          }, [icon('close'), el('span', { text: '关闭会话' })]),
        ]),
      ]),
    ]);
    session.shellEl.append(overlay);
  }

  async function recconnect(session) {
    // 重连需要凭据，而凭据已被会话释放 —— 从表单取当前值（用户此时通常已重新填好）
    const config = {
      host: session.host,
      port: Number(portInput.value) || 22,
      username: session.username,
      authMethod: authSelect.value,
    };
    const credential = authSelect.value === 'password'
      ? { password: passInput.value }
      : { privateKey: keyInput.value.trim(), passphrase: passphraseInput.value };

    if ((authSelect.value === 'password' && !credential.password)
      || (authSelect.value === 'key' && !credential.privateKey)) {
      toast('warning', '需要重新输入凭据', '出于安全考虑，凭据不会在会话间保留，请在左侧补充后重连');
      passInput.focus();
      return;
    }

    closeSession(session.id, { silent: true });
    try {
      const info = await sshApi.connect({ ...config, ...credential, strictHostKey: strictCheck.checked });
      openSession(info, config);
    } catch (err) {
      toast('danger', '重连失败', err?.message || '未知错误');
    }
  }

  /* ================= 复制 / 粘贴 ================= */
  async function copySelection() {
    const session = state.sessions.get(state.activeId);
    const text = session?.term?.getSelection();
    if (!text) {
      toast('info', '没有选中内容', '请先用鼠标在终端中选中文本');
      return;
    }
    const { copyText } = await import('../utils.js');
    const ok = await copyText(text);
    toast(ok ? 'success' : 'warning', ok ? '已复制到剪贴板' : '复制失败', ok ? '' : '浏览器可能限制了剪贴板访问权限');
  }

  async function pasteFromClipboard(session = state.sessions.get(state.activeId)) {
    if (!session) return;
    if (!navigator.clipboard?.readText) {
      toast('warning', '无法读取剪贴板', '请使用 Ctrl+V 或终端右键粘贴');
      return;
    }
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        session.channel?.send(text);
        session.term?.focus();
      }
    } catch {
      toast('warning', '无法读取剪贴板', '浏览器可能未授予剪贴板读取权限，请改用 Ctrl+V');
    }
  }

  /* ================= 辅助功能 ================= */
  function changeFont(delta) {
    const session = state.sessions.get(state.activeId);
    if (!session) {
      toast('info', '请先建立连接', '字号设置将在连接后生效');
      return;
    }
    const currentIndex = TERM_FONT_SIZES.indexOf(session.fontSize);
    const nextIndex = clamp(currentIndex + delta, 0, TERM_FONT_SIZES.length - 1);
    session.fontSize = TERM_FONT_SIZES[nextIndex];
    session.term.options.fontSize = session.fontSize;
    state.fontSizes[session.id] = session.fontSize;
    requestAnimationFrame(() => session.doFit?.());
    toast('info', `字号：${session.fontSize}px`, '', { duration: 1400 });
  }

  function applyTermTheme(session) {
    if (!session.term) return;
    session.term.options.theme = TERM_THEMES[state.termTheme];
    session.shellEl.dataset.termTheme = state.termTheme;
    renderToolbarExtra();
  }

  function sendToActive(data) {
    const session = state.sessions.get(state.activeId);
    if (!session) {
      toast('info', '请先建立连接');
      return;
    }
    session.channel?.send(data);
    session.term?.focus();
  }

  function toggleFullscreen() {
    const target = rightPanel;
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    } else {
      target.requestFullscreen?.().catch(() => {
        toast('warning', '无法进入全屏', '浏览器拒绝了全屏请求，请检查权限设置');
      });
    }
  }

  /* ================= 移动端虚拟辅助键（FR-SYS-07） ================= */
  function renderMobileKeys() {
    clear(mobileKeys);

    // Ctrl 为"粘滞键"：点一次点亮，随后输入的第一个字符会被转成控制键组合
    ctrlBtn = el('button.btn.btn--sm', {
      type: 'button',
      text: 'Ctrl',
      'aria-pressed': 'false',
      'aria-label': 'Ctrl 粘滞键：按下后再输入字母，会发送对应的控制键组合',
      on: {
        click: () => {
          ctrlArmed = !ctrlArmed;
          ctrlBtn.classList.toggle('is-armed', ctrlArmed);
          ctrlBtn.setAttribute('aria-pressed', String(ctrlArmed));
          if (ctrlArmed) toast('info', 'Ctrl 已就绪', '再输入一个字母即可发送组合键，例如 Ctrl + c', { duration: 2200 });
        },
      },
    });
    mobileKeys.append(ctrlBtn);

    const KEYS = [
      { label: 'Esc', seq: '\x1b', name: 'Esc' },
      { label: 'Tab', seq: '\t', name: 'Tab' },
      { label: '↑', seq: '\x1b[A', name: '方向上' },
      { label: '↓', seq: '\x1b[B', name: '方向下' },
      { label: '←', seq: '\x1b[D', name: '方向左' },
      { label: '→', seq: '\x1b[C', name: '方向右' },
      { label: '/', seq: '/', name: '斜杠' },
      { label: '-', seq: '-', name: '连字符' },
      { label: '|', seq: '|', name: '竖线' },
      { label: '~', seq: '~', name: '波浪号' },
    ];

    for (const key of KEYS) {
      mobileKeys.append(el('button.btn.btn--sm', {
        type: 'button',
        text: key.label,
        'aria-label': `发送 ${key.name} 键`,
        on: { click: () => sendToActive(key.seq) },
      }));
    }
  }

  /* ================= 空闲断开（FR-SSH-05） ================= */
  const idleTimer = setInterval(() => {
    const limit = state.idleLimitMin * 60 * 1000;
    const session = state.sessions.get(state.activeId);
    if (!session || session.state !== 'connected') return;
    const idle = Date.now() - session.lastActivity;
    if (idle > limit) {
      closeSession(session.id);
      toast('warning', '会话因长时间无操作已自动断开', `空闲超过 ${state.idleLimitMin} 分钟`);
    } else if (idle > limit - 60000 && !session.__idleWarned) {
      session.__idleWarned = true;
      toast('warning', '会话即将超时', `再空闲 1 分钟将自动断开，请点击终端任意位置继续使用`);
    }
  }, 30000);
  disposers.push(() => clearInterval(idleTimer));

  // 终端任意交互都算作活动
  container.addEventListener('keydown', () => {
    const session = state.sessions.get(state.activeId);
    if (session) { session.lastActivity = Date.now(); session.__idleWarned = false; }
  });

  // 会话计时展示刷新
  const infoTimer = setInterval(renderTermInfo, 30000);
  disposers.push(() => clearInterval(infoTimer));

  /* ================= 安全说明 ================= */
  function showSecurityDialog() {
    const body = el('div.col.gap-3');
    body.append(el('div.alert.alert--accent', {}, [
      icon('shield'),
      el('div.alert-body', {}, [
        el('div.alert-title', { text: '凭据处理方式' }),
        el('div', { text: '密码与私钥仅保存在服务端进程内存中，会话结束时立即销毁。服务端不会将凭据写入磁盘、数据库或日志。' }),
      ]),
    ]));

    const list = [
      ['传输加密', '生产环境必须启用 HTTPS/WSS（TLS 1.2+），否则凭据在链路中以明文传输。'],
      ['登录保护', '服务器版强制开启登录鉴权，且所有 WebSocket 连接必须先通过 Token 校验，禁止匿名连接。'],
      ['主机密钥', '默认首次连接自动信任并写入 known_hosts。高安全要求场景请勾选「严格校验指纹」。'],
      ['目标白名单', '管理员可在 config.toml 的 [security].allowed_hosts 中限制可连接的目标 IP 段。'],
      ['审计日志', '执行过的命令会记录到服务端 logs 目录（脱敏处理，不含密码）。'],
      ['使用边界', '请勿将本模块部署为公网跳板机。开启前务必配置 IP 白名单与二次认证。'],
    ];
    const dl = el('dl.kv');
    for (const [k, v] of list) {
      dl.append(el('dt', { text: k }), el('dd', { text: v }));
    }
    body.append(dl);

    modal({
      title: 'SSH 安全说明',
      size: 'wide',
      body,
      actions: [{ label: '我知道了', variant: 'primary' }],
    });
  }

  /* ================= xterm 基础样式补充 ================= */
  function ensureShellCss() {
    // xterm.css 由 loader 注入；这里补充终端容器的主题背景
    if (document.getElementById('term-theme-css')) return Promise.resolve();
    const style = el('style', { id: 'term-theme-css' });
    style.textContent = `
      .term-shell[data-term-theme="light"] { background: #ffffff; }
      .term-shell[data-term-theme="dark"] { background: #0b1017; }
      .term-shell .xterm-viewport { scrollbar-width: thin; }
    `;
    document.head.append(style);
    return Promise.resolve();
  }

  /* ================= 订阅：视图被隐藏时暂停尺寸计算 ================= */
  disposers.push(subscribe(['theme'], () => { /* 终端主题独立于应用主题，此处不联动 */ }));

  /* ================= 生命周期 ================= */
  return {
    onShow() {
      // 视图重新显示时重新 fit（此前因 display:none 导致尺寸为 0）
      for (const session of state.sessions.values()) {
        requestAnimationFrame(() => session.doFit?.());
      }
      renderTermInfo();
    },
    async unmount() {
      for (const dispose of disposers) {
        try { dispose(); } catch { /* 忽略 */ }
      }
      disposers.length = 0;

      // 关键：必须主动断开全部会话，否则服务端会残留线程与 channel（AC-11 资源回收）
      for (const session of state.sessions.values()) {
        try {
          session.resizeObserver?.disconnect();
          session.channel?.close();
          session.term?.dispose();
        } catch { /* 忽略单个会话的清理异常 */ }
      }
      state.sessions.clear();

      // 清除表单中的凭据，避免在内存中长期驻留
      passInput.value = '';
      keyInput.value = '';
      passphraseInput.value = '';
    },
  };
}
