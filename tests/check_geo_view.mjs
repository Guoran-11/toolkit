/**
 * tests/check_geo_view.mjs — 地图定位视图的集成自检（Node + 极简 DOM/Canvas 替身）
 *
 * 运行方式：
 *     node tests/check_geo_view.mjs
 *
 * 为什么需要它：
 *   本模块的核心行为（防抖自动定位、URL 参数恢复、单击取点、读数换算、历史落盘）
 *   全在视图层里。没有浏览器时，这些逻辑最容易「看起来写完了、实际没接上」。
 *   这里用一个极简 DOM 替身把真实视图跑起来，并对 canvas 记录到的绘制调用做断言
 *   —— 包括**标记是否真的画在画布中心**（等价于验证投影链路端到端正确）。
 *
 * 与 check_geo.mjs 的分工：
 *   check_geo.mjs       纯函数（解析/换算/解码）—— 不碰 DOM
 *   check_geo_view.mjs  视图装配与交互 —— 用替身 DOM 跑真实模块
 *
 * 注意：视图模块内部使用相对导入（'../utils.js'），而仓库根目录没有 package.json，
 *   Node 会把 .js 当 CommonJS。因此先把 js/ 复制到 .verify/ 下并补一个
 *   {"type":"module"}，再从副本导入 —— 不改动被发布的代码。
 */

import { readFileSync, writeFileSync, mkdirSync, cpSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SCRATCH = join(ROOT, '.verify', 'geo-view');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const PASS = [];
const FAIL = [];
function check(name, ok, detail = '') {
  (ok ? PASS : FAIL).push(name);
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? `  -> ${detail}` : ''}`);
}

/* ================================================================== *
 * 1. 极简 DOM 替身（只实现被测代码真正用到的那部分）
 * ================================================================== */

/** DOM 节点基类：utils.js 的 el() 会做 `child instanceof Node` 判断 */
class DomNode {}

class ClassList {
  constructor(node) { this.node = node; this.set = new Set(); }
  add(...names) { names.filter(Boolean).forEach((n) => this.set.add(n)); this.sync(); }
  remove(...names) { names.forEach((n) => this.set.delete(n)); this.sync(); }
  toggle(name, force) {
    const want = force === undefined ? !this.set.has(name) : Boolean(force);
    if (want) this.set.add(name); else this.set.delete(name);
    this.sync();
    return want;
  }
  contains(name) { return this.set.has(name); }
  get value() { return [...this.set].join(' '); }
  sync() { this.node._className = [...this.set].join(' '); }
}

class Style {
  constructor() { this.props = new Map(); }
  setProperty(k, v) { this.props.set(k, String(v)); this[k] = String(v); }
  removeProperty(k) { this.props.delete(k); delete this[k]; }
  getPropertyValue(k) { return this.props.get(k) ?? ''; }
  set cssText(v) { this._cssText = v; }
  get cssText() { return this._cssText || ''; }
}

class TextNode extends DomNode {
  constructor(text) { super(); this.nodeType = 3; this.textContent = String(text); this.children = []; }
  get parentNode() { return this._parent || null; }
  set parentNode(p) { this._parent = p; }
}

class Element extends DomNode {
  constructor(tag) {
    super();
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attrs = new Map();
    this.listeners = new Map();
    this.classList = new ClassList(this);
    this.style = new Style();
    this.dataset = {};
    this.hidden = false;
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this._text = '';
    this._html = '';
    this._w = 800;
    this._h = 600;
    this._ctx = null;
    this._parent = null;
  }

  get className() { return this._className || ''; }
  set className(v) {
    this.classList.set = new Set(String(v).split(/\s+/).filter(Boolean));
    this.classList.sync();
  }
  get classListSet() { return this.classList.set; }
  get parentNode() { return this._parent; }
  set parentNode(p) { this._parent = p; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get childNodes() { return this.children; }
  get nextSibling() {
    if (!this._parent) return null;
    const i = this._parent.children.indexOf(this);
    return this._parent.children[i + 1] || null;
  }
  get previousElementSibling() {
    if (!this._parent) return null;
    const i = this._parent.children.indexOf(this);
    for (let k = i - 1; k >= 0; k -= 1) if (this._parent.children[k].nodeType === 1) return this._parent.children[k];
    return null;
  }

  setAttribute(name, value) {
    this.attrs.set(name, String(value));
    if (name === 'class') this.className = String(value);
    if (name === 'hidden') this.hidden = true;
    if (name === 'value') this.value = String(value);
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_m, c) => c.toUpperCase());
      this.dataset[key] = String(value);
    }
  }
  getAttribute(name) {
    if (name === 'class') return this.className;
    return this.attrs.has(name) ? this.attrs.get(name) : null;
  }
  removeAttribute(name) {
    this.attrs.delete(name);
    if (name === 'hidden') this.hidden = false;
  }
  hasAttribute(name) { return this.attrs.has(name); }

  append(...nodes) {
    for (const node of nodes) {
      if (node === null || node === undefined || node === false) continue;
      const child = typeof node === 'string' ? new TextNode(node) : node;
      child.parentNode = this;
      this.children.push(child);
    }
  }
  appendChild(node) { this.append(node); return node; }
  prepend(...nodes) { this.children.unshift(...nodes); }
  removeChild(node) {
    const i = this.children.indexOf(node);
    if (i >= 0) { this.children.splice(i, 1); node.parentNode = null; }
    return node;
  }
  remove() { this.parentNode?.removeChild(this); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }

  get textContent() {
    if (this.children.length === 0) return this._text;
    return this.children.map((c) => c.textContent).join('');
  }
  set textContent(v) { this.children = []; this._text = String(v); }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); }

  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(fn);
  }
  removeEventListener(type, fn) { this.listeners.get(type)?.delete(fn); }
  dispatchEvent(event) {
    const evt = { type: event.type, target: this, currentTarget: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, ...event };
    for (const fn of [...(this.listeners.get(event.type) || [])]) fn(evt);
    return true;
  }

  /* --- 选择器：够用即可（tag / .class / #id / [attr] / [attr="v"] / 后代） --- */
  _matches(sel) {
    if (String(sel).trim() === '*') return true;
    const m = /^([a-zA-Z]*)((?:[.#][\w-]+)*)((?:\[[^\]]+\])*)$/.exec(sel);
    if (!m) return false;
    const [, tag, classes, attrs] = m;
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    for (const cls of classes.match(/\.[\w-]+/g) || []) {
      if (!this.classList.contains(cls.slice(1))) return false;
    }
    for (const attr of attrs.match(/\[[^\]]+\]/g) || []) {
      const inner = attr.slice(1, -1);
      const eq = inner.indexOf('=');
      if (eq === -1) {
        if (!this.attrs.has(inner)) return false;
      } else {
        const name = inner.slice(0, eq);
        const want = inner.slice(eq + 1).replace(/^["']|["']$/g, '');
        if (this.getAttribute(name) !== want) return false;
      }
    }
    return true;
  }
  _descendants(out = []) {
    for (const child of this.children) {
      if (child.nodeType !== 1) continue;
      out.push(child);
      child._descendants(out);
    }
    return out;
  }
  querySelectorAll(sel) {
    // 逐段匹配后代选择器：'.a .b .c' → 先找全部 .a，再在各自后代里找 .b，以此类推
    const parts = String(sel).trim().split(/\s+/);
    let current = [this];
    for (const part of parts) {
      const next = [];
      for (const node of current) {
        for (const candidate of node._descendants()) {
          if (candidate._matches(part) && !next.includes(candidate)) next.push(candidate);
        }
      }
      current = next;
    }
    return current;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  getElementById(id) {
    return this.querySelectorAll('*').find((n) => n.getAttribute('id') === id) || null;
  }

  getBoundingClientRect() {
    return { left: 0, top: 0, right: this._w, bottom: this._h, width: this._w, height: this._h, x: 0, y: 0 };
  }
  get clientWidth() { return this._w; }
  get clientHeight() { return this._h; }
  get width() { return this._w; }
  set width(v) { this._w = Number(v); }
  get height() { return this._h; }
  set height(v) { this._h = Number(v); }
  focus() { this._focused = true; }
  setPointerCapture() {}
  scrollIntoView() {}
  getContext() { if (!this._ctx) this._ctx = new FakeContext(); return this._ctx; }
  get offsetWidth() { return this._w; }
  get offsetHeight() { return this._h; }
}

/** Canvas 2D 替身：把所有绘制调用记下来，供断言检查 */
class FakeContext {
  constructor() {
    this.calls = [];
    this.fillStyle = '';
    this.strokeStyle = '';
    this.lineWidth = 1;
    this.font = '';
    this.textAlign = 'start';
    this.textBaseline = 'alphabetic';
    this.globalAlpha = 1;
    this._path = [];
  }
  _rec(name, args) { this.calls.push({ name, args }); }
  setTransform(...a) { this._rec('setTransform', a); }
  save() { this._rec('save', []); }
  restore() { this._rec('restore', []); }
  clearRect(...a) { this._rec('clearRect', a); }
  fillRect(...a) { this._rec('fillRect', a); }
  beginPath() { this._path = []; this._rec('beginPath', []); }
  moveTo(...a) { this._path.push(a); this._rec('moveTo', a); }
  lineTo(...a) { this._path.push(a); this._rec('lineTo', a); }
  closePath() { this._rec('closePath', []); }
  rect(...a) { this._path.push(a); this._rec('rect', a); }
  clip() { this._rec('clip', []); }
  arc(...a) { this._rec('arc', a); }
  fill() { this._rec('fill', [this.fillStyle, this._path.length]); }
  stroke() { this._rec('stroke', [this.strokeStyle, this._path.length]); }
  setLineDash(...a) { this._rec('setLineDash', a); }
  fillText(...a) { this._rec('fillText', a); }
  strokeText(...a) { this._rec('strokeText', a); }
  measureText(text) { return { width: String(text).length * 6 }; }
  drawImage(...a) { this._rec('drawImage', a); }
  createLinearGradient() { return { addColorStop() {} }; }
  getImageData() { throw new Error('替身 canvas 不支持 getImageData'); }
}

/* ================================================================== *
 * 2. 全局环境装配
 * ================================================================== */

const storage = new Map();
const documentElement = new Element('html');
documentElement.setAttribute('data-theme', 'light');
const body = new Element('body');

const documentShim = {
  documentElement,
  body,
  title: '',
  createElement: (tag) => new Element(tag),
  createElementNS: (_ns, tag) => new Element(tag),
  createTextNode: (t) => new TextNode(t),
  getElementById: (id) => body.getElementById(id) || documentElement.getElementById(id),
  querySelector: (sel) => body.querySelector(sel),
  querySelectorAll: (sel) => body.querySelectorAll(sel),
  execCommand: () => true,
  addEventListener() {},
  removeEventListener() {},
};

const locationShim = {
  href: 'http://127.0.0.1:5111/#/geo',
  origin: 'http://127.0.0.1:5111',
  pathname: '/',
  search: '',
  hash: '#/geo',
};

const windowShim = {
  location: locationShim,
  history: {
    replaceState(_state, _title, url) {
      const i = String(url).indexOf('#');
      locationShim.hash = i >= 0 ? String(url).slice(i) : '';
    },
  },
  devicePixelRatio: 1,
  innerWidth: 1440,
  innerHeight: 900,
  isSecureContext: true,
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  addEventListener() {},
  removeEventListener() {},
  requestAnimationFrame: (cb) => setTimeout(() => cb(Date.now()), 16),
  cancelAnimationFrame: (id) => clearTimeout(id),
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  ResizeObserver: class { observe() {} disconnect() {} },
  Image: class { set src(v) { this._src = v; } },
};

/** 覆盖全局对象：Node 里部分内建全局（如 navigator）只有 getter，直接赋值会抛错 */
function defineGlobal(name, value) {
  try {
    globalThis[name] = value;
  } catch {
    Object.defineProperty(globalThis, name, { value, writable: true, configurable: true });
  }
}

defineGlobal('Node', DomNode);
defineGlobal('window', windowShim);
defineGlobal('document', documentShim);
defineGlobal('location', locationShim);
defineGlobal('localStorage', {
  getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => storage.set(k, String(v)),
  removeItem: (k) => storage.delete(k),
});
defineGlobal('getComputedStyle', windowShim.getComputedStyle);
defineGlobal('requestAnimationFrame', windowShim.requestAnimationFrame);
defineGlobal('cancelAnimationFrame', windowShim.cancelAnimationFrame);
defineGlobal('ResizeObserver', windowShim.ResizeObserver);
defineGlobal('Image', windowShim.Image);
if (!globalThis.performance) defineGlobal('performance', { now: () => Date.now() });
defineGlobal('navigator', { clipboard: undefined, userAgent: 'node' });

// fetch 替身：把视图请求的离线底图映射到仓库里的真实文件；外部逆地理接口给固定桩数据
globalThis.fetch = async (url) => {
  const href = String(url);
  if (href.includes('nominatim.openstreetmap.org')) {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        display_name: '南池子大街, 东华门街道, 东城区, 北京市, 中国',
        address: {
          country: '中国', state: '北京市', city: '北京市', city_district: '东城区',
          suburb: '东华门街道', road: '南池子大街', house_number: '1号',
        },
      }),
    };
  }
  if (href.includes('restapi.amap.com')) {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        status: '1',
        regeocode: {
          formatted_address: '北京市东城区东华门街道南池子大街1号',
          addressComponent: {
            country: '中国', province: '北京市', city: '北京市', district: '东城区',
            township: '东华门街道', streetNumber: { street: '南池子大街', number: '1号' },
          },
        },
      }),
    };
  }
  const target = href.replace(/^\.?\//, '');
  const abs = join(ROOT, 'app', 'static', target);
  if (!existsSync(abs)) return { ok: false, status: 404, json: async () => ({}) };
  const text = readFileSync(abs, 'utf8');
  return { ok: true, status: 200, json: async () => JSON.parse(text) };
};

/* ================================================================== *
 * 3. 以 ESM 方式导入真实视图模块
 * ================================================================== */

mkdirSync(SCRATCH, { recursive: true });
cpSync(join(ROOT, 'app', 'static', 'js'), join(SCRATCH, 'js'), { recursive: true });
writeFileSync(join(SCRATCH, 'package.json'), '{"type":"module"}\n', 'utf8');

const viewUrl = pathToFileURL(join(SCRATCH, 'js', 'views', 'geo.js')).href;
const { default: createGeoView } = await import(viewUrl);

/* ------------------------------------------------------------------ *
 * 0. 路由回归：带查询串的 hash 必须仍能命中视图
 *    （#/geo?lat=…&lon=… 是地图定位的分享链接形式；router 若不切掉查询串，
 *      会把「geo?lat=…」当成视图 id，查不到就跳「页面不存在」并切走视图）
 * ------------------------------------------------------------------ */
const { defineRoute, startRouter } = await import(pathToFileURL(join(SCRATCH, 'js', 'router.js')).href);
defineRoute({ id: 'geo', title: '地图定位', load: async () => ({ default: async () => ({}) }) });
locationShim.hash = '#/geo?lat=39.9087&lon=116.3975&z=9';
const outletEl = new Element('main');
outletEl.setAttribute('id', 'main');
body.append(outletEl);
startRouter({ outlet: outletEl, defaultId: 'geo', onChange: () => {} });
await sleep(80);
check('带查询串的 hash 能命中视图（分享链接不会被弹走）',
  Boolean(outletEl.querySelector('#view-geo')), locationShim.hash);
outletEl.remove();

/* 独立的投影实现（用于校验标记落点，不复用被测代码） */
const mercY = (lat) => {
  const s = Math.sin(Math.max(-85.05112878, Math.min(85.05112878, lat)) * Math.PI / 180);
  return 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
};

/* ================================================================== *
 * 4. 挂载视图并断言
 * ================================================================== */

console.log('\n== 1. 视图装配 ==');
locationShim.hash = '#/geo?lat=39.9087&lon=116.3975&z=9';
const container = new Element('section');
container.setAttribute('id', 'view-geo');
container.className = 'view view--split';
body.append(container);

const instance = await createGeoView(container);
await sleep(120); // 等底图 fetch（替身是同步的，这里只是让微任务跑完）

const canvas = container.querySelector('.geo-canvas');
check('画布已创建', Boolean(canvas));
check('左右两个面板已装配', container.querySelectorAll('.panel').length === 2,
  `${container.querySelectorAll('.panel').length} 个`);
check('坐标输入框 4 个（整串 + 纬度 + 经度 + 高德 Key）', container.querySelectorAll('input').length === 4,
  `${container.querySelectorAll('input').length} 个`);
check('常用地点预设已渲染（15 个）', container.querySelectorAll('.geo-presets .btn').length === 15,
  `${container.querySelectorAll('.geo-presets .btn').length} 个`);
check('读数行已渲染（12 项 + 分享）', container.querySelectorAll('.geo-readout').length === 13,
  `${container.querySelectorAll('.geo-readout').length} 行`);
check('地图工具条含底图来源切换（高德 / OSM / 离线）',
  container.querySelectorAll('.geo-toolbar .segmented-item').length === 3,
  `${container.querySelectorAll('.geo-toolbar .segmented-item').length} 个`);
check('底图默认选中高德',
  container.querySelector('.geo-toolbar .segmented-item[aria-pressed="true"]')?.textContent === '高德');

console.log('\n== 2. URL 参数直接定位（#/geo?lat=…&lon=…） ==');
const readoutMap = () => {
  const out = {};
  for (const row of container.querySelectorAll('.geo-readout')) {
    const label = row.querySelector('.geo-readout-label')?.textContent?.trim();
    const value = row.querySelector('.geo-readout-value');
    if (label && value) out[label] = value.textContent.trim();
  }
  return out;
};
let readouts = readoutMap();
console.log('  读数：', JSON.stringify(readouts));

check('十进制度读数', (readouts['十进制度'] || '').includes('39.908700') && (readouts['十进制度'] || '').includes('116.397500'), readouts['十进制度']);
check('度分秒读数', (readouts['度分秒'] || '').includes('39°54′31.3″N'), readouts['度分秒']);
check('UTM 读数（50S）', (readouts['UTM 投影'] || '').includes('50S'), readouts['UTM 投影']);
check('Maidenhead 读数（OM89）', (readouts['Maidenhead 网格'] || '').startsWith('OM89'), readouts['Maidenhead 网格']);
check('GCJ-02 换算读数', /39\.9\d+/.test(readouts['GCJ-02（火星坐标）'] || ''), readouts['GCJ-02（火星坐标）']);
check('离线逆地理命中中国', (readouts['国家 / 地区'] || '').includes('中国'), readouts['国家 / 地区']);
check('最近地点命中天安门', (readouts['最近参考地点'] || '').includes('天安门'), readouts['最近参考地点']);

// 逆地理（省/市/区县/街道）是防抖 700ms + 异步接口，等它回来
await sleep(1600);
readouts = readoutMap();
check('省 / 市 / 区县 已填充', (readouts['省 / 市 / 区县'] || '').includes('东城区'), readouts['省 / 市 / 区县']);
check('街道 / 道路 / 门牌 已填充', (readouts['街道 / 道路 / 门牌'] || '').includes('南池子大街'), readouts['街道 / 道路 / 门牌']);
check('详细地址 已填充', (readouts['详细地址'] || '').includes('北京市'), readouts['详细地址']);
check('地址来源标注为 Nominatim', (readouts['地址来源'] || '').includes('OpenStreetMap'), readouts['地址来源']);

console.log('\n== 3. 画布绘制（标记必须落在画布中心） ==');
const ctx = canvas.getContext('2d');
const fills = ctx.calls.filter((c) => c.name === 'fill');
const strokes = ctx.calls.filter((c) => c.name === 'stroke');
// 注意：城市级缩放下视野内只剩少数几个国家，视口剔除会让 fill 次数明显变少
// （这正是 culling 生效的证据），所以阈值只用来确认「确实画了」。
check('已绘制底图（fill 调用 > 10 次）', fills.length > 10, `fill=${fills.length}`);
check('已绘制国界/网格（stroke 调用 > 10 次）', strokes.length > 10, `stroke=${strokes.length}`);
check('陆地色被使用', fills.some((c) => String(c.args[0]).toLowerCase() === '#eef2f7'),
  JSON.stringify(fills.map((c) => c.args[0]).slice(0, 3)));

const bigPath = fills.filter((c) => (c.args[1] || 0) > 200);
check('存在顶点数 > 200 的填充路径（真实国界多边形）', bigPath.length > 0, `最大顶点数=${Math.max(0, ...fills.map((c) => c.args[1] || 0))}`);

const arcs = ctx.calls.filter((c) => c.name === 'arc');
const centerArc = arcs.find((c) => Math.abs(c.args[0] - canvas.clientWidth / 2) < 1.5 && Math.abs(c.args[1] - canvas.clientHeight / 2) < 1.5);
check('标记绘制在画布中心（视图已居中到该坐标）', Boolean(centerArc),
  centerArc ? `arc(${centerArc.args[0].toFixed(1)}, ${centerArc.args[1].toFixed(1)})` : `画布中心=${canvas.clientWidth / 2},${canvas.clientHeight / 2}，arc 样本=${JSON.stringify(arcs.slice(0, 3).map((a) => a.args))}`);

const markerFill = fills.some((c) => String(c.args[0]).toLowerCase().includes('#2563eb'));
check('标记使用主题标记色', markerFill, String(fills.map((c) => c.args[0]).filter((s) => String(s).startsWith('#')).slice(0, 4)));

console.log('\n== 4. 输入即定位（防抖） ==');
const inputs = container.querySelectorAll('input');
const [smartInput, latInput, lonInput] = inputs;
const typeInto = (node, value) => { node.value = value; node.dispatchEvent({ type: 'input' }); };

typeInto(latInput, '31.2304');
typeInto(lonInput, '121.4737');
await sleep(900); // 防抖 420ms + 飞行动画
readouts = readoutMap();
check('防抖后自动定位到上海', (readouts['十进制度'] || '').includes('31.230400'), readouts['十进制度']);
check('最近地点更新为上海', (readouts['最近参考地点'] || '').includes('上海'), readouts['最近参考地点']);
check('整串输入框被回填', smartInput.value.includes('31.230400'), smartInput.value);

console.log('\n== 5. 历史记录与持久化 ==');
const historyItems = container.querySelectorAll('.geo-history-item');
// 深链接进入时不记历史（remember:false），所以此时只有「防抖定位」写入的 1 条
check('历史列表已写入（≥ 1 条）', historyItems.length >= 1, `${historyItems.length} 条`);
const stored = JSON.parse(storage.get('toolkit.geo.history') || '[]');
check('localStorage 已持久化历史', Array.isArray(stored) && stored.length >= 1, JSON.stringify(stored.slice(0, 2)));
check('历史条目含经纬度', stored.every((it) => Number.isFinite(it.lat) && Number.isFinite(it.lon)));
check('历史条目去重（同一坐标只留一条）', stored.filter((it) => Math.abs(it.lat - 31.2304) < 1e-9).length === 1);

console.log('\n== 6. 单击地图取点 ==');
// 不依赖「猜测当前缩放/中心」：视图在定位后一定把标记居中，
// 所以「点画布正中心」必须得到与当前标记一致的经纬度 —— 这是个不变量断言。
const beforePick = readoutMap()['十进制度'];
const [markerLat, markerLon] = beforePick.split(',').map((s) => Number(s.trim()));
const centerX = canvas.clientWidth / 2;
const centerY = canvas.clientHeight / 2;
canvas.dispatchEvent({ type: 'pointerdown', pointerId: 1, clientX: centerX, clientY: centerY });
canvas.dispatchEvent({ type: 'pointerup', pointerId: 1, clientX: centerX, clientY: centerY });
await sleep(80);
const pickedLat = Number(latInput.value);
const pickedLon = Number(lonInput.value);
check('点画布中心 → 取回当前标记坐标（视图确实居中了）',
  Math.abs(pickedLat - markerLat) < 0.01 && Math.abs(pickedLon - markerLon) < 0.01,
  `标记=${beforePick} 取点=${pickedLat}, ${pickedLon}`);

// 方向性断言：往右点 → 经度变大（向东）；往下点 → 纬度变小（向南）
canvas.dispatchEvent({ type: 'pointerdown', pointerId: 2, clientX: centerX + 120, clientY: centerY });
canvas.dispatchEvent({ type: 'pointerup', pointerId: 2, clientX: centerX + 120, clientY: centerY });
await sleep(80);
const eastLon = Number(lonInput.value);
canvas.dispatchEvent({ type: 'pointerdown', pointerId: 3, clientX: centerX, clientY: centerY + 120 });
canvas.dispatchEvent({ type: 'pointerup', pointerId: 3, clientX: centerX, clientY: centerY + 120 });
await sleep(80);
const southLat = Number(latInput.value);
check('向右取点经度增大（东）', eastLon > pickedLon, `${pickedLon} → ${eastLon}`);
check('向下取点纬度减小（南）', southLat < pickedLat, `${pickedLat} → ${southLat}`);
check('点击取点写入历史', container.querySelectorAll('.geo-history-item').length >= 2,
  `${container.querySelectorAll('.geo-history-item').length} 条`);

console.log('\n== 7. URL 同步 ==');
check('URL 带上了坐标参数', /#\/geo\?lat=[\d.]+&lon=[\d.]+&z=/.test(locationShim.hash), locationShim.hash);

console.log('\n== 8. 清空 ==');
const clearBtn = container.querySelectorAll('.panel-footer .btn').find((b) => b.textContent.includes('清空'));
clearBtn.dispatchEvent({ type: 'click' });
await sleep(60);
const afterClear = readoutMap();
check('清空后读数归零', (afterClear['十进制度'] || '') === '—', afterClear['十进制度']);
check('清空后输入框为空', latInput.value === '' && lonInput.value === '' && smartInput.value === '');
check('清空后 URL 去掉坐标参数', locationShim.hash === '#/geo', locationShim.hash);

instance.unmount();

console.log('\n' + '='.repeat(68));
console.log(`结果：${PASS.length} 项通过，${FAIL.length} 项失败`);
if (FAIL.length) {
  console.log('失败项：');
  for (const name of FAIL) console.log(`  · ${name}`);
}
console.log('='.repeat(68));
process.exit(FAIL.length ? 1 : 0);
