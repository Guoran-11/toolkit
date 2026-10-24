/**
 * store.js — 轻量状态中心（订阅/发布）
 *
 * 为什么不引框架：需求硬性要求原生 JS。这里用 40 行实现的极简 store
 * 满足「跨视图共享状态（任务列表、鉴权、主题）」的需求，且零运行时成本。
 *
 * 设计：
 * - state 为单一可变对象，通过 setState(patch) 更新
 * - 订阅粒度按「切片名」划分，避免任务列表变动触发 SSH 视图重渲染
 */

import { CFG } from './config.js';
import { clamp } from './utils.js';

/* ------------------------------------------------------------------ *
 * 1. 初始状态
 * ------------------------------------------------------------------ */

/**
 * 读取持久化的主题偏好。
 *
 * 必须在 applyTheme 之前调用，否则会出现「刷新后主题被重置」的 bug：
 * state.theme 的初始值是 'auto'，而 theme-boot.js 已经按 localStorage 设好了
 * <html data-theme>；若此时用 'auto' 再应用一次，就会把用户选定的深色主题
 * 覆盖回系统偏好。这是一个真实发生过的缺陷，故在此显式说明。
 */
function readStoredTheme() {
  try {
    const stored = localStorage.getItem(CFG.storageKeys.theme);
    if (stored && ['auto', 'light', 'dark'].includes(stored)) return stored;
  } catch {
    /* 隐私模式：无法读取，退回默认值 */
  }
  return CFG.defaultTheme;
}

const state = {
  /** 主题：'auto' | 'light' | 'dark'（auto 表示跟随系统） */
  theme: readStoredTheme(),
  /** 系统当前偏好，用于 auto 模式解析 */
  systemDark: false,

  /** 鉴权 */
  auth: {
    required: false,
    loggedIn: false,
    token: null,
    user: null,
  },

  /** 后端是否可用（false 时为演示模式） */
  backend: {
    online: false,
    checked: false,
    version: null,
    diskFreeBytes: null,
    diskWarning: false,
  },

  /** 引擎自检结果 */
  engines: {
    libreoffice: { state: 'unknown', version: null, path: null, hint: null },
    chromium:    { state: 'unknown', version: null, path: null, hint: null },
    ffmpeg:      { state: 'unknown', version: null, path: null, hint: null },
  },

  /** 任务中心：task_id → task 对象 */
  tasks: {},
  /** 任务 ID 顺序（新任务在前） */
  taskOrder: [],

  /** 转换中心：当前选择的源格式 */
  convertSource: null,
  /** 音频模块：免责声明确认状态 */
  audioGateAccepted: false,
  /** SSH：已保存主机（不含凭据） */
  sshHosts: [],
  /** SSH：当前活动会话 ID */
  activeSession: null,
};

/* ------------------------------------------------------------------ *
 * 2. 订阅机制
 * ------------------------------------------------------------------ */
/** @type {Map<string, Set<Function>>} */
const listeners = new Map();
const ALL = '__all__';

/**
 * 订阅状态变化。
 * @param {string|string[]} slices 关注的切片名数组，'*' 表示任意变化
 * @param {(state, changed) => void} handler
 * @returns {() => void} 取消订阅函数
 */
export function subscribe(slices, handler) {
  const keys = [].concat(slices);
  for (const key of keys) {
    if (!listeners.has(key)) listeners.set(key, new Set());
    listeners.get(key).add(handler);
  }
  return () => {
    for (const key of keys) listeners.get(key)?.delete(handler);
  };
}

function emit(changed) {
  const notified = new Set();
  const targets = [...(listeners.get(ALL) || []), ...changed.flatMap((k) => [...(listeners.get(k) || [])])];
  for (const handler of targets) {
    if (notified.has(handler)) continue; // 同一 handler 被多个切片命中时只调用一次
    notified.add(handler);
    try {
      handler(state, changed);
    } catch (err) {
      // 单个订阅者异常不应阻断其它订阅者（隔离故障）
      console.error('[store] 订阅回调异常：', err);
    }
  }
}

/** 不可变读取入口（避免外部直接改 state） */
export const getState = () => state;

/**
 * 更新状态。仅做浅合并；深层对象（如 engines）由调用方自行构造完整对象。
 * @param {object} patch
 */
export function setState(patch) {
  const changed = [];
  for (const [key, value] of Object.entries(patch)) {
    if (state[key] === value) continue;
    state[key] = value;
    changed.push(key);
  }
  if (changed.length > 0) emit(changed);
}

/* ------------------------------------------------------------------ *
 * 3. 主题
 * ------------------------------------------------------------------ */
const mq = window.matchMedia('(prefers-color-scheme: dark)');
state.systemDark = mq.matches;

/** 把 'auto' 解析为具体主题 */
export const resolveTheme = (theme = state.theme) => (
  theme === 'auto' ? (state.systemDark ? 'dark' : 'light') : theme
);

/** 应用主题到 <html>，并同步 highlight.js 皮肤 */
export function applyTheme(theme = state.theme) {
  const resolved = resolveTheme(theme);
  document.documentElement.setAttribute('data-theme', resolved);

  // meta theme-color：影响移动端浏览器地址栏配色
  let meta = document.querySelector('meta[name="theme-color"]');
  if (!meta) {
    meta = document.createElement('meta');
    meta.name = 'theme-color';
    document.head.append(meta);
  }
  meta.content = resolved === 'dark' ? '#0c1218' : '#f4f6fa';

  // 需要时同步代码高亮皮肤（动态导入避免循环依赖）
  import('./loader.js').then(({ switchHighlightTheme }) => switchHighlightTheme(resolved)).catch(() => {});
  return resolved;
}

/** 显式设置主题并持久化 */
export function setTheme(theme) {
  const next = ['auto', 'light', 'dark'].includes(theme) ? theme : 'auto';
  try {
    localStorage.setItem(CFG.storageKeys.theme, next);
  } catch {
    /* 隐私模式下 localStorage 可能抛异常，忽略即可，不影响功能 */
  }
  setState({ theme: next });
  applyTheme(next);
}

/** 系统偏好变化（auto 模式需要实时响应） */
mq.addEventListener('change', (e) => {
  state.systemDark = e.matches;
  if (state.theme === 'auto') applyTheme('auto');
  emit(['systemDark']);
});

/* ------------------------------------------------------------------ *
 * 4. 鉴权
 * ------------------------------------------------------------------ */
export function setAuth(patch) {
  Object.assign(state.auth, patch);
  emit(['auth']);
}

export function saveToken(token) {
  try {
    if (token) localStorage.setItem(CFG.storageKeys.token, token);
    else localStorage.removeItem(CFG.storageKeys.token);
  } catch { /* 忽略 */ }
  state.auth.token = token ?? null;
  state.auth.loggedIn = Boolean(token);
}

export function loadToken() {
  try {
    const token = localStorage.getItem(CFG.storageKeys.token);
    state.auth.token = token;
    state.auth.loggedIn = Boolean(token);
    return token;
  } catch {
    return null;
  }
}

/* ------------------------------------------------------------------ *
 * 5. 任务中心
 * ------------------------------------------------------------------ */
const TASK_LIMIT = 200; // 前端仅保留最近 200 条，防止长跑后内存膨胀（AC-11）

/** 新增或更新任务 */
export function upsertTask(task) {
  if (!task?.task_id) return;
  const isNew = !state.tasks[task.task_id];
  state.tasks[task.task_id] = { ...state.tasks[task.task_id], ...task };

  if (isNew) {
    state.taskOrder.unshift(task.task_id);
    if (state.taskOrder.length > TASK_LIMIT) {
      const dropped = state.taskOrder.splice(TASK_LIMIT);
      for (const id of dropped) delete state.tasks[id];
    }
  }
  emit(['tasks']);
}

/** 批量写入（列表接口返回时使用，避免 N 次 emit） */
export function replaceTasks(list) {
  state.tasks = {};
  state.taskOrder = [];
  for (const task of list) {
    state.tasks[task.task_id] = task;
    state.taskOrder.push(task.task_id);
  }
  state.taskOrder = state.taskOrder.slice(0, TASK_LIMIT);
  emit(['tasks']);
}

export function removeTask(taskId) {
  if (!state.tasks[taskId]) return;
  delete state.tasks[taskId];
  state.taskOrder = state.taskOrder.filter((id) => id !== taskId);
  emit(['tasks']);
}

export function clearTasks() {
  state.tasks = {};
  state.taskOrder = [];
  emit(['tasks']);
}

/** 按创建时间倒序的任务数组 */
export const getTasks = () => state.taskOrder.map((id) => state.tasks[id]).filter(Boolean);

/** 进行中的任务（用于顶栏徽标与状态栏进度） */
export const getActiveTasks = () => getTasks().filter((t) => t.status === 'pending' || t.status === 'running');

/* ------------------------------------------------------------------ *
 * 6. UI 偏好
 * ------------------------------------------------------------------ */
export function loadPanelWidth() {
  let width = parseInt(localStorage.getItem(CFG.storageKeys.panelWidth) || '', 10);
  if (!Number.isFinite(width) || width <= 0) width = null;
  return width;
}

export function savePanelWidth(px) {
  const clamped = clamp(Math.round(px), 320, 620);
  try {
    localStorage.setItem(CFG.storageKeys.panelWidth, String(clamped));
  } catch { /* 忽略 */ }
  return clamped;
}

export function loadSshHosts() {
  try {
    const raw = localStorage.getItem(CFG.storageKeys.savedHosts);
    const list = raw ? JSON.parse(raw) : [];
    state.sshHosts = Array.isArray(list) ? list : [];
  } catch {
    state.sshHosts = [];
  }
  return state.sshHosts;
}

/** 保存主机信息（严格排除凭据字段，NFR-SSH-01） */
export function saveSshHosts(hosts) {
  const sanitized = hosts.map((h) => ({
    id: h.id,
    name: h.name,
    host: h.host,
    port: h.port,
    username: h.username,
    authMethod: h.authMethod,
    // 注意：password / privateKey / passphrase 一律不落盘
  }));
  state.sshHosts = sanitized;
  try {
    localStorage.setItem(CFG.storageKeys.savedHosts, JSON.stringify(sanitized));
  } catch { /* 忽略 */ }
  emit(['sshHosts']);
  return sanitized;
}

export function loadAudioGate() {
  try {
    state.audioGateAccepted = localStorage.getItem(CFG.storageKeys.audioGateAccepted) === '1';
  } catch {
    state.audioGateAccepted = false;
  }
  return state.audioGateAccepted;
}

export function setAudioGate(accepted) {
  state.audioGateAccepted = Boolean(accepted);
  try {
    if (accepted) localStorage.setItem(CFG.storageKeys.audioGateAccepted, '1');
    else localStorage.removeItem(CFG.storageKeys.audioGateAccepted);
  } catch { /* 忽略 */ }
  emit(['audioGateAccepted']);
}

export { state };
