/**
 * views/video.js — 模块六：短视频作品下载
 *
 * 覆盖需求：FR-VD-01 ~ FR-VD-11（见 docs/视频下载模块-PRD-v1.0.md §4 / §6）
 *   - 链接输入：整段分享文案提取、粘贴、回车/按钮触发、300ms 防抖、20 条前端拦截
 *   - 解析结果：封面 / 标题 / 作者 / 时长 / 平台 / 清晰度 / 预估体积 / 下载
 *   - 部分成功：ok=false 且带 item 时降级可用，并提示「去设置」配置 Cookie
 *   - 批量：多结果勾选 → 批量下载；多个成功任务 → 打包 ZIP
 *   - 下载：POST 后立即禁用、1s 轮询、进度、失败重试、TTL 相对时间
 *   - 历史抽屉：平台筛选、标题搜索、单条删除、清空、复用标记
 *   - 设置弹层：默认清晰度/格式、并发、单文件上限、平台开关、Cookie（脱敏回显）
 *   - 风控/限流：黄色横幅 + 倒计时（retry_after 从 ApiError.detail 读取）
 *
 * 合规约束（§10）：
 *   - 只下载用户有权访问的公开作品，仅供个人学习研究与离线备份；
 *   - 不提供去水印/破解/绕过去付费能力，会员清晰度只展示不下载；
 *   - Cookie 只回显脱敏值，明文不写入日志、不进入任何展示文案。
 */

import { CFG, runtime } from '../config.js';
import {
  el, clear, formatBytes, formatDuration, formatRelative, formatTime,
  debounce, uid,
} from '../utils.js';
import { icon } from '../icons.js';
import { videoApi, api, ApiError, pollTask } from '../api.js';
import {
  toast, modal, confirmDialog, createProgress, statusPill, emptyState, tag,
} from '../ui.js';
import { upsertTask, getTasks } from '../store.js';

/* ------------------------------------------------------------------ *
 * 常量
 * ------------------------------------------------------------------ */

/** 平台展示名（与后端 platform 字段一一对应） */
const PLATFORM_META = {
  douyin: { label: '抖音' },
  bilibili: { label: 'B站' },
  kuaishou: { label: '快手' },
};

/**
 * 域名白名单：仅用于「前端预校验」，避免明显不支持的链接消耗服务端限流配额。
 * 唯一可信来源仍是后端 adapters/registry.py，这里不接受子域通配。
 */
const SUPPORTED_HOSTS = {
  douyin: ['douyin.com', 'www.douyin.com', 'v.douyin.com', 'iesdouyin.com', 'www.iesdouyin.com'],
  bilibili: ['bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 'b23.tv'],
  kuaishou: ['kuaishou.com', 'www.kuaishou.com', 'v.kuaishou.com', 'm.kuaishou.com'],
};

/** 与后端契约 §3.2 相同的链接提取正则；组尾的中文标点由 normalizeUrl 清理 */
const URL_PATTERN = /https?:\/\/[^\s"'<>（）()，。！？【】]+/g;

/** 清晰度归一化排序（下标越小越高） */
const QUALITY_RANK = ['4K', '2K', '1080P60', '1080P', '720P', '540P', '480P', '360P', 'audio'];

/** 默认清晰度候选（与后端 prefer_quality 枚举一致） */
const QUALITY_OPTIONS = [
  { value: '4K', label: '4K（体积最大，多为会员专属）' },
  { value: '1080P', label: '1080P（推荐）' },
  { value: '720P', label: '720P' },
  { value: '480P', label: '480P' },
];

const FORMAT_LABELS = {
  mp4: 'MP4 视频（兼容性最好）',
  mp3: 'MP3 音频（仅音频作品）',
  m4a: 'M4A 音频（仅音频作品）',
};

/** 演示用示例文案：三条链接分别对应「成功 / 需登录 / 解析失败」三种状态 */
const SAMPLE_TEXT = [
  '7.43 复制打开抖音，看看【山野来信】的作品 https://v.douyin.com/demo123/',
  'https://www.bilibili.com/video/BV1xx411c7mD',
  'https://v.kuaishou.com/demo456 老街的清晨',
].join('\n');

/** 页脚合规声明（需求原文，不可省略） */
const COMPLIANCE_TEXT = '本功能仅用于下载你有权访问的公开作品，供个人学习、研究与离线备份使用；'
  + '下载内容请勿用于商业用途或二次传播。产物默认保留 24 小时。';

/** Cookie 合规提示（§4.9.7 原文） */
const COOKIE_NOTICE = '请使用你本人账号的 Cookie，共享/租借账号违反平台协议。';

/* ------------------------------------------------------------------ *
 * 纯函数工具
 * ------------------------------------------------------------------ */

const platformLabel = (name) => PLATFORM_META[name]?.label || name || '未知平台';

/** 清晰度排序值（未知等级排在最后） */
function qualityRank(quality) {
  const index = QUALITY_RANK.indexOf(quality);
  return index < 0 ? QUALITY_RANK.length : index;
}

/** 去掉尾部标点并把 http 归一化为 https（FR-VD-01 第 3/7 条） */
function normalizeUrl(raw) {
  return String(raw || '')
    .replace(/[.,;:!?，。；：！？、】」』"'）)]+$/g, '')
    .replace(/^http:\/\//i, 'https://');
}

/** 从任意文本中提取并去重链接 */
function extractUrls(text) {
  const found = String(text || '').match(URL_PATTERN) || [];
  const seen = new Set();
  const out = [];
  for (const candidate of found) {
    const url = normalizeUrl(candidate);
    if (!/^https?:\/\//i.test(url) || seen.has(url)) continue;
    seen.add(url);
    out.push(url);
  }
  return out;
}

/** 按 host 精确匹配平台；未命中返回 null */
function platformOfUrl(url) {
  let host = '';
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch {
    return null;
  }
  for (const [name, hosts] of Object.entries(SUPPORTED_HOSTS)) {
    if (hosts.includes(host)) return name;
  }
  return null;
}

/** ApiError / 普通 Error 统一取用户可读文案 */
function errorText(err) {
  if (err instanceof ApiError) return err.message;
  return String(err?.message || err || '请求失败');
}

/** 从错误体里取 retry_after（后端 extra 平铺在 body 顶层） */
function retryAfterOf(err) {
  const detail = err?.detail;
  return Number(
    detail?.retry_after ?? detail?.retryAfter ?? detail?.detail?.retry_after ?? 0,
  ) || 0;
}

/** 秒 → mm:ss（风控倒计时） */
function formatCountdown(seconds) {
  const total = Math.max(0, Math.ceil(Number(seconds) || 0));
  const mm = String(Math.floor(total / 60)).padStart(2, '0');
  const ss = String(total % 60).padStart(2, '0');
  return `${mm}:${ss}`;
}

/** 数字千分位 */
const formatCount = (value) => Number(value || 0).toLocaleString('zh-CN');

/** 文件名清洗（仅用于历史下载时拼文件名，服务端有自己的命名规则） */
function safeFileName(text) {
  return String(text || 'video').replace(/[\\/:*?"<>|\u0000-\u001f]/g, '').slice(0, 120) || 'video';
}

/* ------------------------------------------------------------------ *
 * 视图工厂
 * ------------------------------------------------------------------ */

export default async function createVideoView(container) {
  /* ---------------- 状态 ---------------- */
  const state = {
    platforms: null,
    settings: null,
    results: [],
    jobs: [],
    parsing: false,
    submitting: false,
    moduleDisabled: false,
    parseError: null,
    loginHint: false,
    risk: null,
  };

  const abortController = new AbortController();
  let riskTimer = null;
  let ttlTimer = null;
  let historyCleanup = null;
  const disposers = [];

  // 用 classList 增删而非赋值 className：路由依赖容器上的 `is-active` 控制可见性
  container.classList.remove('view--split');
  container.classList.add('view', 'view--single');
  container.innerHTML = '';

  /* ================= 顶部：平台状态 ================= */
  const platformChips = el('div.video-platform-chips', { role: 'status', 'aria-label': '平台与登录态' });

  /* ================= 输入区 ================= */
  const inputEl = el('textarea.textarea.video-input', {
    id: 'video-links',
    rows: 4,
    spellcheck: 'false',
    placeholder: `粘贴抖音 / B站 / 快手作品链接，或整段分享文案（每行一条，单次最多 20 条）\n例如：\n${SAMPLE_TEXT}`,
    'aria-label': '视频作品链接输入框',
  });

  const linkCount = el('span.t-xs.t-muted', { text: '尚未识别到链接' });
  const parseLabel = el('span', { text: '解析' });

  const parseBtn = el('button.btn.btn--primary', {
    type: 'button',
    on: { click: () => debouncedParse() },
  }, [icon('video'), parseLabel]);

  const pasteBtn = el('button.btn', {
    type: 'button',
    title: '读取剪贴板并填入输入框（不会自动解析）',
    on: { click: () => pasteFromClipboard() },
  }, [icon('clipboard'), el('span', { text: '粘贴' })]);

  const sampleBtn = el('button.btn.btn--sm.btn--ghost', {
    type: 'button',
    on: { click: () => fillSample() },
  }, [icon('link'), el('span', { text: '填入示例' })]);

  const inputSection = el('section.video-card', { 'aria-labelledby': 'video-input-title' }, [
    el('div.video-card-head', {}, [
      el('div.col.gap-1.grow', {}, [
        el('div.video-card-title#video-input-title', {}, [icon('video'), el('span', { text: '作品链接' })]),
        el('div.t-xs.t-muted', {
          text: '支持整段分享文案与多行粘贴 · 回车或点「解析」才会提交 · 单次最多 20 条',
        }),
      ]),
      sampleBtn,
    ]),
    inputEl,
    el('div.video-input-bar', {}, [
      pasteBtn,
      parseBtn,
      el('div.grow'),
      linkCount,
    ]),
  ]);

  /* ================= 提示条（登录 / 风控 / 请求失败） ================= */
  const alertHost = el('div.col.gap-2');

  /* ================= 解析结果 ================= */
  const resultCount = el('span.t-sm.t-muted');
  const batchBar = el('div.video-batchbar', { hidden: true });
  const resultsHost = el('div.col.gap-3');

  const resultsSection = el('section.video-card', { 'aria-labelledby': 'video-results-title' }, [
    el('div.video-card-head', {}, [
      el('div.video-card-title#video-results-title', {}, [icon('listTree'), el('span', { text: '解析结果' })]),
      el('div.grow'),
      resultCount,
    ]),
    batchBar,
    resultsHost,
  ]);

  /* ================= 最近任务 ================= */
  const taskCount = el('span.t-sm.t-muted');
  const zipBtn = el('button.btn.btn--sm', {
    type: 'button',
    disabled: true,
    title: '把至少 2 个成功任务的结果打包为 ZIP',
    on: { click: () => downloadAllZip() },
  }, [icon('archive'), el('span', { text: '打包下载' })]);

  const tasksHost = el('div.col.gap-2');

  const tasksSection = el('section.video-card', { 'aria-labelledby': 'video-tasks-title' }, [
    el('div.video-card-head', {}, [
      el('div.video-card-title#video-tasks-title', {}, [icon('tasks'), el('span', { text: '最近任务' })]),
      el('div.grow'),
      taskCount,
      zipBtn,
    ]),
    tasksHost,
  ]);

  /* ================= 页脚合规声明 ================= */
  const footer = el('footer.video-compliance', {}, [
    icon('shield'),
    el('div.col.gap-1', {}, [
      el('div', { text: COMPLIANCE_TEXT }),
      el('div.t-xs.t-muted', {
        text: '清晰度以平台实际提供的为准，不承诺无水印或原画质；遇到平台风控请稍后再试。',
      }),
    ]),
  ]);

  /* ================= 外壳 ================= */
  const panel = el('div.panel', {}, [
    el('div.panel-header', {}, [
      el('h2', {}, [icon('video'), el('span', { text: '视频下载' })]),
      el('div.panel-actions', {}, [
        el('button.btn.btn--sm.btn--ghost', {
          type: 'button',
          title: '查看平台开关、Cookie 与引擎状态',
          on: { click: () => openPlatformStatus() },
        }, [icon('activity'), el('span', { text: '平台状态' })]),
        el('button.btn.btn--sm.btn--ghost', {
          type: 'button',
          title: '下载历史（可筛选、搜索、删除）',
          on: { click: () => openHistory() },
        }, [icon('history'), el('span', { text: '历史' })]),
        el('button.btn.btn--sm', {
          type: 'button',
          on: { click: () => openSettings() },
        }, [icon('settings'), el('span', { text: '设置' })]),
      ]),
    ]),
    el('div.panel-body.video-body', {}, [
      platformChips,
      inputSection,
      alertHost,
      resultsSection,
      tasksSection,
      footer,
    ]),
  ]);

  container.append(panel);

  /* ================= 事件与防抖 ================= */
  // 300ms 防抖：回车连击 / 解析按钮双击都只发一次请求（FR-VD-01 第 4 条）
  const debouncedParse = debounce(() => { parseLinks(); }, 300);
  disposers.push(() => debouncedParse.cancel());

  inputEl.addEventListener('input', () => updateLinkCount());
  inputEl.addEventListener('keydown', (event) => {
    // 输入法组合中（中文候选词）按回车不应触发解析
    if (event.isComposing) return;
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      debouncedParse();
    }
  });

  /* ------------------------------------------------------------------ *
   * 1. 输入区交互
   * ------------------------------------------------------------------ */

  function updateLinkCount() {
    const urls = extractUrls(inputEl.value);
    const max = CFG.limits.maxVideoUrls || 20;
    if (urls.length === 0) {
      linkCount.textContent = inputEl.value.trim() ? '没有识别到作品链接' : '尚未识别到链接';
      linkCount.classList.remove('t-danger');
      return;
    }
    linkCount.textContent = `已识别 ${urls.length} / ${max} 条链接`;
    linkCount.classList.toggle('t-danger', urls.length > max);
  }

  function updateParseButton() {
    parseBtn.disabled = state.parsing || state.moduleDisabled;
    parseBtn.classList.toggle('is-loading', state.parsing);
    parseLabel.textContent = state.parsing ? '解析中…' : '解析';
    pasteBtn.disabled = state.parsing;
  }

  /** 粘贴只填充、不提交：避免剪贴板里残留内容自动触发请求 */
  async function pasteFromClipboard() {
    if (!navigator.clipboard?.readText) {
      toast('warning', '当前浏览器不支持读取剪贴板', '请手动粘贴（Ctrl+V）到输入框');
      return;
    }
    try {
      const text = await navigator.clipboard.readText();
      if (!text || !text.trim()) {
        toast('info', '剪贴板为空', '请先复制作品链接或整段分享文案');
        return;
      }
      const current = inputEl.value.trim();
      inputEl.value = current ? `${current}\n${text.trim()}` : text.trim();
      updateLinkCount();
      inputEl.focus();
      toast('success', '已填入剪贴板内容', '按回车或点「解析」开始解析');
    } catch {
      // 非安全上下文 / 用户拒绝授权 / 浏览器策略限制，都只提示、不抛异常
      toast('warning', '无法读取剪贴板', '浏览器拒绝了剪贴板访问，请手动粘贴（Ctrl+V）');
    }
  }

  function fillSample() {
    const current = inputEl.value.trim();
    inputEl.value = current ? `${current}\n${SAMPLE_TEXT}` : SAMPLE_TEXT;
    updateLinkCount();
    inputEl.focus();
  }

  /* ------------------------------------------------------------------ *
   * 2. 解析
   * ------------------------------------------------------------------ */

  function preferQuality() {
    return state.settings?.prefer_quality || '1080P';
  }

  function normalizeResult(raw, index) {
    const item = raw?.item || null;
    const result = {
      key: uid('vr'),
      index,
      url: raw?.url || item?.url || '',
      ok: Boolean(raw?.ok),
      error: raw?.error || null,
      item,
      checked: true,
      formatId: '',
    };
    if (item) {
      if (item.media_type === 'image') {
        result.formatId = '';
      } else {
        // 后端推荐的 default_format_id 优先（后端保证是有媒体地址的项）；
        // 若推荐值缺失或恰好落在不可用项上，再退回本地挑选逻辑
        const formats = Array.isArray(item.formats) ? item.formats : [];
        const recommended = item.default_format_id
          ? formats.find((f) => f.format_id === item.default_format_id
            && !f.need_login && !f.need_vip && f.available !== false)
          : null;
        result.formatId = recommended ? recommended.format_id : pickDefaultFormat(item);
      }
    }
    return result;
  }

  /** 清晰度项是否可用于下载（需登录 / 会员 / 平台未返回媒体地址的占位项都不算） */
  function isFormatUsable(format) {
    return Boolean(format)
      && !format.need_login
      && !format.need_vip
      && format.available !== false;
  }

  /**
   * 默认清晰度（契约 §4.2）：
   *   1. 后端推荐的 item.default_format_id 若可用 → 用它；
   *   2. 否则用户设置的默认清晰度若可用 → 用它；
   *   3. 否则取 ≤1080P 的最高可用档（按 QUALITY_RANK 下标升序，下标越小画质越高）；
   *   4. 绝不默认 4K（除非用户显式把默认清晰度设为 4K）。
   *   注：available === false 表示平台未返回媒体地址的占位项，任何情况下都不能作为默认值。
   */
  function pickDefaultFormat(item) {
    const formats = Array.isArray(item.formats) ? item.formats : [];
    if (formats.length === 0) return '';
    const usable = formats.filter(isFormatUsable);
    if (usable.length === 0) return '';

    const prefer = preferQuality();
    // 1. 后端推荐值优先（保证有媒体地址）
    let chosen = item.default_format_id
      ? usable.find((f) => f.format_id === item.default_format_id)
      : null;
    // 2. 用户偏好
    if (!chosen) chosen = usable.find((f) => f.quality === prefer);
    // 3. ≤1080P 的最高可用：不依赖数组顺序，按画质从高到低排序后取第一
    if (!chosen) {
      chosen = usable
        .filter((f) => qualityRank(f.quality) >= qualityRank('1080P'))
        .sort((a, b) => qualityRank(a.quality) - qualityRank(b.quality))[0] || null;
    }
    // 4. 兜底：非 4K 的最高可用
    if (!chosen) {
      chosen = usable
        .filter((f) => f.quality !== '4K')
        .sort((a, b) => qualityRank(a.quality) - qualityRank(b.quality))[0] || usable[0];
    }
    // 5. 绝不默认 4K（除非用户显式偏好 4K）
    if (chosen && chosen.quality === '4K' && prefer !== '4K') {
      const capped = usable
        .filter((f) => qualityRank(f.quality) >= qualityRank('1080P'))
        .sort((a, b) => qualityRank(a.quality) - qualityRank(b.quality));
      chosen = capped[0] || usable.find((f) => f.quality !== '4K') || chosen;
    }
    return chosen?.format_id || '';
  }

  async function parseLinks() {
    if (state.parsing || state.moduleDisabled) return;
    const raw = inputEl.value;
    if (!raw.trim()) {
      // 空输入不发请求（FR-VD-01 第 4 条）
      toast('warning', '请先粘贴作品链接', '支持整段分享文案，未输入时不会发起请求');
      inputEl.focus();
      return;
    }

    const urls = extractUrls(raw);
    if (urls.length === 0) {
      toast('warning', '没有识别到作品链接', '请粘贴完整分享文案或链接');
      return;
    }

    const max = CFG.limits.maxVideoUrls || 20;
    if (urls.length > max) {
      // 前端拦截：不消耗服务端限流配额
      toast('warning', `单次最多解析 ${max} 条链接`, `当前 ${urls.length} 条，请分批粘贴后解析`);
      return;
    }

    const supported = urls.filter((url) => platformOfUrl(url));
    const skipped = urls.length - supported.length;
    if (supported.length === 0) {
      toast('warning', '暂不支持该平台', '当前支持抖音、B站、快手');
      return;
    }
    if (skipped > 0) {
      toast('info', `已跳过 ${skipped} 条不支持的链接`, '当前支持抖音、B站、快手', { duration: 3200 });
    }

    state.parsing = true;
    state.parseError = null;
    state.loginHint = false;
    state.results = [];
    updateParseButton();
    renderResults();
    renderAlerts();

    try {
      const data = await videoApi.parse(supported, preferQuality());
      const list = Array.isArray(data?.results) ? data.results : [];
      state.results = list.map((rawItem, index) => normalizeResult(rawItem, index));

      if (state.results.length === 0) {
        toast('warning', '没有解析到作品', '请确认链接是否仍然有效');
      } else {
        const okCount = state.results.filter((r) => r.ok).length;
        const partialCount = state.results.filter((r) => !r.ok && r.item).length;
        const failCount = state.results.length - okCount - partialCount;
        const parts = [`${okCount} 成功`];
        if (partialCount > 0) parts.push(`${partialCount} 需登录`);
        if (failCount > 0) parts.push(`${failCount} 失败`);
        const elapsed = Number(data?.elapsed_ms);
        const elapsedText = Number.isFinite(elapsed) && elapsed > 0 ? `，耗时 ${(elapsed / 1000).toFixed(1)} 秒` : '';
        toast(
          okCount > 0 ? 'success' : 'warning',
          `解析完成（${parts.join(' / ')}）`,
          `共 ${state.results.length} 条${elapsedText}`,
        );
      }
      state.loginHint = state.results.some((r) => !r.ok && r.item
        && ['LOGIN_REQUIRED', 'COOKIE_INVALID'].includes(r.error?.code));
    } catch (err) {
      handleApiError(err, '解析失败');
    } finally {
      state.parsing = false;
      updateParseButton();
      renderResults();
      renderAlerts();
    }
  }

  /** 单条失败重试：有 item 直接重新提交下载；无 item 只重解析这一条 */
  async function retryResult(result) {
    if (result.item) {
      await submitResults([result]);
      return;
    }
    try {
      const data = await videoApi.parse([result.url], preferQuality());
      const fresh = Array.isArray(data?.results) ? data.results[0] : null;
      if (!fresh) {
        toast('warning', '仍没有解析结果', '请确认链接是否仍然有效');
        return;
      }
      const next = normalizeResult(fresh, result.index);
      const index = state.results.indexOf(result);
      if (index >= 0) state.results.splice(index, 1, next);
      else state.results.push(next);
      renderResults();
      renderAlerts();
      if (next.ok) toast('success', '重新解析成功', '现在可以下载了');
      else toast('warning', '仍然解析失败', next.error?.detail || '请稍后重试');
    } catch (err) {
      handleApiError(err, '重新解析失败');
    }
  }

  /* ------------------------------------------------------------------ *
   * 3. 提示条：登录 / 风控 / 请求失败
   * ------------------------------------------------------------------ */

  function renderAlerts() {
    clear(alertHost);

    if (state.risk) {
      const remain = Math.max(0, Math.ceil((state.risk.until - Date.now()) / 1000));
      alertHost.append(el('div.alert.alert--warning.video-risk', { role: 'alert' }, [
        icon('alertTriangle'),
        el('div.alert-body.grow', {}, [
          el('div.alert-title', {
            text: state.risk.code === 'RATE_LIMITED' ? '操作太频繁，已触发限流' : '平台风控中，暂时无法解析',
          }),
          el('div', {}, [
            el('span', { text: `${state.risk.message} ` }),
            el('strong', { text: `剩余 ${formatCountdown(remain)}` }),
          ]),
        ]),
        el('span.t-xs.t-mono.t-muted', { text: state.risk.code || '' }),
      ]));
    }

    const loginNeeded = state.loginHint || state.results.some((r) => (
      !r.ok && r.item && ['LOGIN_REQUIRED', 'COOKIE_INVALID'].includes(r.error?.code)
    ));
    if (loginNeeded) {
      alertHost.append(el('div.alert.alert--accent', {}, [
        icon('info'),
        el('div.alert-body.grow', {}, [
          el('div.alert-title', { text: '部分作品需要登录后才能下载更高清晰度' }),
          el('div', {
            text: '未配置对应平台 Cookie 时只能选择较低的可用清晰度。'
              + '你仍可直接按当前可用清晰度下载，或先到「设置」中填写本人账号的 Cookie。',
          }),
        ]),
        el('button.btn.btn--sm', {
          type: 'button',
          on: { click: () => openSettings() },
        }, [icon('settings'), el('span', { text: '去设置' })]),
      ]));
    }

    if (state.parseError) {
      alertHost.append(el('div.alert.alert--danger', { role: 'alert' }, [
        icon('alertCircle'),
        el('div.alert-body.grow', {}, [
          el('div.alert-title', { text: '请求失败' }),
          el('div', { text: state.parseError }),
        ]),
        el('button.btn.btn--sm', {
          type: 'button',
          on: {
            click: () => {
              state.parseError = null;
              renderAlerts();
              parseLinks();
            },
          },
        }, [icon('refresh'), el('span', { text: '重试' })]),
      ]));
    }
  }

  function startRiskTimer() {
    clearInterval(riskTimer);
    riskTimer = setInterval(() => {
      if (!state.risk) {
        clearInterval(riskTimer);
        riskTimer = null;
        return;
      }
      if (Date.now() >= state.risk.until) {
        state.risk = null;
        clearInterval(riskTimer);
        riskTimer = null;
        renderAlerts();
        toast('info', '限制已解除', '可以重新解析或下载了', { duration: 2600 });
        return;
      }
      renderAlerts();
    }, 1000);
  }

  /**
   * 统一错误处理：把错误码翻译成界面语言。
   * 注意：parseErrorBody 已把整个错误体放进 ApiError.detail，
   * 后端额外字段（retry_after）是平铺在 body 顶层的，因此从 detail 里读。
   */
  function handleApiError(err, context) {
    const code = err?.code || '';
    const retryAfter = retryAfterOf(err);

    if (code === 'RISK_CONTROL' || code === 'RATE_LIMITED') {
      const seconds = retryAfter > 0 ? retryAfter : (code === 'RISK_CONTROL' ? 600 : 60);
      state.risk = {
        code,
        message: errorText(err),
        until: Date.now() + seconds * 1000,
      };
      toast(
        'warning',
        code === 'RATE_LIMITED' ? '操作太频繁' : '平台风控中',
        `${errorText(err)}（${formatCountdown(seconds)} 后可重试）`,
        { duration: 6000 },
      );
      renderAlerts();
      startRiskTimer();
      return;
    }

    if (code === 'LOGIN_REQUIRED' || code === 'COOKIE_INVALID') {
      state.loginHint = true;
      renderAlerts();
      toast('warning', '需要登录态', errorText(err));
      return;
    }

    if (code === 'SERVICE_DISABLED') {
      state.moduleDisabled = true;
      state.parseError = errorText(err);
      updateParseButton();
      renderAlerts();
      return;
    }

    state.parseError = errorText(err);
    renderAlerts();
    toast('danger', context, errorText(err));
  }

  /* ------------------------------------------------------------------ *
   * 4. 解析结果渲染
   * ------------------------------------------------------------------ */

  function coverNode(item) {
    const wrap = el('div.video-cover');
    const url = item?.cover_url;
    if (!url) {
      wrap.append(coverPlaceholder());
      return wrap;
    }
    const img = el('img', {
      src: url,
      alt: `${item.title || '作品'} 封面`,
      // 不发送 Referer，避免泄漏来源页（PRD 6.4.5）
      referrerpolicy: 'no-referrer',
      loading: 'lazy',
    });
    img.addEventListener('error', () => {
      clear(wrap);
      wrap.append(coverPlaceholder());
    });
    wrap.append(img);
    return wrap;
  }

  function coverPlaceholder() {
    return el('div.video-cover-placeholder', {}, [icon('video', 'icon icon--lg')]);
  }

  function buildResultCard(result) {
    const { item, error, ok } = result;
    const partial = !ok && Boolean(item);
    const card = el(
      `div.video-result${!ok && !item ? '.is-error' : ''}${partial ? '.is-warn' : ''}`,
      { 'data-key': result.key },
    );

    // ---- 硬失败：红色原因 + 错误码 + 重试 ----
    if (!item) {
      card.append(el('div.video-result-main', {}, [
        el('div.row.gap-2.wrap', {}, [
          el('span.video-result-title', { text: '解析失败' }),
          error?.code ? el('span.tag.tag--danger.tag--mono', { text: error.code }) : null,
        ]),
        el('div.video-result-desc.t-danger', { text: error?.detail || '解析失败，请稍后重试' }),
        el('div.video-result-desc.t-xs.t-muted.t-break', { text: result.url, title: result.url }),
        el('div.video-actions', {}, [
          el('button.btn.btn--sm', {
            type: 'button',
            on: { click: () => retryResult(result) },
          }, [icon('refresh'), el('span', { text: '重试' })]),
          el('button.btn.btn--sm.btn--ghost', {
            type: 'button',
            on: { click: () => copyLink(result.url) },
          }, [icon('copy'), el('span', { text: '复制链接' })]),
        ]),
      ]));
      return card;
    }

    // ---- 正常 / 部分成功卡片 ----
    const formats = Array.isArray(item.formats) ? item.formats : [];
    // available === false 是平台未返回媒体地址的占位项，不能算作可用清晰度
    const usable = formats.filter(isFormatUsable);
    const isImage = item.media_type === 'image';
    const sizeEl = el('span.video-size', { text: '体积未知' });
    const noteEl = el('div.t-xs.t-muted');
    const currentFormat = formats.find((f) => f.format_id === result.formatId) || null;

    const refreshFormatInfo = () => {
      const format = formats.find((f) => f.format_id === result.formatId) || null;
      if (!format) {
        sizeEl.textContent = isImage ? '图文作品 · 打包 ZIP' : '体积未知';
        noteEl.textContent = '';
        return;
      }
      // 没有媒体地址的占位项拿不到可信体积，统一按「体积未知」展示
      if (format.available === false) {
        sizeEl.textContent = '体积未知';
        noteEl.textContent = format.note || '';
        return;
      }
      sizeEl.textContent = format.size_bytes ? `约 ${formatBytes(format.size_bytes)}` : '体积未知';
      noteEl.textContent = format.note || '';
    };

    let formatSelect = null;
    if (!isImage && formats.length > 0) {
      formatSelect = el('select.select.video-select', {
        'aria-label': `选择「${item.title || '作品'}」的清晰度`,
      });
      // 一个可用清晰度都没有时补占位项：否则浏览器会把第一个 disabled 项显示成已选中
      if (usable.length === 0) {
        formatSelect.append(el('option', {
          value: '',
          text: '无可用清晰度（需登录/会员/平台未返回地址）',
          selected: true,
          disabled: true,
          title: '当前没有可下载的清晰度，请先在「设置」中配置对应平台 Cookie',
        }));
      }
      for (const format of formats) {
        // 置灰优先级：需会员 > 需登录 > 平台未返回媒体地址
        const unavailable = format.available === false;
        const disabled = Boolean(format.need_login || format.need_vip || unavailable);
        const reason = format.need_vip
          ? '会员专属清晰度，暂不支持下载'
          : format.need_login
            ? '需要登录后可用，请先在「设置」中配置对应平台 Cookie'
            : unavailable
              ? '平台未返回该清晰度的媒体地址，请选择其他清晰度'
              : '';
        formatSelect.append(el('option', {
          value: format.format_id,
          text: `${format.quality}${format.note ? ` · ${format.note}` : ''}`
            + `${format.need_vip ? '（需会员）' : format.need_login ? '（需登录）' : unavailable ? '（平台未返回地址）' : ''}`,
          selected: format.format_id === result.formatId,
          disabled,
          title: reason,
        }));
      }
      formatSelect.addEventListener('change', () => {
        result.formatId = formatSelect.value;
        refreshFormatInfo();
      });
    }

    const activeJob = state.jobs.find((job) => (
      job.resultKey === result.key && (job.status === 'pending' || job.status === 'running')
    ));
    const canDownload = isImage ? true : usable.length > 0;

    const downloadBtn = el('button.btn.btn--primary.btn--sm', {
      type: 'button',
      disabled: !canDownload || Boolean(activeJob) || state.submitting,
      title: !canDownload ? '当前没有可下载的清晰度（需登录、会员专属或平台未返回地址）' : '',
      on: { click: () => submitResults([result]) },
    }, [
      icon('download'),
      el('span', {
        text: activeJob
          ? `下载中 ${activeJob.progress || 0}%`
          : isImage ? '下载图文（ZIP）' : partial ? '按可用清晰度下载' : '下载',
      }),
    ]);

    const head = el('div.video-result-head', {}, [
      state.results.length > 1
        ? el('input', {
            type: 'checkbox',
            checked: result.checked,
            'aria-label': `选择「${item.title || '作品'}」加入批量下载`,
            on: {
              change: (event) => {
                result.checked = event.currentTarget.checked;
                renderResults();
              },
            },
          })
        : null,
      coverNode(item),
      el('div.video-result-main', {}, [
        el('div.row.gap-2.wrap', {}, [
          el('span.video-result-title.t-clamp-2', {
            text: item.title || '未命名作品',
            title: item.title || '',
          }),
          tag(platformLabel(item.platform), 'accent'),
          isImage ? tag('图文', 'mono') : null,
          partial ? tag('部分可用', 'warning') : null,
        ]),
        el('div.video-result-meta', {}, [
          el('span', { text: item.author?.name || '未知作者' }),
          isImage
            ? el('span', { text: `${(item.images || []).length || '多'} 张图片` })
            : el('span', { text: `时长 ${formatDuration(item.duration_sec)}` }),
          item.stats?.play ? el('span', { text: `播放 ${formatCount(item.stats.play)}` }) : null,
        ]),
        el('div.video-result-controls', {}, [
          formatSelect,
          sizeEl,
          noteEl,
        ]),
        el('div.video-actions', {}, [
          downloadBtn,
          el('button.btn.btn--sm.btn--ghost', {
            type: 'button',
            title: '复制作品长链',
            on: { click: () => copyLink(item.url || result.url) },
          }, [icon('copy'), el('span', { text: '复制链接' })]),
        ]),
      ]),
    ]);

    card.append(head);

    if (partial) {
      card.append(el('div.video-result-warn', {}, [
        icon('alertTriangle'),
        el('div.grow', {}, [
          el('div', { text: error?.detail || '部分清晰度当前不可用，可按可用清晰度下载' }),
          error?.code ? el('div.t-xs.t-mono.t-muted', { text: `错误码 ${error.code}` }) : null,
        ]),
        el('button.btn.btn--sm.btn--ghost', {
          type: 'button',
          on: { click: () => openSettings() },
        }, [el('span', { text: '去设置' })]),
      ]));
    }

    if (currentFormat?.expire_at) {
      card.append(el('div.t-xs.t-warning', {
        text: `该清晰度的媒体地址将于 ${formatTime(currentFormat.expire_at)} 过期，请尽快下载`,
      }));
    }
    refreshFormatInfo();
    return card;
  }

  function renderBatchBar() {
    clear(batchBar);
    if (state.results.length < 2) {
      batchBar.hidden = true;
      return;
    }
    batchBar.hidden = false;

    // 可批量下载：有 item，且图文作品或至少有一个可用清晰度（占位项不算）
    const batchable = (r) => Boolean(r.item) && (r.item.media_type === 'image'
      || (r.item.formats || []).some(isFormatUsable));
    const downloadable = state.results.filter(batchable);
    const selected = state.results.filter((r) => r.checked && batchable(r));
    const allChecked = downloadable.length > 0 && downloadable.every((r) => r.checked);

    batchBar.append(
      el('label.check', {}, [
        el('input', {
          type: 'checkbox',
          checked: allChecked,
          'aria-label': '全选可下载作品',
          on: {
            change: (event) => {
              const checked = event.currentTarget.checked;
              for (const result of state.results) {
                if (result.item) result.checked = checked;
              }
              renderResults();
            },
          },
        }),
        el('span', { text: '全选' }),
      ]),
      el('span.t-sm.t-muted', { text: `已选 ${selected.length} / 可下载 ${downloadable.length}` }),
      el('div.grow'),
      el('button.btn.btn--sm.btn--primary', {
        type: 'button',
        disabled: selected.length === 0 || state.submitting,
        on: { click: () => submitResults(selected) },
      }, [icon('download'), el('span', {
        text: `批量下载${selected.length ? `（${selected.length}）` : ''}`,
      })]),
      el('button.btn.btn--sm', {
        type: 'button',
        disabled: successTaskIds().length < 2,
        title: '把本页已完成的任务打包为 ZIP',
        on: { click: () => downloadAllZip() },
      }, [icon('archive'), el('span', { text: '打包下载' })]),
    );
  }

  function renderResults() {
    clear(resultsHost);
    clear(batchBar);
    batchBar.hidden = true;
    resultCount.textContent = state.results.length > 0
      ? `共 ${state.results.length} 条 · 成功 ${state.results.filter((r) => r.ok).length}`
      : '';

    if (state.parsing) {
      resultsHost.append(el('div.video-loading', {}, [
        el('span.video-spinner', { 'aria-hidden': 'true' }),
        el('span', { text: '解析中… 正在读取作品信息（不会下载媒体文件）' }),
      ]));
      return;
    }

    if (state.results.length === 0) {
      resultsHost.append(emptyState({
        iconName: 'video',
        title: '还没有解析结果',
        desc: '粘贴作品链接后按回车或点「解析」。解析只读取作品信息，不会下载媒体文件，也不占用任务队列。',
      }));
      return;
    }

    renderBatchBar();
    for (const result of state.results) resultsHost.append(buildResultCard(result));
  }

  async function copyLink(url) {
    if (!url) return;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(url);
      } else {
        const ta = el('textarea', { text: url, style: { position: 'fixed', top: '-9999px' } });
        document.body.append(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      toast('success', '链接已复制', '', { duration: 1600 });
    } catch {
      toast('warning', '复制失败', '浏览器拒绝了剪贴板访问，可手动选中链接复制');
    }
  }

  /* ------------------------------------------------------------------ *
   * 5. 提交下载 + 轮询
   * ------------------------------------------------------------------ */

  /** 把一条解析结果转换成后端下载接口需要的 item（契约 §3.3） */
  function buildDownloadItem(result) {
    const item = result.item;
    const format = (item.formats || []).find((f) => f.format_id === result.formatId) || null;
    let output = 'mp4';
    if (item.media_type !== 'image' && format?.quality === 'audio') {
      output = state.settings?.prefer_format === 'm4a' ? 'm4a' : 'mp3';
    }
    return {
      url: item.url,
      platform: item.platform,
      item_id: item.item_id,
      format_id: result.formatId || format?.format_id || '',
      output,
      title: item.title || '',
      author: item.author?.name || '',
      quality: format?.quality || '',
    };
  }

  async function submitResults(targets) {
    const list = (targets || []).filter((result) => result?.item);
    if (list.length === 0) {
      toast('warning', '请先选择要下载的作品');
      return;
    }
    const payload = list.map((result) => buildDownloadItem(result));
    await submitPayloads(payload, list);
  }

  /**
   * 真正提交任务。
   * @param {object[]} payload 后端 items
   * @param {object[]} references 与 payload 同序的解析结果（重试时可能为空）
   */
  async function submitPayloads(payload, references = []) {
    if (state.submitting) return;
    if (!Array.isArray(payload) || payload.length === 0) return;
    if (runtime.demo) {
      toast('info', '当前为演示模式', '将生成可下载的占位产物，用于验证「任务 → 进度 → 产物」链路', { duration: 2800 });
    }

    state.submitting = true;
    renderResults();
    renderTasks();

    try {
      const created = [].concat(await videoApi.download(payload) || []);
      const reused = created.filter((task) => task.reused);

      created.forEach((task, index) => {
        const reference = references[index] || null;
        upsertTask(task);
        state.jobs.unshift({
          id: task.task_id,
          resultKey: reference?.key || null,
          name: task.inputs?.[0]?.name || '视频作品',
          status: task.status,
          progress: task.progress || 0,
          output: task.output || null,
          error: task.error || null,
          errorCode: task.error_code || task.errorCode || null,
          expireAt: task.expire_at || null,
          reused: Boolean(task.reused),
          reusedFrom: task.reused_from || null,
          quality: reference?.item
            ? ((reference.item.formats || []).find((f) => f.format_id === reference.formatId)?.quality || '')
            : '',
          payload: payload[index],
          retrying: false,
        });
      });

      renderResults();
      renderTasks();
      if (reused.length > 0) {
        const since = reused[0].reused_from;
        toast(
          'success',
          '已为你复用上次的下载',
          since ? `${formatRelative(since)}生成的产物仍有效，未重复请求平台` : '24 小时内已下载过同一作品，直接复用产物',
        );
      }
      // 轮询放到后台执行，不阻塞用户继续提交下一批
      pollJobs(created);
    } catch (err) {
      handleApiError(err, '提交下载失败');
    } finally {
      state.submitting = false;
      renderResults();
      renderTasks();
    }
  }

  /** 1 秒轮询所有任务，直到终态（复用 api.pollTask） */
  async function pollJobs(created) {
    if (!Array.isArray(created) || created.length === 0) return;
    const polls = created.map((task) => pollTask(task.task_id, {
      intervalMs: CFG.pollIntervalMs,
      timeoutMs: CFG.limits.videoPollTimeoutMs,
      signal: abortController.signal,
      onUpdate: (latest) => {
        upsertTask(latest);
        const job = state.jobs.find((item) => item.id === latest.task_id);
        if (!job) return;
        job.status = latest.status;
        job.progress = latest.progress ?? job.progress;
        job.error = latest.error || null;
        job.errorCode = latest.error_code || latest.errorCode || null;
        job.output = latest.output || null;
        job.expireAt = latest.expire_at || job.expireAt;
        renderTasks();
        renderResults();
      },
    }).catch((err) => {
      if (err?.code === 'ABORTED') return null;
      const job = state.jobs.find((item) => item.id === task.task_id);
      if (job && (job.status === 'pending' || job.status === 'running')) {
        job.status = 'failed';
        job.error = errorText(err);
      }
      renderTasks();
      return null;
    }));

    const settled = await Promise.all(polls);
    const done = settled.filter(Boolean);
    renderTasks();
    renderResults();

    if (done.length === 0) return;
    const ok = done.filter((task) => task.status === 'success').length;
    const failed = done.length - ok;
    if (failed === 0) {
      toast('success', `下载完成（${ok} 个）`, '可在「最近任务」里下载产物，产物默认保留 24 小时');
    } else if (ok === 0) {
      toast('danger', '全部下载失败', '请查看失败原因后重试');
    } else {
      toast('warning', `部分成功（成功 ${ok} / 失败 ${failed}）`, '失败项已标注原因，可单独重试');
    }
  }

  /* ------------------------------------------------------------------ *
   * 6. 最近任务渲染
   * ------------------------------------------------------------------ */

  function successTaskIds() {
    return state.jobs
      .filter((job) => job.status === 'success' && job.output?.file_id)
      .map((job) => job.id);
  }

  function expireText(expireAt) {
    if (!expireAt) return '有效期未知';
    return `产物保留至 ${formatTime(expireAt, false)}（${formatRelative(expireAt)}过期）`;
  }

  function jobSubtitle(job) {
    switch (job.status) {
      case 'pending':
        return '排队中 · 等待可用的下载槽位';
      case 'running':
        return `下载中 ${job.progress || 0}% · 每 1 秒刷新`;
      case 'success':
        return job.output
          ? `${formatBytes(job.output.size)} · ${expireText(job.expireAt)}`
          : '已完成 · 产物信息缺失';
      case 'failed':
        return `失败：${job.error || '未知原因'}`;
      case 'cancelled':
        return '已取消';
      default:
        return job.status || '';
    }
  }

  function buildJobRow(job) {
    const row = el('div.video-job', { 'data-status': job.status });

    const actions = [];
    if (job.status === 'pending' || job.status === 'running') {
      actions.push(el('button.btn.btn--sm.btn--danger-ghost', {
        type: 'button',
        on: {
          click: async () => {
            try {
              await api.tasks.cancel(job.id);
              toast('info', '已请求取消任务');
            } catch (err) {
              toast('danger', '取消失败', errorText(err));
            }
          },
        },
      }, [icon('stop'), el('span', { text: '取消' })]));
    }
    if (job.status === 'success' && job.output?.file_id) {
      actions.push(el('button.btn.btn--sm', {
        type: 'button',
        on: { click: () => downloadJobOutput(job) },
      }, [icon('download'), el('span', { text: '下载产物' })]));
    }
    if (job.status === 'failed') {
      actions.push(el('button.btn.btn--sm', {
        type: 'button',
        disabled: job.retrying,
        on: { click: () => retryJob(job) },
      }, [icon('refresh'), el('span', { text: '重试' })]));
    }
    actions.push(el('button.btn.btn--sm.btn--icon.btn--ghost', {
      type: 'button',
      'aria-label': `从列表移除 ${job.name}`,
      'data-tip': '移除记录',
      on: {
        click: () => {
          state.jobs = state.jobs.filter((item) => item !== job);
          renderTasks();
          renderResults();
        },
      },
    }, [icon('trash')]));

    row.append(el('div.video-job-head', {}, [
      el('div.col.gap-1.grow', {}, [
        el('div.row.gap-2.wrap', {}, [
          el('span.video-job-name.t-clamp-1', { text: job.name, title: job.name }),
          statusPill(job.status),
          job.reused ? tag('已复用', 'accent') : null,
          job.quality ? tag(job.quality, 'mono') : null,
          job.errorCode ? el('span.tag.tag--danger.tag--mono', { text: job.errorCode }) : null,
        ]),
        el('div.t-xs.t-muted', { text: jobSubtitle(job) }),
      ]),
      el('div.video-job-actions', {}, actions),
    ]));

    if (job.status === 'pending' || job.status === 'running') {
      const bar = createProgress({ size: 'sm' });
      if (job.status === 'pending') bar.setIndeterminate();
      else bar.setValue(job.progress, undefined);
      bar.setLabel(`${job.name} 下载进度`);
      row.append(bar);
    }

    if (job.status === 'failed') {
      row.append(el('div.video-job-error', {}, [
        icon('alertCircle'),
        el('span', { text: job.error || '下载失败，请重试' }),
      ]));
    }

    if (job.reused) {
      row.append(el('div.video-job-note', {}, [
        icon('history'),
        el('span', {
          text: job.reusedFrom
            ? `已为你复用 ${formatRelative(job.reusedFrom)}的下载，未重复请求平台`
            : '已为你复用上次的下载，未重复请求平台',
        }),
      ]));
    }

    return row;
  }

  function renderTasks() {
    clear(tasksHost);
    taskCount.textContent = state.jobs.length > 0 ? `共 ${state.jobs.length} 个` : '';
    zipBtn.disabled = successTaskIds().length < 2;

    if (state.jobs.length === 0) {
      tasksHost.append(el('div.video-empty-line', {
        text: '下载任务会出现在这里：显示进度、失败原因、产物大小与有效期。',
      }));
      return;
    }
    for (const job of state.jobs) tasksHost.append(buildJobRow(job));
  }

  async function downloadJobOutput(job) {
    if (!job.output?.file_id) return;
    try {
      await api.download(job.output.file_id, job.output.name);
      toast('success', '已开始下载', job.output.name);
    } catch (err) {
      toast('danger', '下载失败', errorText(err));
    }
  }

  async function retryJob(job) {
    if (job.retrying) return;
    job.retrying = true;
    renderTasks();
    try {
      const reference = state.results.find((result) => result.key === job.resultKey) || null;
      await submitPayloads([job.payload], reference ? [reference] : []);
    } finally {
      job.retrying = false;
      renderTasks();
    }
  }

  async function downloadAllZip() {
    const ids = successTaskIds();
    if (ids.length === 0) {
      toast('warning', '还没有可打包的产物', '请先完成至少一个下载任务');
      return;
    }
    if (ids.length < 2) {
      toast('info', '打包下载需要至少 2 个成功产物', '可直接在任务条目里点「下载产物」');
      return;
    }
    try {
      await api.batchZip(ids, `video-${ids.length}files.zip`);
      toast('success', '打包下载已开始', `包含 ${ids.length} 个产物`);
    } catch (err) {
      toast('danger', '打包失败', errorText(err));
    }
  }

  /* ------------------------------------------------------------------ *
   * 7. 平台状态
   * ------------------------------------------------------------------ */

  function renderPlatformChips() {
    clear(platformChips);
    const items = state.platforms?.items || [];
    if (items.length === 0) {
      platformChips.append(el('span.t-xs.t-muted', { text: '平台状态加载中…' }));
      return;
    }
    platformChips.append(el('span.t-xs.t-muted', { text: '平台' }));
    for (const item of items) {
      const cookieConfigured = Boolean(item.cookie?.configured);
      platformChips.append(el('span.video-platform-chip', {
        'data-state': item.enabled ? 'on' : 'off',
        title: item.enabled
          ? `${item.label} 已启用${cookieConfigured ? `，登录态：${item.cookie?.masked || '已配置'}` : '，未配置登录态'}`
          : `${item.label} 已在设置中停用`,
      }, [
        el('span.video-dot', { 'aria-hidden': 'true' }),
        el('span', { text: item.label }),
        cookieConfigured
          ? el('span.video-chip-badge', { text: '已登录' })
          : null,
        !item.enabled
          ? el('span.video-chip-badge', { text: '已停用' })
          : null,
      ]));
    }
    if (state.platforms?.prefer_quality) {
      platformChips.append(el('span.t-xs.t-muted', {
        text: `默认 ${state.platforms.prefer_quality} · ${String(state.platforms.prefer_format || 'mp4').toUpperCase()} · 单次最多 ${state.platforms.max_urls || 20} 条`,
      }));
    }
  }

  async function openPlatformStatus() {
    let data;
    try {
      data = await videoApi.platforms();
      state.platforms = data;
      renderPlatformChips();
    } catch (err) {
      toast('danger', '读取平台状态失败', errorText(err));
      return;
    }

    const body = el('div.col.gap-4');
    body.append(el('div.alert.alert--info', {}, [
      icon('info'),
      el('div.alert-body', {}, [
        el('div.alert-title', { text: '平台与引擎' }),
        el('div', {
          text: `单次最多解析 ${data.max_urls || 20} 条；默认清晰度 ${data.prefer_quality || '1080P'}，`
            + `默认输出 ${String(data.prefer_format || 'mp4').toUpperCase()}。`,
        }),
      ]),
    ]));

    const table = el('table.table', { 'aria-label': '平台能力与登录态' });
    table.append(
      el('thead', {}, [el('tr', {}, ['平台', '开关', 'Cookie', '能力'].map((text) => el('th', { text })))]),
      el('tbody', {}, (data.items || []).map((item) => el('tr', {}, [
        el('td', {}, [
          el('div.row.gap-2', {}, [
            el('strong', { text: item.label }),
            tag(item.name, 'mono'),
          ]),
        ]),
        el('td', {}, [item.enabled ? tag('已启用', 'success') : tag('已停用', 'danger')]),
        el('td', {}, [
          item.cookie?.configured
            ? el('span.tag.tag--accent.tag--mono', { text: item.cookie.masked || '已配置' })
            : tag('未配置'),
        ]),
        el('td', {}, [
          el('div.row.gap-1.wrap', {}, [
            item.capabilities?.video ? tag('视频', 'success') : null,
            item.capabilities?.image ? tag('图文', 'mono') : null,
            item.capabilities?.multi_part ? tag('多P', 'mono') : null,
          ]),
        ]),
      ]))),
    );
    body.append(el('div.table-wrap', {}, [table]));

    const engineRows = [
      ['FFmpeg', data.engine?.ffmpeg],
      ['yt-dlp', data.engine?.ytdlp],
    ].map(([name, info]) => el('div.video-engine-row', {}, [
      el('span.video-engine-name', { text: name }),
      info?.state === 'ok' ? tag('可用', 'success')
        : info?.state === 'warn' ? tag('降级', 'warning')
          : tag('不可用', 'danger'),
      el('span.t-xs.t-muted', {
        text: info?.version ? `v${info.version}` : (info?.hint || '未检测到'),
      }),
    ]));
    body.append(el('div.form-section', {}, [
      el('div.form-section-title', { text: '引擎自检' }),
      el('div.col.gap-1', {}, engineRows),
    ]));

    modal({
      title: '平台与引擎状态',
      size: 'wide',
      body,
      actions: [{ label: '关闭', variant: 'secondary' }],
    });
  }

  /* ------------------------------------------------------------------ *
   * 8. 设置弹层
   * ------------------------------------------------------------------ */

  function field(labelText, control, helpText) {
    return el('div.field', {}, [
      el('label.label', { text: labelText }),
      control,
      helpText ? el('div.help', { text: helpText }) : null,
    ]);
  }

  function numberInput(id, min, max, value) {
    return el('input.input', {
      id,
      type: 'number',
      min,
      max,
      step: '1',
      value: String(value ?? ''),
    });
  }

  async function openSettings() {
    let data;
    try {
      data = await videoApi.settings();
      state.settings = data;
    } catch (err) {
      toast('danger', '读取设置失败', errorText(err));
      return;
    }

    /** 待提交的 Cookie：'' 表示清除，未列出表示不修改 */
    const pendingCookies = {};
    const clearedPlatforms = new Set();
    const body = el('div.col.gap-5');

    // ---- 下载偏好 ----
    const qualitySelect = el('select.select', { id: 'video-setting-quality' });
    for (const option of QUALITY_OPTIONS) {
      qualitySelect.append(el('option', {
        value: option.value,
        text: option.label,
        selected: data.prefer_quality === option.value,
      }));
    }
    const formatSelect = el('select.select', { id: 'video-setting-format' });
    for (const [value, label] of Object.entries(FORMAT_LABELS)) {
      formatSelect.append(el('option', {
        value,
        text: label,
        selected: data.prefer_format === value,
      }));
    }
    body.append(el('div.form-section', {}, [
      el('div.form-section-title', { text: '下载偏好' }),
      el('div.field-row', {}, [
        field('默认清晰度', qualitySelect, '设置项不可用时回退到 ≤1080P 的最高可用档；不会默认选 4K。'),
        field('默认输出格式', formatSelect, '仅音频作品应用该选择；视频统一输出 MP4 以保证兼容性。'),
      ]),
    ]));

    // ---- 任务与带宽 ----
    const concurrentInput = numberInput('video-setting-concurrent', 1, 4, data.max_concurrent);
    const segmentsInput = numberInput('video-setting-segments', 1, 8, data.download_segments);
    const maxFileInput = numberInput('video-setting-maxfile', 1, 10240, data.max_file_mb);
    body.append(el('div.form-section', {}, [
      el('div.form-section-title', { text: '任务与带宽' }),
      el('div.field-row.field-row--3', {}, [
        field('同时下载数', concurrentInput, '1–4。过高容易触发平台风控。'),
        field('分片并发', segmentsInput, '1–8。单个任务内部的分片下载并发数。'),
        field('单文件上限（MB）', maxFileInput, '超过上限的任务会失败并提示降低清晰度。'),
      ]),
    ]));

    // ---- 平台开关 ----
    const platformToggles = new Map();
    const platformSection = el('div.form-section', {});
    platformSection.append(el('div.form-section-title', { text: '平台开关' }));
    for (const [name, meta] of Object.entries(PLATFORM_META)) {
      const platformData = data.platforms?.[name] || { enabled: true, cookie: {} };
      const checkbox = el('input', {
        type: 'checkbox',
        id: `video-platform-${name}`,
        checked: platformData.enabled !== false,
      });
      platformToggles.set(name, checkbox);
      platformSection.append(el('label.switch', { for: `video-platform-${name}` }, [
        checkbox,
        el('span.switch-track'),
        el('span', { text: meta.label }),
        platformData.cookie?.configured ? tag('已配置 Cookie', 'success') : tag('未配置 Cookie'),
      ]));
    }
    body.append(platformSection);

    // ---- Cookie ----
    const cookieSection = el('div.form-section', {});
    cookieSection.append(
      el('div.form-section-title', { text: '登录态 Cookie' }),
      el('div.alert.alert--warning', {}, [
        icon('shield'),
        el('div.alert-body', {}, [el('div', { text: COOKIE_NOTICE })]),
      ]),
    );

    const cookieInputs = new Map();
    for (const [name, meta] of Object.entries(PLATFORM_META)) {
      const cookie = data.platforms?.[name]?.cookie || { configured: false, masked: null };
      const input = el('input.input', {
        type: 'password',
        autocomplete: 'off',
        placeholder: cookie.configured
          ? `已配置：${cookie.masked || '****'}（留空表示不修改）`
          : '未配置，粘贴完整 Cookie 后保存',
        'aria-label': `${meta.label} Cookie`,
      });
      const status = el('span.t-xs.t-muted', {
        text: cookie.configured ? `当前：${cookie.masked || '已配置'}` : '当前未配置',
      });
      const clearBtn = el('button.btn.btn--sm.btn--danger-ghost', {
        type: 'button',
        disabled: !cookie.configured,
        on: {
          click: () => {
            pendingCookies[name] = '';
            clearedPlatforms.add(name);
            input.value = '';
            input.disabled = true;
            clearBtn.disabled = true;
            status.textContent = '保存后将清除该平台 Cookie';
          },
        },
      }, [icon('trash'), el('span', { text: '清除' })]);

      cookieInputs.set(name, input);
      cookieSection.append(el('div.field', {}, [
        el('label.label', { text: `${meta.label} Cookie` }),
        el('div.row.gap-2', {}, [input, clearBtn]),
        status,
      ]));
    }
    body.append(cookieSection);

    const dialog = modal({
      title: '视频下载设置',
      size: 'wide',
      body,
      actions: [
        { label: '取消', value: null },
        { label: '保存设置', variant: 'primary', closeOnClick: false, onClick: () => saveSettings() },
      ],
    });

    async function saveSettings() {
      try {
        const cookies = { ...pendingCookies };
        for (const [name, input] of cookieInputs) {
          if (clearedPlatforms.has(name)) continue;
          const value = input.value.trim();
          if (value) cookies[name] = value;
        }

        const patch = {
          prefer_quality: qualitySelect.value,
          prefer_format: formatSelect.value,
          max_concurrent: Number(concurrentInput.value) || 2,
          download_segments: Number(segmentsInput.value) || 4,
          max_file_mb: Number(maxFileInput.value) || 2048,
          platforms: {},
        };
        for (const [name, checkbox] of platformToggles) {
          patch.platforms[name] = { enabled: checkbox.checked };
        }
        if (Object.keys(cookies).length > 0) patch.cookies = cookies;

        const updated = await videoApi.updateSettings(patch);
        state.settings = updated;
        dialog.close();
        toast('success', '设置已保存', '默认清晰度与平台状态已刷新');
        await refreshPlatforms();
        // 默认清晰度可能变化：重算尚未提交下载的结果的默认档位
        for (const result of state.results) {
          if (result.item && result.item.media_type !== 'image') {
            result.formatId = pickDefaultFormat(result.item);
          }
        }
        renderResults();
        renderAlerts();
      } catch (err) {
        handleApiError(err, '保存设置失败');
      }
    }
  }

  async function refreshPlatforms() {
    try {
      const [platforms, settings] = await Promise.all([videoApi.platforms(), videoApi.settings()]);
      state.platforms = platforms;
      state.settings = settings;
      renderPlatformChips();
      updateParseButton();
    } catch (err) {
      if (err?.code === 'SERVICE_DISABLED') {
        state.moduleDisabled = true;
        updateParseButton();
        state.parseError = errorText(err);
        renderAlerts();
      }
    }
  }

  /* ------------------------------------------------------------------ *
   * 9. 历史抽屉
   * ------------------------------------------------------------------ */

  function trapFocus(scope, event) {
    const focusables = Array.from(scope.querySelectorAll(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]',
    )).filter((node) => node.offsetParent !== null);
    if (focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function openHistory() {
    // 同一时刻只保留一个抽屉
    if (historyCleanup) {
      historyCleanup();
      historyCleanup = null;
    }

    const filters = { platform: '', keyword: '' };
    const listHost = el('div.video-history-list');
    const countEl = el('span.t-sm.t-muted');
    const searchInput = el('input.input', {
      type: 'search',
      placeholder: '搜索标题或作者…',
      'aria-label': '搜索下载历史',
    });

    const segmented = el('div.segmented', { role: 'group', 'aria-label': '按平台筛选历史' });
    const filterDefs = [
      { id: '', label: '全部' },
      { id: 'douyin', label: '抖音' },
      { id: 'bilibili', label: 'B站' },
      { id: 'kuaishou', label: '快手' },
    ];
    const filterButtons = filterDefs.map((def) => {
      const button = el('button.segmented-item', {
        type: 'button',
        text: def.label,
        'aria-pressed': String(filters.platform === def.id),
        on: {
          click: () => {
            filters.platform = def.id;
            filterButtons.forEach((item, index) => {
              item.setAttribute('aria-pressed', String(filterDefs[index].id === def.id));
            });
            loadHistory();
          },
        },
      });
      segmented.append(button);
      return button;
    });

    const drawer = el('aside.video-drawer', {
      role: 'dialog',
      'aria-modal': 'true',
      'aria-label': '下载历史',
    }, [
      el('div.video-drawer-header', {}, [
        el('h2', {}, [icon('history'), el('span', { text: '下载历史' })]),
        el('button.btn.btn--sm.btn--icon.btn--ghost', {
          type: 'button',
          'aria-label': '关闭历史抽屉',
          on: { click: () => close() },
        }, [icon('close')]),
      ]),
      el('div.video-drawer-toolbar', {}, [
        segmented,
        searchInput,
      ]),
      listHost,
      el('div.video-drawer-footer', {}, [
        countEl,
        el('div.grow'),
        el('button.btn.btn--sm.btn--danger-ghost', {
          type: 'button',
          on: { click: () => clearAllHistory() },
        }, [icon('trash'), el('span', { text: '清空历史' })]),
        el('button.btn.btn--sm', {
          type: 'button',
          on: { click: () => close() },
        }, [el('span', { text: '关闭' })]),
      ]),
    ]);

    const scrim = el('div.scrim.video-drawer-scrim', {}, [drawer]);
    const previousFocus = document.activeElement;

    let closed = false;
    function close() {
      if (closed) return;
      closed = true;
      document.removeEventListener('keydown', onKeyDown, true);
      searchDebounced.cancel();
      scrim.remove();
      document.body.style.removeProperty('overflow');
      if (previousFocus && document.contains(previousFocus)) previousFocus.focus();
      historyCleanup = null;
    }

    function onKeyDown(event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        close();
        return;
      }
      if (event.key === 'Tab') trapFocus(drawer, event);
    }

    scrim.addEventListener('mousedown', (event) => {
      if (event.target === scrim) close();
    });
    document.addEventListener('keydown', onKeyDown, true);
    document.body.style.overflow = 'hidden';
    document.body.append(scrim);
    historyCleanup = close;

    const searchDebounced = debounce(() => {
      filters.keyword = searchInput.value.trim();
      loadHistory();
    }, 300);
    searchInput.addEventListener('input', searchDebounced);

    async function loadHistory() {
      clear(listHost);
      listHost.append(el('div.video-loading', {}, [
        el('span.video-spinner', { 'aria-hidden': 'true' }),
        el('span', { text: '正在读取历史…' }),
      ]));
      try {
        const data = await videoApi.history({
          platform: filters.platform,
          keyword: filters.keyword,
          page: 1,
          pageSize: 100,
        });
        countEl.textContent = `共 ${data?.total ?? 0} 条`;
        renderHistoryItems(Array.isArray(data?.items) ? data.items : []);
      } catch (err) {
        clear(listHost);
        listHost.append(el('div.alert.alert--danger', {}, [
          icon('alertCircle'),
          el('div.alert-body', {}, [el('div', { text: errorText(err) })]),
        ]));
      }
    }

    function renderHistoryItems(items) {
      clear(listHost);
      if (items.length === 0) {
        listHost.append(emptyState({
          iconName: 'history',
          title: '暂无下载历史',
          desc: '成功下载过的作品会记录在这里，24 小时内再次下载同一作品会直接复用产物。',
        }));
        return;
      }
      for (const item of items) listHost.append(buildHistoryItem(item));
    }

    function buildHistoryItem(item) {
      const row = el('div.video-history-item');
      const cover = el('div.video-history-cover');
      if (item.cover_url) {
        const img = el('img', {
          src: item.cover_url,
          alt: '',
          referrerpolicy: 'no-referrer',
          loading: 'lazy',
        });
        img.addEventListener('error', () => { clear(cover); cover.append(icon('video')); });
        cover.append(img);
      } else {
        cover.append(icon('video'));
      }

      const actions = [];
      if (item.file_id) {
        actions.push(el('button.btn.btn--sm', {
          type: 'button',
          on: { click: () => downloadHistoryItem(item) },
        }, [icon('download'), el('span', { text: '下载' })]));
      }
      actions.push(el('button.btn.btn--sm.btn--icon.btn--ghost', {
        type: 'button',
        'aria-label': `删除历史记录 ${item.title || item.item_id}`,
        'data-tip': '删除记录',
        on: { click: () => removeHistoryItem(item) },
      }, [icon('trash')]));

      row.append(cover, el('div.video-history-main', {}, [
        el('div.row.gap-2.wrap', {}, [
          el('span.t-clamp-1', {
            text: item.title || '未命名作品',
            title: item.title || '',
            style: { fontWeight: 'var(--fw-medium)' },
          }),
          tag(platformLabel(item.platform), 'accent'),
          item.reused ? tag('已复用', 'warning') : null,
          statusPill(item.status),
        ]),
        el('div.t-xs.t-muted', {
          text: `${item.author || '未知作者'} · ${item.quality || '—'} · ${formatBytes(item.size_bytes)} · ${formatRelative(item.created_at)}`,
        }),
        item.reused
          ? el('div.t-xs.t-warning', {
              text: `已为你复用 ${formatRelative(item.created_at)}的下载`,
            })
          : null,
        item.file_id
          ? el('div.t-xs.t-muted', { text: expireText(item.expire_at) })
          : null,
      ]), el('div.video-history-actions', {}, actions));

      return row;
    }

    async function downloadHistoryItem(item) {
      const name = safeFileName(
        `${platformLabel(item.platform)}-${item.author || '未知作者'}-${item.title || '未命名作品'}-${item.quality || ''}.mp4`,
      );
      try {
        await api.download(item.file_id, name);
        toast('success', '已开始下载', name);
      } catch (err) {
        toast('danger', '下载失败', errorText(err));
      }
    }

    async function removeHistoryItem(item) {
      const confirmed = await confirmDialog({
        title: '删除这条历史记录',
        message: '只删除历史记录，不删除仍有效的产物文件（产物到期后自动回收）。',
        confirmLabel: '删除',
        danger: true,
      });
      if (!confirmed) return;
      try {
        const result = await videoApi.removeHistory(item.item_id, item.platform);
        toast('success', '已删除记录', `移除 ${result?.removed ?? 1} 条`);
        loadHistory();
      } catch (err) {
        toast('danger', '删除失败', errorText(err));
      }
    }

    async function clearAllHistory() {
      const confirmed = await confirmDialog({
        title: '清空全部下载历史',
        message: '历史记录会被全部清除，但已生成的产物仍保留至各自的过期时间。此操作不可撤销。',
        confirmLabel: '全部清空',
        danger: true,
      });
      if (!confirmed) return;
      try {
        const result = await videoApi.clearHistory();
        toast('success', '历史已清空', `共清除 ${result?.removed ?? 0} 条记录`);
        loadHistory();
      } catch (err) {
        toast('danger', '清空失败', errorText(err));
      }
    }

    loadHistory();
    requestAnimationFrame(() => searchInput.focus());
  }

  /* ------------------------------------------------------------------ *
   * 10. 初始化与生命周期
   * ------------------------------------------------------------------ */

  updateLinkCount();
  updateParseButton();
  renderResults();
  renderTasks();
  renderAlerts();
  renderPlatformChips();

  // 支持 #/video?url=https://... 预填（FR-VD-01 第 8 条，便于后续做分享直达）
  try {
    const rawHash = window.location.hash;
    const queryIndex = rawHash.indexOf('?');
    if (queryIndex >= 0) {
      const preset = new URLSearchParams(rawHash.slice(queryIndex + 1)).get('url');
      if (preset) {
        inputEl.value = preset;
        updateLinkCount();
      }
    }
  } catch { /* 非浏览器环境或 hash 非法时忽略 */ }

  refreshPlatforms();
  ttlTimer = setInterval(() => {
    if (state.jobs.some((job) => job.status === 'success' && job.expireAt)) renderTasks();
  }, 30000);
  disposers.push(() => clearInterval(ttlTimer));

  return {
    onShow() {
      // 从任务中心返回时同步任务状态（与音频视图一致）
      const known = getTasks();
      for (const job of state.jobs) {
        const task = known.find((item) => item.task_id === job.id);
        if (!task) continue;
        job.status = task.status;
        job.progress = task.progress;
        job.error = task.error || null;
        job.errorCode = task.error_code || null;
        job.output = task.output || job.output;
        job.expireAt = task.expire_at || job.expireAt;
      }
      renderTasks();
      renderResults();
    },
    async unmount() {
      abortController.abort();
      debouncedParse.cancel();
      clearInterval(riskTimer);
      riskTimer = null;
      clearInterval(ttlTimer);
      ttlTimer = null;
      for (const dispose of disposers) {
        try { dispose(); } catch { /* 忽略 */ }
      }
      disposers.length = 0;
      if (historyCleanup) {
        try { historyCleanup(); } catch { /* 忽略 */ }
        historyCleanup = null;
      }
    },
  };
}
