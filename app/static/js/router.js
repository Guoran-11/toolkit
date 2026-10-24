/**
 * router.js — 极简 hash 路由
 *
 * 为什么用 hash 而非 History API：
 * 本项目需要「双击 exe 直接打开本地文件」之外的两种部署形态都能工作，
 * hash 路由不需要服务端配置 fallback，也不会在反向代理的奇怪 path 规则下 404。
 *
 * 视图按需动态 import：首屏只加载当前视图的代码，降低初始 JS 体积（LCP 优化）。
 */

const routes = new Map();
let outlet = null;
let notFound = null;
let currentId = null;
let currentInstance = null;
const changeHandlers = new Set();

/** 路由定义 */
export function defineRoute(def) {
  routes.set(def.id, def);
}

/**
 * 启动路由。
 * @param {object} cfg
 * @param {HTMLElement} cfg.outlet        视图挂载容器
 * @param {string} cfg.defaultId          默认视图
 * @param {(id:string, viewId:string, def:object)=>void} [cfg.onChange]
 */
export function startRouter({ outlet: outletEl, defaultId, onChange, onNotFound }) {
  outlet = outletEl;
  notFound = onNotFound;
  if (onChange) changeHandlers.add(onChange);

  window.addEventListener('hashchange', () => render(parseHash(), { push: false }));
  // 首屏：无 hash 时用 replaceState 补齐，避免在历史记录里多出一条
  const initial = parseHash();
  if (!initial) history.replaceState(null, '', `#/${defaultId}`);
  render(initial || defaultId, { push: false });
}

/** 从 location.hash 解析视图 id，并把剩余部分作为参数传入视图 */
function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, '');
  if (!raw) return '';
  // 必须先切掉查询串：`#/geo?lat=39.9&lon=116.4` 若不处理，
  // 整个「geo?lat=…」会被当成视图 id → 路由查不到 → 跳「页面不存在」并切走视图。
  // （地图定位的分享链接就带查询串，实测踩过：打开分享链接会被弹回转换中心）
  const [path] = raw.split('?');
  const [id] = path.split('/');
  return id;
}

/** 解析附加参数，如 #/markdown?file=a.md → { file: 'a.md' } */
export function parseQuery() {
  const raw = window.location.hash;
  const qIndex = raw.indexOf('?');
  if (qIndex < 0) return {};
  return Object.fromEntries(new URLSearchParams(raw.slice(qIndex + 1)));
}

export const currentRoute = () => currentId;

/** 编程式导航 */
export function navigate(id, { replace = false } = {}) {
  const hash = `#/${id}`;
  if (window.location.hash === hash) {
    render(id, { push: false });
    return;
  }
  if (replace) history.replaceState(null, '', hash);
  else window.location.hash = hash;
  if (replace) render(id, { push: false });
}

/** 卸载当前视图（释放 WebSocket、定时器、事件监听，防止内存泄漏 —— AC-11） */
export async function teardownCurrent() {
  if (!currentInstance) return;
  try {
    await currentInstance.unmount?.();
  } catch (err) {
    console.error(`[router] 视图 ${currentId} 卸载时出错：`, err);
  }
  currentInstance = null;
}

async function render(id, { push }) {
  const def = routes.get(id);
  if (!def) {
    notFound?.(id);
    return;
  }
  if (id === currentId && currentInstance) return;

  await teardownCurrent();

  // 切换前先隐藏旧的，避免两个视图同时占用布局产生闪烁
  document.querySelectorAll('.view.is-active').forEach((n) => n.classList.remove('is-active'));

  let container = document.getElementById(`view-${id}`);
  if (!container) {
    container = document.createElement('section');
    container.id = `view-${id}`;
    container.className = def.className || 'view';
    outlet.append(container);
  }
  container.classList.add('is-active');
  // 面板标题用的是 h2，页面级 h1 由这里统一提供（屏幕阅读器可据此定位当前模块）
  container.setAttribute('aria-label', def.title);

  currentId = id;
  document.title = `${def.title} · 全能工具站`;
  setPageHeading(def.title);

  try {
    const module = await def.load();
    const factory = module.default || module.createView;
    if (typeof factory !== 'function') throw new Error(`视图 ${id} 未导出默认工厂函数`);

    currentInstance = await factory(container);
    // 支持视图在挂载后暴露的显示/隐藏钩子
    currentInstance?.onShow?.();
  } catch (err) {
    console.error(`[router] 视图 ${id} 加载失败：`, err);
    container.replaceChildren();
    const box = document.createElement('div');
    box.className = 'empty';
    box.innerHTML = `
      <div class="empty-title">页面加载失败</div>
      <div class="empty-desc">${String(err?.message || err)}</div>`;
    container.append(box);
  }

  for (const fn of changeHandlers) fn(currentId, def);
}

/** 供视图内部监听路由变化（例如刷新时重新拉取数据） */
export const onRouteChange = (fn) => {
  changeHandlers.add(fn);
  return () => changeHandlers.delete(fn);
};

/**
 * 维护页面级 h1。
 *
 * 为什么需要：SPA 外壳里各面板自己的标题是 h2（它们确实是主内容的子区块），
 * 于是整个文档没有任何 h1 —— 依赖标题层级导航的读屏用户会失去"当前在哪一页"的锚点，
 * 搜索引擎也无法判断页面主题。
 *
 * 采用视觉隐藏而非可见标题，是为了不破坏已定的视觉层级（模块名已显示在面板头部）。
 */
function setPageHeading(text) {
  let h1 = document.getElementById('page-heading');
  if (!h1) {
    h1 = document.createElement('h1');
    h1.id = 'page-heading';
    h1.className = 'sr-only';
    // 插到主内容区最前面，保证它在 DOM 顺序上先于所有内容
    outlet.prepend(h1);
  }
  h1.textContent = text;
}
