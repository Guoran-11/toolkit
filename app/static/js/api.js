/**
 * api.js — 统一 API 客户端
 *
 * 职责：
 * 1. 屏蔽「真实后端」与「演示模式（mock）」的差异，视图层只调用同一套方法。
 * 2. 统一错误语义：所有异常都归一化为 ApiError，携带 status 与用户可读 message。
 * 3. 携带鉴权 Token；401 时清除本地凭据并广播（由 app.js 弹出登录框）。
 * 4. 上传走 XHR 以获得真实进度事件（fetch 至今无法上报请求体上传进度）。
 */

import { CFG, runtime } from './config.js';
import { mock, getMockFile } from './mock.js';
import { loadToken, saveToken, setAuth, setState } from './store.js';

/* ------------------------------------------------------------------ *
 * 1. 错误类型
 * ------------------------------------------------------------------ */
export class ApiError extends Error {
  constructor(message, { status = 0, code = null, detail = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

/** 把任意异常/响应归一化为用户可读的 ApiError */
function toApiError(err, fallbackStatus = 0) {
  if (err instanceof ApiError) return err;

  if (err instanceof TypeError) {
    // fetch 在网络失败、DNS 失败、被 CSP 拦截时抛 TypeError
    return new ApiError('无法连接到服务端，请确认服务已启动或检查网络', { status: 0, code: 'NETWORK' });
  }
  if (err?.name === 'AbortError') {
    return new ApiError('请求已取消', { status: 0, code: 'ABORTED' });
  }
  return new ApiError(err?.message || '请求失败，请稍后重试', { status: fallbackStatus, code: err?.code });
}

/**
 * 按接口路径取体积上限（MB）：音频模块有独立且更宽松的上限（config.toml [limits]）。
 * 仅用于 413 的兜底文案——后端返回 JSON detail 时以 detail 为准。
 */
function limitMbFor(path) {
  return String(path || '').includes('/audio/')
    ? (CFG.limits.maxAudioUploadMb ?? CFG.limits.maxUploadMb)
    : CFG.limits.maxUploadMb;
}

/** 解析后端错误体：FastAPI 的 {"detail": "..."} 或 {"detail":[{"msg":"..."}]} */
async function parseErrorBody(res, path = '') {
  let message = `请求失败（HTTP ${res.status}）`;
  let code = null;
  let detail = null;
  try {
    const data = await res.json();
    detail = data;
    if (typeof data?.detail === 'string') message = data.detail;
    else if (Array.isArray(data?.detail)) {
      message = data.detail.map((d) => d?.msg || JSON.stringify(d)).join('；');
    } else if (typeof data?.message === 'string') message = data.message;
    code = data?.code ?? null;
  } catch {
    // 非 JSON 响应（网关错误页等），保留默认文案
    if (res.status === 413) message = `文件体积超出服务端限制（${limitMbFor(path)} MB）`;
    if (res.status === 429) message = '任务队列已满，请稍后重试';
    if (res.status === 401) message = '登录状态已失效，请重新登录';
  }
  return { message, code, detail };
}

/* ------------------------------------------------------------------ *
 * 2. 基础 fetch 封装
 * ------------------------------------------------------------------ */
const url = (path) => `${CFG.apiBase}${path}`;

/** 401 广播：由 app.js 订阅后弹出登录框（避免 api 层反向依赖 UI 层） */
const authExpiredHandlers = new Set();
export const onAuthExpired = (fn) => {
  authExpiredHandlers.add(fn);
  return () => authExpiredHandlers.delete(fn);
};

async function request(path, { method = 'GET', body = null, headers = {}, isForm = false, signal } = {}) {
  const finalHeaders = { Accept: 'application/json', ...headers };
  const token = loadToken();
  if (token) finalHeaders.Authorization = `Bearer ${token}`;

  let payload = body;
  if (body && !isForm && typeof body === 'object') {
    finalHeaders['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(url(path), { method, headers: finalHeaders, body: payload, signal });
  } catch (err) {
    throw toApiError(err);
  }

  if (res.status === 401) {
    // 仅当「原本确实持有会话」时才广播失效：否则在登录页输错一次密码，
    // 也会被当成「登录状态已失效」再弹一次提示，属于误导。
    const hadSession = Boolean(loadToken());
    saveToken(null);
    setAuth({ loggedIn: false, token: null, user: null });
    if (hadSession) for (const fn of authExpiredHandlers) fn();
    const { message } = await parseErrorBody(res, path);
    throw new ApiError(message, { status: 401, code: 'UNAUTHORIZED' });
  }

  if (!res.ok) {
    const { message, code, detail } = await parseErrorBody(res, path);
    throw new ApiError(message, { status: res.status, code, detail });
  }

  if (res.status === 204) return null;
  const type = res.headers.get('content-type') || '';
  return type.includes('application/json') ? res.json() : res.blob();
}

/**
 * 带进度回调的上传。
 * @param {string} path
 * @param {File[]} files
 * @param {object} fields 附加字段（全部转为字符串）
 * @param {(loaded:number, total:number, percent:number)=>void} [onProgress]
 * @returns {Promise<any>}
 */
function upload(path, files, fields = {}, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const file of files) form.append('files', file, file.name);
    for (const [key, value] of Object.entries(fields)) {
      form.append(key, typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value));
    }

    const xhr = new XMLHttpRequest();
    xhr.open('POST', url(path), true);
    xhr.responseType = 'json';

    const token = loadToken();
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);

    if (onProgress) {
      xhr.upload.addEventListener('progress', (e) => {
        if (!e.lengthComputable) return;
        onProgress(e.loaded, e.total, Math.round((e.loaded / e.total) * 100));
      });
    }

    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr.response ?? null);
        return;
      }
      if (xhr.status === 401) {
        const hadSession = Boolean(loadToken());
        saveToken(null);
        setAuth({ loggedIn: false, token: null, user: null });
        if (hadSession) for (const fn of authExpiredHandlers) fn();
      }
      const detail = xhr.response?.detail;
      const message = typeof detail === 'string' ? detail
        : xhr.status === 413 ? `文件体积超出服务端限制（${limitMbFor(path)} MB）`
          : `上传失败（HTTP ${xhr.status}）`;
      reject(new ApiError(message, { status: xhr.status, code: xhr.response?.code ?? null }));
    });

    xhr.addEventListener('error', () => reject(new ApiError('上传过程中网络中断，请重试', { status: 0, code: 'NETWORK' })));
    xhr.addEventListener('abort', () => reject(new ApiError('上传已取消', { status: 0, code: 'ABORTED' })));
    xhr.addEventListener('timeout', () => reject(new ApiError('上传超时，请检查网络或缩小文件体积', { status: 0, code: 'TIMEOUT' })));

    xhr.send(form);
  });
}

/** 把 Blob 触发为浏览器下载（统一处理文件名与 URL 回收） */
function deliverBlob(blob, filename) {
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objectUrl;
  a.download = filename || 'download';
  a.rel = 'noopener';
  a.style.display = 'none';
  document.body.append(a);
  a.click();
  a.remove();
  // 延迟回收：过早 revoke 会导致大文件下载被中断
  setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
}

/* ------------------------------------------------------------------ *
 * 3. 启动探测与模式闸门
 * ------------------------------------------------------------------ */

/**
 * 模式就绪闸门。
 *
 * 问题背景：为了让首屏不被网络探测阻塞，probeBackend() 是在路由启动**之后**
 * 才异步执行的；但用户可以在这 1~1.5 秒内就切到「任务中心」等视图，
 * 此时 runtime.demo 仍是初始的 false，这些请求会打到并不存在的后端上，
 * 产生一串 404，界面上表现为「引擎自检为空」「任务列表为空」。
 *
 * 解法：所有业务请求先 await 这道闸门。探测一结束闸门即放行，
 * 因此既保持了首屏速度，又保证请求一定走正确的通道。
 */
let releaseModeGate = null;
const modeGate = new Promise((resolve) => { releaseModeGate = resolve; });

/** 兜底上限：即使探测逻辑本身卡住，也不允许业务请求无限期挂起 */
const MODE_GATE_TIMEOUT_MS = 4000;
setTimeout(() => {
  if (releaseModeGate) {
    console.warn('[api] 后端探测超时，已按演示模式放行请求');
    releaseModeGate();
    releaseModeGate = null;
  }
}, MODE_GATE_TIMEOUT_MS);

function openModeGate() {
  releaseModeGate?.();
  releaseModeGate = null;
}

/** 等待运行模式确定（'live' 还是 'demo'） */
export const whenModeReady = () => modeGate;

/**
 * 在正确的通道上执行一次调用。
 *
 * 这是本文件的核心约定：**任何** 业务方法都必须经由它分发，
 * 不允许在方法内部直接判断 runtime.demo —— 否则就会出现"分支判断早于闸门放行"的竞态。
 *
 * @param {() => Promise<any>} demoFn 演示模式实现
 * @param {() => Promise<any>} liveFn 直连后端实现
 */
async function dispatch(demoFn, liveFn) {
  await modeGate;
  return runtime.demo ? demoFn() : liveFn();
}

/**
 * 探测后端可用性。成功 → live 模式；失败 → demo 模式（降级而非报错）。
 * @returns {Promise<{online:boolean, info:object|null}>}
 */
export async function probeBackend() {
  try {
    return await probeBackendInner();
  } finally {
    // 无论成功、失败还是走了 demo 短路分支，都必须放行闸门
    openModeGate();
  }
}

async function probeBackendInner() {
  if (CFG.mode === 'demo') {
    runtime.demo = true;
    setState({
      backend: { online: false, checked: true, version: CFG.app.version, diskFreeBytes: null, diskWarning: false },
      engines: normalizeEngines(null),
    });
    return { online: false, info: null };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CFG.probe.timeoutMs);

  try {
    const res = await fetch(url(CFG.probe.path), {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const info = await res.json();

    runtime.demo = false;
    runtime.serverVersion = info.version ?? null;

    setState({
      backend: {
        online: true,
        checked: true,
        version: info.version ?? null,
        diskFreeBytes: info.disk_free_bytes ?? null,
        diskWarning: typeof info.disk_free_bytes === 'number' && info.disk_free_bytes < 1024 ** 3,
      },
      engines: normalizeEngines(info.engines),
    });

    // 服务端据此决定前端是否展示登录页；这里如实跟随，不做单向黏滞
    setAuth({ required: Boolean(info.auth_required) });
    return { online: true, info };
  } catch {
    // 探测失败不是错误：自动进入演示模式，保证界面永远可用
    runtime.demo = CFG.mode !== 'live';
    setState({
      backend: { online: false, checked: true, version: null, diskFreeBytes: null, diskWarning: false },
      engines: normalizeEngines(null),
    });
    if (CFG.mode === 'live') {
      throw new ApiError('已配置为直连模式，但服务端不可达', { status: 0, code: 'OFFLINE' });
    }
    return { online: false, info: null };
  } finally {
    clearTimeout(timer);
  }
}

/** 后端只返回字符串状态时也要能渲染，统一补齐结构 */
function normalizeEngines(raw) {
  const keys = ['libreoffice', 'chromium', 'ffmpeg'];
  const out = {};
  for (const key of keys) {
    const value = raw?.[key];
    if (typeof value === 'string') out[key] = { state: value, version: null, path: null, hint: null };
    else if (value && typeof value === 'object') {
      out[key] = {
        state: value.state || (value.available ? 'ok' : 'fail'),
        version: value.version ?? null,
        path: value.path ?? null,
        hint: value.hint ?? null,
      };
    } else {
      out[key] = { state: 'unknown', version: null, path: null, hint: null };
    }
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * 4. 业务 API
 * ------------------------------------------------------------------ */
export const api = {
  /* ---------- 系统 ---------- */
  /** 健康检查（探针自身，不走闸门） */
  healthz: () => probeBackend(),

  engines: () => dispatch(
    () => mock.engines(),
    async () => {
      const data = await request('/api/system/engines');
      if (data?.engines) setState({ engines: normalizeEngines(data.engines) });
      return data;
    },
  ),

  /* ---------- 鉴权 ---------- */
  async login({ username, password, code }) {
    const data = await dispatch(
      () => mock.login({ username, password }),
      () => request('/api/auth/login', { method: 'POST', body: { username, password, code: code || '' } }),
    );
    saveToken(data.token);
    setAuth({ loggedIn: true, token: data.token, user: data.user ?? { username } });
    return data;
  },

  logout() {
    // 登出端点只做审计留痕（Token 无状态）；在清 Token 之前调用才能带上凭据。
    // 失败不影响本地登出——本地清理后界面一定会回到登录页。
    if (!runtime.demo) request('/api/auth/logout', { method: 'POST' }).catch(() => {});
    saveToken(null);
    setAuth({ loggedIn: false, token: null, user: null });
  },

  /* ---------- 账号体系（注册 / 验证码 / 找回密码） ---------- */

  /**
   * 公开的鉴权配置：是否开放注册、是否需要验证码、邮件是否已配置。
   * 演示模式下返回一份保守的默认值（不开放注册）。
   */
  authConfig: () => dispatch(
    () => Promise.resolve({
      auth_required: false,
      allow_registration: false,
      require_email_verification: false,
      login_require_code: false,
      min_password_length: 8,
      code_ttl_sec: 300,
      code_resend_sec: 60,
      email_configured: false,
      sms_configured: false,
    }),
    () => request('/api/auth/config'),
  ),

  /** 拉取当前登录身份（用于刷新页面后恢复用户名/角色） */
  me: () => dispatch(
    () => Promise.resolve({ user: null, auth_required: false, local: true, is_superadmin: false }),
    () => request('/api/auth/me'),
  ),

  /** 发送邮箱/短信验证码 */
  sendCode({ target, channel = 'email', purpose = 'register' }) {
    return dispatch(
      () => { throw new ApiError('演示模式下无法发送验证码，请连接真实服务端。', { status: 0, code: 'DEMO_MODE' }); },
      () => request('/api/auth/send-code', { method: 'POST', body: { target, channel, purpose } }),
    );
  },

  /** 邮箱注册（成功后自动登录） */
  async register({ email, password, code, displayName }) {
    const data = await dispatch(
      () => { throw new ApiError('演示模式下无法注册，请连接真实服务端。', { status: 0, code: 'DEMO_MODE' }); },
      () => request('/api/auth/register', {
        method: 'POST',
        body: { email, password, code: code || '', display_name: displayName || '' },
      }),
    );
    if (data?.token) {
      saveToken(data.token);
      setAuth({ loggedIn: true, token: data.token, user: data.user ?? { username: email } });
    }
    return data;
  },

  /** 用邮箱验证码重置密码 */
  resetPassword({ email, code, newPassword }) {
    return dispatch(
      () => { throw new ApiError('演示模式下无法重置密码，请连接真实服务端。', { status: 0, code: 'DEMO_MODE' }); },
      () => request('/api/auth/reset-password', {
        method: 'POST',
        body: { email, code, new_password: newPassword },
      }),
    );
  },

  /** 修改当前账号密码（成功后后端会返回新 Token，这里同步刷新本地凭据） */
  async changePassword({ oldPassword, newPassword }) {
    const data = await dispatch(
      () => { throw new ApiError('演示模式下无法修改密码，请连接真实服务端。', { status: 0, code: 'DEMO_MODE' }); },
      () => request('/api/auth/password', {
        method: 'POST',
        body: { old_password: oldPassword, new_password: newPassword },
      }),
    );
    if (data?.token) saveToken(data.token);
    return data;
  },

  /* ---------- 账号管理（仅管理员） ---------- */

  admin: {
    listUsers: ({ page = 1, pageSize = 50, search = '' } = {}) => dispatch(
      () => Promise.resolve({ total: 0, page: 1, page_size: pageSize, items: [] }),
      () => request(`/api/admin/users?page=${page}&pageSize=${pageSize}&search=${encodeURIComponent(search)}`),
    ),

    updateUser: (userId, patch) => dispatch(
      () => { throw new ApiError('演示模式下无法管理账号。', { status: 0, code: 'DEMO_MODE' }); },
      () => request(`/api/admin/users/${encodeURIComponent(userId)}`, { method: 'PATCH', body: patch }),
    ),

    removeUser: (userId) => dispatch(
      () => { throw new ApiError('演示模式下无法管理账号。', { status: 0, code: 'DEMO_MODE' }); },
      () => request(`/api/admin/users/${encodeURIComponent(userId)}`, { method: 'DELETE' }),
    ),

    revokeTokens: (userId) => dispatch(
      () => { throw new ApiError('演示模式下无法管理账号。', { status: 0, code: 'DEMO_MODE' }); },
      () => request(`/api/admin/users/${encodeURIComponent(userId)}/revoke-tokens`, { method: 'POST' }),
    ),
  },

  /* ---------- 转换中心 ---------- */
  /** 拉取后端支持矩阵（服务端会依据引擎可用性动态禁用某些路径） */
  matrix: () => dispatch(
    () => mock.matrix(),
    () => request('/api/convert/matrix'),
  ),

  /**
   * 提交转换任务。
   * @param {File[]} files
   * @param {{source:string, target:string, params:object}} options
   * @param {(loaded:number,total:number,percent:number)=>void} [onProgress]
   * @returns {Promise<object[]>} 任务数组
   */
  convert(files, options, onProgress) {
    return dispatch(
      async () => {
        // 演示模式模拟上传耗时，让进度条真实可见
        if (onProgress) {
          const total = files.reduce((sum, f) => sum + f.size, 0);
          for (let i = 1; i <= 10; i += 1) {
            // eslint-disable-next-line no-await-in-loop
            await new Promise((r) => setTimeout(r, 60));
            onProgress(Math.round((total * i) / 10), total, i * 10);
          }
        }
        return mock.convert(files, options);
      },
      async () => {
        const data = await upload('/api/convert', files, {
          source_format: options.source,
          target_format: options.target,
          params: JSON.stringify(options.params ?? {}),
        }, onProgress);
        return Array.isArray(data) ? data : [data];
      },
    );
  },

  /* ---------- 任务 ---------- */
  tasks: {
    list: (params = {}) => dispatch(
      () => mock.listTasks(params),
      () => {
        const qs = new URLSearchParams(
          Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''),
        ).toString();
        return request(`/api/tasks${qs ? `?${qs}` : ''}`);
      },
    ),

    get: (taskId) => dispatch(
      () => mock.getTask(taskId),
      () => request(`/api/tasks/${encodeURIComponent(taskId)}`),
    ),

    cancel: (taskId) => dispatch(
      () => mock.cancelTask(taskId),
      () => request(`/api/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' }),
    ),

    remove: (taskId) => dispatch(
      () => mock.deleteTask(taskId),
      () => request(`/api/tasks/${encodeURIComponent(taskId)}?purge=true`, { method: 'DELETE' }),
    ),
  },

  /**
   * 下载产物并保存到本地。
   * 说明：正式环境也可以直接把 output.url 交给 <a download>，
   * 这里统一走 Blob 是为了统一错误处理（文件过期时能弹出友好提示而不是白屏）。
   */
  async download(fileId, filename) {
    const blob = await dispatch(
      () => mock.download(fileId),
      () => request(`/api/download/${encodeURIComponent(fileId)}`),
    );
    deliverBlob(blob, filename);
    return blob;
  },

  /**
   * 取可被浏览器原生消费的 URL（用于 <audio src> / <iframe src>）。
   * 演示模式返回 objectURL；直连模式返回后端下载地址。
   */
  fileUrl(fileId) {
    if (runtime.demo) {
      const file = getMockFile(fileId);
      return file ? URL.createObjectURL(file.blob) : '';
    }
    return url(`/api/download/${encodeURIComponent(fileId)}`);
  },

  /** 批量打包下载 */
  async batchZip(taskIds, filename = 'toolkit-batch.zip') {
    const blob = await dispatch(
      () => mock.batchZip(taskIds),
      () => request('/api/convert/batch-zip', { method: 'POST', body: { task_ids: taskIds } }),
    );
    deliverBlob(blob, filename);
    return blob;
  },
};

/* ------------------------------------------------------------------ *
 * 5. 音频模块 API
 * ------------------------------------------------------------------ */
export const audioApi = {
  /** 嗅探格式（不落长期盘，快速返回） */
  probe: (files) => dispatch(
    () => mock.audioProbe(files),
    () => {
      const form = new FormData();
      for (const file of files) form.append('files', file, file.name);
      return request('/api/audio/probe', { method: 'POST', body: form, isForm: true });
    },
  ),

  convert(files, options, onProgress) {
    return dispatch(
      async () => {
        if (onProgress) {
          for (let i = 1; i <= 8; i += 1) {
            // eslint-disable-next-line no-await-in-loop
            await new Promise((r) => setTimeout(r, 55));
            onProgress(i / 8, 1, i * 12);
          }
        }
        return mock.audioConvert(files, options);
      },
      async () => {
        const data = await upload('/api/audio/convert', files, {
          output_format: options.outputFormat,
          bitrate: options.bitrate ?? '',
          sample_rate: options.sampleRate ?? '',
          channels: options.channels ?? '',
          keep_metadata: options.keepMetadata ?? true,
        }, onProgress);
        return Array.isArray(data) ? data : [data];
      },
    );
  },
};

/* ------------------------------------------------------------------ *
 * 6. 视频下载模块 API
 *
 * 与后端契约（docs/视频下载模块-实现契约.md §3）严格对应：
 *   - 所有方法都经 dispatch 走「演示模式 / 直连后端」双通道；
 *   - parse 的逐条失败不抛异常，而是返回 {ok:false, error:{code,detail}}；
 *   - history 的筛选参数同时接受 camelCase（pageSize）与 snake_case。
 * ------------------------------------------------------------------ */
export const videoApi = {
  /** 平台清单、开关、Cookie 配置摘要与引擎状态 */
  platforms: () => dispatch(
    () => mock.videoPlatforms(),
    () => request('/api/video/platforms'),
  ),

  /**
   * 解析链接（不下载）。
   * @param {string[]} urls 已由前端提取并去重的链接数组
   * @param {string} [preferQuality] 仅为提示后端默认清晰度，最终以 item.default_format_id 为准
   * @returns {Promise<{results:object[], elapsed_ms:number}>}
   */
  parse: (urls, preferQuality) => dispatch(
    () => mock.videoParse(urls, preferQuality),
    () => request('/api/video/parse', {
      method: 'POST',
      body: { urls, prefer_quality: preferQuality || '' },
    }),
  ),

  /**
   * 提交下载任务。
   * @param {Array<object>} items 每项：{url, platform, item_id, format_id, output, title, author, quality}
   * @returns {Promise<object[]>} 任务数组（字段同 TaskRecord.to_api()）
   */
  download: (items) => dispatch(
    () => mock.videoDownload(items),
    () => request('/api/video/download', { method: 'POST', body: { items } }),
  ),

  /** 历史记录（分页 + 平台筛选 + 标题关键字） */
  history: (params = {}) => dispatch(
    () => mock.videoHistory(params),
    () => {
      const qs = new URLSearchParams(
        Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''),
      ).toString();
      return request(`/api/video/history${qs ? `?${qs}` : ''}`);
    },
  ),

  /** 删除单条历史（不删产物） */
  removeHistory: (itemId, platform) => dispatch(
    () => mock.removeVideoHistory(itemId, platform),
    () => request(
      `/api/video/history/${encodeURIComponent(itemId)}?platform=${encodeURIComponent(platform || '')}`,
      { method: 'DELETE' },
    ),
  ),

  /** 清空历史（不删产物） */
  clearHistory: () => dispatch(
    () => mock.clearVideoHistory(),
    () => request('/api/video/history', { method: 'DELETE' }),
  ),

  /** 读取设置（Cookie 只返回脱敏值） */
  settings: () => dispatch(
    () => mock.videoSettings(),
    () => request('/api/video/settings'),
  ),

  /** 更新设置（部分字段；cookies 为空字符串表示清除） */
  updateSettings: (patch) => dispatch(
    () => mock.videoUpdateSettings(patch),
    () => request('/api/video/settings', { method: 'PUT', body: patch }),
  ),
};

/* ------------------------------------------------------------------ *
 * 7. Markdown 模块 API
 * ------------------------------------------------------------------ */
export const mdApi = {
  /** 导出为 PDF / HTML（PDF 走 Playwright 通道，复用转换模块的渲染引擎） */
  export: (payload) => dispatch(
    () => mock.mdExport(payload),
    () => request('/api/md/export', { method: 'POST', body: payload }),
  ),
};

/* ------------------------------------------------------------------ *
 * 8. SSH 模块 API
 * ------------------------------------------------------------------ */
export const sshApi = {
  connect: (config) => dispatch(
    () => mock.sshConnect(config),
    () => request('/api/ssh/connect', { method: 'POST', body: config }),
  ),

  disconnect: (sessionId) => dispatch(
    () => mock.sshDisconnect(sessionId),
    () => request(`/api/ssh/${encodeURIComponent(sessionId)}`, { method: 'DELETE' }),
  ),

  hosts: () => dispatch(
    () => Promise.resolve({ items: [] }),
    () => request('/api/ssh/hosts'),
  ),

  sessions: () => dispatch(
    () => mock.sshSessions(),
    () => request('/api/ssh/sessions'),
  ),

  /**
   * 打开终端数据通道。
   *
   * 注意：这是唯一一个**同步**返回的接口，因此不能走 dispatch 闸门。
   * 安全性论证：本方法只会在 sshApi.connect() 成功之后被调用，
   * 而 connect 已经 await 过闸门，所以此处读取 runtime.demo 一定是最终值。
   *
   * @param {string} sessionId
   * @param {{onData:(text:string)=>void, onClose:(info:object)=>void, onOpen?:Function, onError?:Function}} handlers
   * @returns {{send:(d:string)=>void, resize:(c:number,r:number)=>void, close:()=>void}}
   */
  openChannel(sessionId, handlers) {
    if (runtime.demo) return mock.createSshChannel(sessionId, handlers);

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${window.location.host}${url(`/ws/ssh/${encodeURIComponent(sessionId)}`)}`);
    ws.binaryType = 'arraybuffer';

    const decoder = new TextDecoder('utf-8');

    ws.addEventListener('open', () => handlers.onOpen?.());
    ws.addEventListener('message', (event) => {
      if (typeof event.data === 'string') {
        handlers.onData?.(event.data);
        return;
      }
      // 二进制帧按 UTF-8 解码（后端约定：文本以字节流下发，降低 base64 开销）
      handlers.onData?.(decoder.decode(new Uint8Array(event.data), { stream: true }));
    });
    ws.addEventListener('error', () => handlers.onError?.(new ApiError('终端通道异常', { status: 0, code: 'WS_ERROR' })));
    ws.addEventListener('close', (event) => {
      handlers.onClose?.({ code: event.code, reason: event.reason || '连接已关闭' });
    });

    return {
      send(data) {
        if (ws.readyState === WebSocket.OPEN) ws.send(new TextEncoder().encode(data));
      },
      resize(cols, rows) {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'resize', cols, rows }));
      },
      close() {
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) ws.close(1000, 'client closed');
      },
    };
  },
};

/* ------------------------------------------------------------------ *
 * 9. 任务轮询器
 * ------------------------------------------------------------------ */
/**
 * 轮询单个任务直至终态。
 * @param {string} taskId
 * @param {{onUpdate?:(task:object)=>void, intervalMs?:number, timeoutMs?:number, signal?:AbortSignal}} [opts]
 * @returns {Promise<object>} 终态任务
 */
export function pollTask(taskId, opts = {}) {
  const interval = opts.intervalMs ?? CFG.pollIntervalMs;
  const timeout = opts.timeoutMs ?? CFG.limits.pollTimeoutMs;

  return new Promise((resolve, reject) => {
    const startedAt = Date.now();
    let timer = null;

    const stop = () => { clearTimeout(timer); opts.signal?.removeEventListener('abort', onAbort); };
    const onAbort = () => {
      stop();
      reject(new ApiError('轮询已取消', { status: 0, code: 'ABORTED' }));
    };
    opts.signal?.addEventListener('abort', onAbort, { once: true });

    const tick = async () => {
      if (opts.signal?.aborted) return;
      try {
        const task = await api.tasks.get(taskId);
        opts.onUpdate?.(task);

        if (task.status === 'success' || task.status === 'failed' || task.status === 'cancelled') {
          stop();
          resolve(task);
          return;
        }
        if (Date.now() - startedAt > timeout) {
          stop();
          // 超时不视为失败：任务可能仍在服务端运行，提示用户去任务中心查看
          reject(new ApiError('等待时间超过 5 分钟，任务可能仍在后台执行，请到「任务中心」查看', {
            status: 0, code: 'POLL_TIMEOUT', detail: task,
          }));
          return;
        }
        timer = setTimeout(tick, interval);
      } catch (err) {
        stop();
        reject(toApiError(err));
      }
    };

    tick();
  });
}
