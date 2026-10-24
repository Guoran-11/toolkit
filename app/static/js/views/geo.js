/**
 * views/geo.js — 地图定位：输入经纬度，自动在地图上标出位置
 *
 * 覆盖需求（新增模块 FR-GEO-01 ~ FR-GEO-06）：
 *   FR-GEO-01  支持十进制度 / 度分秒 / 半球字母 / 中文「北纬东经」四种写法，粘贴即用
 *   FR-GEO-02  输入即定位（防抖自动跟随），无需先点按钮
 *   FR-GEO-03  地图支持拖动平移、滚轮/双指缩放、单击取点（点哪儿读哪儿）
 *   FR-GEO-04  读数面板给出十进制度、度分秒、UTM、GCJ-02、Maidenhead 网格
 *   FR-GEO-05  离线可用的底图（Natural Earth 国界 + 经纬网 + 参考城市），
 *              在线瓦片（OSM）为可选开关，失败自动降级
 *   FR-GEO-06  定位历史（localStorage）与可分享链接（#/geo?lat=&lon=&z=）
 *
 * 为什么不做「获取当前位置」按钮：
 *   后端下发的 Permissions-Policy 显式禁用了 geolocation（见 app/main.py），
 *   这是有意的隐私取舍 —— 本模块只处理用户手工输入/粘贴的坐标。
 */

import { el, clear, debounce, formatRelative, $$, sleep } from '../utils.js';
import { icon } from '../icons.js';
import { CFG } from '../config.js';
import { toast, createCopyButton, attachSplitter } from '../ui.js';
import { createMap, DEFAULT_PROVIDER } from '../geo/basemap.js';
import { decodeCountries, buildCountryIndex, countryAt } from '../geo/topojson.js';
import { nearestPlaces, translateCountry, PRESETS } from '../geo/places.js';
import {
  parseLatLon, parseAngle, formatDMS, formatDecimal, toUTM, maidenhead,
  wgs84ToGcj02, outOfChina, isValidLat, isValidLon, distanceKm, formatDistance,
} from '../geo/coords.js';
import { loadPanelWidth, savePanelWidth } from '../store.js';

/** 定位历史的 localStorage 键（config.js 未定义时回退到字面量，避免耦合顺序） */
const HISTORY_KEY = CFG.storageKeys?.geoHistory || 'toolkit.geo.history';
const HISTORY_MAX = 8;
/** 默认视图：中国全境附近 */
const HOME = { lat: 34.5, lon: 104, zoom: 3.2 };
/** 定位时若当前缩放比这个值小，自动拉近到 CITY_ZOOM（否则保持用户的手动缩放） */
const MIN_LOCATE_ZOOM = 6.5;
const CITY_ZOOM = 8.5;

/* ------------------------------------------------------------------ *
 * 历史记录读写
 * ------------------------------------------------------------------ */

function loadHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
    if (!Array.isArray(raw)) return [];
    return raw.filter((it) => isValidLat(it?.lat) && isValidLon(it?.lon)).slice(0, HISTORY_MAX);
  } catch {
    return [];
  }
}

function saveHistory(list) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, HISTORY_MAX)));
  } catch { /* 隐私模式/配额不足时静默失败，不影响主流程 */ }
}

/* ------------------------------------------------------------------ *
 * 视图工厂
 * ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ *
 * 逆地理编码（省 / 市 / 区县 / 街道 / 道路）
 *
 * 为什么必须有在线接口：离线底图只到国界，无法给出国内行政区划与道路。
 * 两个来源（都不经本站服务器，直接由浏览器请求）：
 *   · 高德 Web 服务 regeo —— 国内最准，需要用户自备免费 Key（存本机 localStorage）
 *   · OpenStreetMap Nominatim —— 无需 Key，全球覆盖，国内细节略少
 * 站点服务器不代理这两个请求：一是无必要（浏览器直连更快），
 * 二是保持服务端零外部依赖（部署机可能没有外网出口）。
 * ------------------------------------------------------------------ */

const AMAP_KEY_STORAGE = 'toolkit.geo.amapKey';
const NOMINATIM_MIN_INTERVAL = 1100; // Nominatim 使用政策：≤ 1 次/秒
let lastNominatimAt = 0;

function readAmapKey() {
  try { return (localStorage.getItem(AMAP_KEY_STORAGE) || '').trim(); } catch { return ''; }
}

/** 从高德 regeo 响应里抽出结构化地址 */
function parseAmap(data) {
  const ac = data?.regeocode?.addressComponent || {};
  const streetNumber = typeof ac.streetNumber === 'object' && ac.streetNumber !== null ? ac.streetNumber : {};
  return {
    provider: '高德地图',
    formatted: data?.regeocode?.formatted_address || '',
    country: ac.country || '',
    province: typeof ac.province === 'string' ? ac.province : '',
    city: typeof ac.city === 'string' && ac.city ? ac.city : (typeof ac.province === 'string' ? ac.province : ''),
    district: ac.district || '',
    township: ac.township || '',
    street: streetNumber.street || (typeof ac.streetNumber === 'string' ? ac.streetNumber : '') || '',
    number: streetNumber.number || '',
  };
}

/** 从 Nominatim 响应里抽出结构化地址 */
function parseNominatim(data) {
  const a = data?.address || {};
  return {
    provider: 'OpenStreetMap',
    formatted: data?.display_name || '',
    country: a.country || '',
    province: a.state || a.province || a.region || '',
    city: a.city || a.town || a.municipality || a.county || '',
    district: a.city_district || a.district || a.suburb || a.borough || '',
    township: a.township || a.village || a.hamlet || a.neighbourhood || '',
    street: a.road || a.pedestrian || a.footway || a.path || '',
    number: a.house_number || '',
  };
}

export default async function createGeoView(container) {
  const state = {
    lat: null,
    lon: null,
    zoom: HOME.zoom,
    online: false,
    country: null,
    baseReady: false,
    error: '',
  };
  let historyList = loadHistory();
  let countryIndex = [];
  let lastLocate = null; // 上一次定位点，用于「距上一点」读数

  container.classList.remove('view--single');
  container.classList.add('view', 'view--split');
  container.innerHTML = '';

  /* ================= 左侧：输入与读数 ================= */

  const smartInput = el('input.input', {
    type: 'text',
    placeholder: '例如 39.9087, 116.3975 或 39°54′31″N 116°23′51″E',
    'aria-label': '粘贴或输入经纬度',
    autocomplete: 'off',
    spellcheck: 'false',
  });

  const latInput = el('input.input', {
    type: 'text',
    inputmode: 'decimal',
    placeholder: '39.9087 或 39°54′31″N',
    'aria-label': '纬度',
    autocomplete: 'off',
  });

  const lonInput = el('input.input', {
    type: 'text',
    inputmode: 'decimal',
    placeholder: '116.3975 或 116°23′51″E',
    'aria-label': '经度',
    autocomplete: 'off',
  });

  const inputHint = el('div.geo-hint');

  /** 高德 Web 服务 Key（可选）：填写后逆地理走高德，国内省市区/道路更准 */
  const amapKeyInput = el('input.input', {
    type: 'text',
    placeholder: '高德 Web 服务 Key（可选）',
    'aria-label': '高德地图 Key',
    autocomplete: 'off',
    spellcheck: 'false',
    value: readAmapKey(),
  });
  amapKeyInput.addEventListener('change', () => {
    const value = amapKeyInput.value.trim();
    try {
      if (value) localStorage.setItem(AMAP_KEY_STORAGE, value);
      else localStorage.removeItem(AMAP_KEY_STORAGE);
    } catch { /* 隐私模式忽略 */ }
    if (isValidLat(state.lat) && isValidLon(state.lon)) refreshAddress();
    toast('info', value ? '已保存高德 Key' : '已清除高德 Key',
      value ? '逆地理将优先使用高德（省/市/区县/街道/门牌）' : '逆地理将使用 OpenStreetMap Nominatim');
  });

  const locateBtn = el('button.btn.btn--primary.btn--block', {
    type: 'button',
    on: { click: () => locateFromInputs({ animate: true }) },
  }, [icon('locate'), el('span', { text: '定位到该坐标' })]);

  const resetBtn = el('button.btn.btn--ghost.btn--block', {
    type: 'button',
    on: { click: () => clearAll() },
  }, [icon('refresh'), el('span', { text: '清空输入与标记' })]);

  /* ---------- 读数面板 ---------- */

  const readoutHost = el('div.geo-readouts');
  /** @type {Record<string, {value:HTMLElement, row:HTMLElement}>} */
  const readouts = {};

  function addReadout(key, label, { copy = true } = {}) {
    const value = el('span.geo-readout-value.t-mono', { text: '—' });
    const row = el('div.geo-readout', {}, [
      el('span.geo-readout-label', { text: label }),
      value,
      copy
        ? createCopyButton(() => value.textContent, { label: '' })
        : null,
    ]);
    readouts[key] = { value, row };
    readoutHost.append(row);
  }

  addReadout('decimal', '十进制度');
  addReadout('dms', '度分秒');
  addReadout('utm', 'UTM 投影');
  addReadout('gcj', 'GCJ-02（火星坐标）');
  addReadout('grid', 'Maidenhead 网格');
  addReadout('country', '国家 / 地区', { copy: false });
  addReadout('admin', '省 / 市 / 区县', { copy: false });
  addReadout('street', '街道 / 道路 / 门牌', { copy: false });
  addReadout('address', '详细地址');
  addReadout('geoSource', '地址来源', { copy: false });
  addReadout('places', '最近参考地点', { copy: false });
  addReadout('distance', '距上一次定位', { copy: false });

  /* ---------- 常用地点 ---------- */

  const presetHost = el('div.geo-presets');
  for (const preset of PRESETS) {
    presetHost.append(el('button.btn.btn--sm.btn--ghost', {
      type: 'button',
      text: preset.label,
      on: {
        click: () => {
          latInput.value = preset.lat.toFixed(4);
          lonInput.value = preset.lon.toFixed(4);
          smartInput.value = `${preset.lat.toFixed(4)}, ${preset.lon.toFixed(4)}`;
          locateFromInputs({ animate: true });
        },
      },
    }));
  }

  /* ---------- 历史记录 ---------- */

  const historyHost = el('div.geo-history');

  function renderHistory() {
    clear(historyHost);
    if (!historyList.length) {
      historyHost.append(el('div.help', { text: '暂无记录。定位过的坐标会保存在本机浏览器中。' }));
      return;
    }
    for (const item of historyList) {
      const label = `${item.lat.toFixed(5)}, ${item.lon.toFixed(5)}`;
      historyHost.append(el('div.geo-history-item', {}, [
        el('button.geo-history-main', {
          type: 'button',
          title: '点击重新定位',
          on: {
            click: () => {
              latInput.value = item.lat.toFixed(6);
              lonInput.value = item.lon.toFixed(6);
              smartInput.value = label;
              locateFromInputs({ animate: true });
            },
          },
        }, [
          icon('pin'),
          el('span.t-mono', { text: label }),
          el('span.t-xs.t-muted', { text: formatRelative(item.ts) }),
        ]),
        el('button.btn.btn--icon.btn--sm.btn--ghost', {
          type: 'button',
          'aria-label': '删除该记录',
          on: {
            click: () => {
              historyList = historyList.filter((x) => x !== item);
              saveHistory(historyList);
              renderHistory();
            },
          },
        }, [icon('close')]),
      ]));
    }
  }

  const leftPanel = el('div.panel.panel--left', {}, [
    el('div.panel-header', {}, [
      el('h2', {}, [icon('map'), el('span', { text: '地图定位' })]),
      el('div.panel-actions', {}, [
        el('span.tag', { text: '离线可用' }),
      ]),
    ]),
    el('div.panel-body.panel-body--grid', {}, [
      el('div.form-section', {}, [
        el('div.form-section-title', {}, [icon('crosshair'), el('span', { text: '输入坐标' })]),
        el('div.field', {}, [
          el('label.label', { text: '整串粘贴（纬度, 经度）' }),
          smartInput,
          inputHint,
        ]),
        el('div.field', {}, [
          el('label.label', { text: '纬度（-90 ~ 90）' }),
          latInput,
        ]),
        el('div.field', {}, [
          el('label.label', { text: '经度（-180 ~ 180）' }),
          lonInput,
        ]),
        el('div.help', {
          text: '支持 39.9087 / 39°54′31″N / N39.9087 / 北纬39.9087 等写法；'
            + '输入或粘贴后会自动定位，也可以直接点击地图取点。',
        }),
      ]),
      el('div.form-section', {}, [
        el('div.form-section-title', {}, [icon('pin'), el('span', { text: '常用地点' })]),
        presetHost,
      ]),
      el('div.form-section', {}, [
        el('div.form-section-title', {}, [icon('hash'), el('span', { text: '地址查询（可选）' })]),
        el('div.field', {}, [
          el('label.label', { text: '高德地图 Key' }),
          amapKeyInput,
        ]),
        el('div.help', {
          text: '省 / 市 / 区县 / 街道 / 道路需要在线逆地理服务：填写高德 Key 时用高德（国内最准），'
            + '留空则用 OpenStreetMap Nominatim（无需 Key）。Key 只保存在本机浏览器，不会上传到本站服务器。',
        }),
      ]),
      el('div.form-section', {}, [
        el('div.form-section-title', {}, [icon('hash'), el('span', { text: '定位读数' })]),
        readoutHost,
      ]),
      el('div.form-section', {}, [
        el('div.form-section-title', {}, [icon('clock'), el('span', { text: '历史记录' })]),
        historyHost,
      ]),
    ]),
    el('div.panel-footer', {}, [
      el('div.col.gap-2', {}, [locateBtn, resetBtn]),
    ]),
  ]);

  /* ================= 右侧：地图 ================= */

  const splitter = el('div.splitter', {
    role: 'separator',
    tabindex: '0',
    'aria-orientation': 'vertical',
    'aria-label': '调整面板宽度（使用左右方向键）',
    'aria-valuemin': '320',
    'aria-valuemax': '620',
    'aria-valuenow': '400',
  });

  const canvas = el('canvas.geo-canvas', {
    tabindex: '0',
    role: 'img',
    'aria-label': '地图：可拖动平移、滚轮缩放、单击取点。当前标记的经纬度见左侧读数面板。',
  });

  const emptyHint = el('div.geo-empty', {}, [
    el('div.geo-empty-icon', {}, [icon('map', 'icon icon--lg')]),
    el('div', { text: '输入经纬度，或直接在地图上单击取点' }),
  ]);

  const centerLabel = el('span.t-xs.t-mono', { text: '—' });
  const zoomLabel = el('span.t-xs.t-mono', { text: `z${HOME.zoom.toFixed(1)}` });

  /** 底图来源切换（离线 / 高德 / OSM） */
  const PROVIDERS = [
    { id: 'amap', label: '高德', tip: '高德瓦片：中文标注、国内道路级（默认，仅需能访问高德瓦片服务）' },
    { id: 'osm', label: 'OSM', tip: 'OpenStreetMap 瓦片：全球覆盖，国内访问可能较慢' },
    { id: 'offline', label: '离线', tip: '离线国界底图 + 经纬网，零外网请求' },
  ];
  const providerHost = el('div.segmented', { role: 'group', 'aria-label': '底图来源' });
  for (const item of PROVIDERS) {
    providerHost.append(el('button.segmented-item', {
      type: 'button',
      text: item.label,
      'data-provider': item.id,
      'data-tip': item.tip,
      'aria-pressed': String(item.id === DEFAULT_PROVIDER),
      on: { click: () => applyProvider(item.id) },
    }));
  }

  const mapToolbar = el('div.geo-toolbar', { role: 'toolbar', 'aria-label': '地图工具' }, [
    providerHost,
    el('button.btn.btn--icon.btn--sm.btn--ghost', {
      type: 'button',
      'aria-label': '放大',
      on: { click: () => map.zoomBy(1) },
    }, [icon('plus')]),
    el('button.btn.btn--icon.btn--sm.btn--ghost', {
      type: 'button',
      'aria-label': '缩小',
      on: { click: () => map.zoomBy(-1) },
    }, [icon('minus')]),
    el('button.btn.btn--icon.btn--sm.btn--ghost', {
      type: 'button',
      'aria-label': '回到全球视图',
      'data-tip': '回到全球视图',
      on: { click: () => map.setView({ ...HOME, zoom: 1.6 }, { animate: true }) },
    }, [icon('globe')]),
  ]);

  const mapStatus = el('div.geo-status', {}, [
    el('span.t-xs.t-muted', { text: '中心' }),
    centerLabel,
    el('span.t-xs.t-muted', { text: '缩放' }),
    zoomLabel,
  ]);

  const mapWrap = el('div.geo-map', {}, [canvas, emptyHint, mapToolbar, mapStatus]);

  const rightPanel = el('div.panel.panel--right', {}, [
    el('div.panel-header', {}, [
      el('h2', {}, [icon('globe'), el('span', { text: '地图' })]),
      el('div.panel-actions', {}, [el('span.geo-attribution-note.t-xs.t-muted', { text: '底图：Natural Earth 110m' })]),
    ]),
    el('div.panel-body.panel-body--flush', {}, [mapWrap]),
  ]);

  container.append(leftPanel, splitter, rightPanel);

  /* ================= 地图初始化 ================= */

  const savedWidth = loadPanelWidth?.();
  if (savedWidth) container.style.setProperty('--panel-w', `${savedWidth}px`);
  attachSplitter(splitter, container, {
    min: 320,
    max: 620,
    initial: savedWidth || 400,
    onChange: (w) => savePanelWidth?.(w),
  });

  const map = createMap(canvas, { center: HOME, zoom: HOME.zoom, minZoom: 0.8, maxZoom: 19 });
  // 默认走高德瓦片（国内可用性最好、带中文道路标注）；瓦片失败会自动退回离线底图
  applyProvider(DEFAULT_PROVIDER);

  map.on('move', ({ zoom }) => {
    state.zoom = zoom;
    updateMapStatus();
    // 拖动/缩放后同步 URL，刷新页面能回到同一视图
    syncUrlDebounced();
  });

  map.on('pick', ({ lat, lon }) => {
    latInput.value = lat.toFixed(6);
    lonInput.value = lon.toFixed(6);
    smartInput.value = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
    setPoint(lat, lon, { animate: false, pulse: true, source: 'click' });
  });

  map.on('tilefail', () => {
    // 瓦片挂了就自动回到离线底图：宁可少细节，也不能给用户一片空白
    state.provider = 'offline';
    map.setProvider('offline');
    markProviderButtons('offline');
    toast('warning', '在线瓦片加载失败', '已自动切回离线底图（国界 + 经纬网）；可换用其它底图来源，定位功能不受影响。');
  });

  /** 同步底图来源按钮的选中态 */
  function markProviderButtons(activeId) {
    for (const btn of $$('.segmented-item', providerHost)) {
      btn.setAttribute('aria-pressed', String(btn.dataset.provider === activeId));
    }
  }

  /** 切换底图来源：高德（默认）/ OSM / 离线 */
  function applyProvider(id) {
    const effective = map.setProvider(id);
    state.provider = effective;
    markProviderButtons(effective);
    if (id !== 'offline' && effective === 'offline') {
      toast('warning', '底图不可用', '该底图来源不可用，保持离线底图。');
    }
    return effective;
  }

  function updateMapStatus() {
    const v = map.getView();
    centerLabel.textContent = `${v.lat.toFixed(4)}, ${v.lon.toFixed(4)}`;
    zoomLabel.textContent = `z${v.zoom.toFixed(1)}`;
  }

  /* ---------- 离线底图数据 ---------- */

  (async () => {
    try {
      const res = await fetch('vendor/geo/countries-110m.json', { cache: 'force-cache' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const topo = await res.json();
      const countries = decodeCountries(topo);
      countryIndex = buildCountryIndex(countries);
      map.setBasemap(countries, countryIndex);
      state.baseReady = true;
      // 底图就绪后补算一次国家名（首屏可能先于数据到达）
      if (state.lat !== null) updateReadouts();
    } catch (err) {
      console.warn('[geo] 离线底图加载失败：', err);
      state.baseReady = false;
      const note = el('div.geo-notice', {
        text: '离线底图数据未加载（演示模式或静态资源缺失），仍可定位、读数与在线取点。',
      });
      mapWrap.append(note);
    }
  })();

  /* ================= 地址：省 / 市 / 区县 / 街道 / 道路 ================= */

  const geocodeCache = new Map();
  let geocodeAbort = null;

  function setAddressRows({ admin, street, address, source }) {
    readouts.admin.value.textContent = admin || '—';
    readouts.street.value.textContent = street || '—';
    if (address !== undefined) readouts.address.value.textContent = address || '—';
    if (source !== undefined) readouts.geoSource.value.textContent = source || '—';
  }

  /** 拼接地址片段并去重（高德对直辖市会给出「北京市 北京市」这种重复） */
  function joinParts(parts) {
    const seen = new Set();
    const out = [];
    for (const part of parts) {
      const value = String(part || '').trim();
      if (!value || seen.has(value)) continue;
      seen.add(value);
      out.push(value);
    }
    return out.join(' ');
  }

  async function reverseGeocode(lat, lon, key) {
    const cacheKey = `${key ? 'amap' : 'osm'}:${lat.toFixed(4)},${lon.toFixed(4)}`;
    if (geocodeCache.has(cacheKey)) return geocodeCache.get(cacheKey);

    geocodeAbort?.abort();
    geocodeAbort = new AbortController();
    const { signal } = geocodeAbort;
    // 逆地理接口在部分网络下会「连不上也不报错」（一直挂起），
    // 因此必须自己加超时，否则读数会永远停在「查询中…」。
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; geocodeAbort?.abort(); }, 8000);
    let info = null;

    try {
      if (key) {
        const gcj = wgs84ToGcj02(lat, lon);
        const url = 'https://restapi.amap.com/v3/geocode/regeo'
          + `?key=${encodeURIComponent(key)}&location=${gcj.lon.toFixed(6)},${gcj.lat.toFixed(6)}`
          + '&extensions=base&radius=1000&output=JSON';
        const res = await fetch(url, { signal });
        const data = await res.json();
        if (data?.status === '1' && data.regeocode) info = parseAmap(data);
        else throw new Error(data?.info || '高德接口返回异常');
      } else {
        // Nominatim 使用政策要求 ≤1 请求/秒
        const wait = Math.max(0, NOMINATIM_MIN_INTERVAL - (Date.now() - lastNominatimAt));
        if (wait > 0) await sleep(wait);
        lastNominatimAt = Date.now();
        const url = 'https://nominatim.openstreetmap.org/reverse'
          + `?format=jsonv2&lat=${lat.toFixed(6)}&lon=${lon.toFixed(6)}&zoom=18&addressdetails=1&accept-language=zh-CN`;
        const res = await fetch(url, { signal, headers: { Accept: 'application/json' } });
        if (!res.ok) throw new Error(`Nominatim HTTP ${res.status}`);
        info = parseNominatim(await res.json());
      }
    } catch (err) {
      if (timedOut) throw new Error('地址服务超时（该网络可能访问不到，可填写高德 Key）');
      throw err;
    } finally {
      clearTimeout(timer);
    }
    if (info) geocodeCache.set(cacheKey, info);
    return info;
  }

  async function refreshAddress() {
    if (!isValidLat(state.lat) || !isValidLon(state.lon)) return;
    const lat = state.lat;
    const lon = state.lon;
    setAddressRows({
      admin: '查询中…',
      street: '—',
      address: '',
      source: readAmapKey() ? '高德地图' : 'OpenStreetMap（Nominatim）',
    });
    try {
      const info = await reverseGeocode(lat, lon, readAmapKey());
      if (lat !== state.lat || lon !== state.lon) return; // 已切到新点，丢弃过期结果
      if (!info) throw new Error('无结果');
      setAddressRows({
        admin: joinParts([info.province, info.city, info.district]) || '—',
        street: joinParts([info.township, info.street, info.number]) || '—',
        address: info.formatted || '—',
        source: info.provider,
      });
    } catch (err) {
      if (err?.name === 'AbortError') return;
      if (lat !== state.lat || lon !== state.lon) return;
      setAddressRows({
        admin: '查询失败',
        street: '—',
        address: err?.message || '网络不可用',
        source: '离线（仅国家 / 最近地点）',
      });
    }
  }

  const geocodeDebounced = debounce(() => refreshAddress(), 700);

  /* ================= 定位与读数 ================= */

  /**
   * 设置当前点：更新状态、标记、读数、URL 与历史。
   * @param {number} lat
   * @param {number} lon
   * @param {{animate?:boolean, pulse?:boolean, source?:string, remember?:boolean}} [opts]
   */
  function setPoint(lat, lon, opts = {}) {
    const { animate = true, pulse = true, remember = true } = opts;
    state.lat = lat;
    state.lon = lon;
    state.error = '';
    inputHint.textContent = '';
    inputHint.classList.remove('is-error');

    map.setMarker(lat, lon, { pulse });
    const v = map.getView();
    const targetZoom = v.zoom < MIN_LOCATE_ZOOM ? CITY_ZOOM : v.zoom;
    map.setView({ lat, lon, zoom: targetZoom }, { animate });
    state.zoom = targetZoom;

    emptyHint.hidden = true;
    updateReadouts();
    updateMapStatus();
    // 省市区/道路是异步查询的（在线接口），防抖后统一刷新
    geocodeDebounced();
    syncUrl({ replace: true });

    if (remember) {
      historyList = [{ lat, lon, ts: Date.now() }, ...historyList.filter((it) => !(Math.abs(it.lat - lat) < 1e-6 && Math.abs(it.lon - lon) < 1e-6))]
        .slice(0, HISTORY_MAX);
      saveHistory(historyList);
      renderHistory();
    }
    lastLocate = { lat, lon };
  }

  function updateReadouts() {
    const { lat, lon } = state;
    if (!isValidLat(lat) || !isValidLon(lon)) return;

    readouts.decimal.value.textContent = `${formatDecimal(lat, 6)}, ${formatDecimal(lon, 6)}`;
    readouts.dms.value.textContent = `${formatDMS(lat, 'lat')} ${formatDMS(lon, 'lon')}`;

    const utm = toUTM(lat, lon);
    readouts.utm.value.textContent = utm
      ? `${utm.label} ${utm.easting}E ${utm.northing}N`
      : '—';

    const gcj = wgs84ToGcj02(lat, lon);
    if (outOfChina(lat, lon)) {
      readouts.gcj.value.textContent = '境外无需换算';
    } else if (gcj) {
      readouts.gcj.value.textContent = `${formatDecimal(gcj.lat, 6)}, ${formatDecimal(gcj.lon, 6)}`;
    } else {
      readouts.gcj.value.textContent = '—';
    }

    readouts.grid.value.textContent = maidenhead(lat, lon) || '—';

    if (state.baseReady) {
      const raw = countryAt(countryIndex, lat, lon);
      const zh = raw ? translateCountry(raw) : null;
      state.country = raw;
      readouts.country.value.textContent = raw
        ? (zh && zh !== raw ? `${zh}（${raw}）` : raw)
        : '公海 / 无陆地';
    } else {
      readouts.country.value.textContent = '底图未就绪';
    }

    const near = nearestPlaces(lat, lon, 3);
    clear(readouts.places.value);
    if (!near.length) {
      readouts.places.value.textContent = '—';
    } else {
      for (const item of near) {
        readouts.places.value.append(el('div.geo-near', {}, [
          el('span', { text: item.name }),
          el('span.t-muted', { text: formatDistance(item.distanceKm) }),
        ]));
      }
    }

    if (lastLocate && (Math.abs(lastLocate.lat - lat) > 1e-9 || Math.abs(lastLocate.lon - lon) > 1e-9)) {
      const d = distanceKm(lastLocate.lat, lastLocate.lon, lat, lon);
      readouts.distance.value.textContent = formatDistance(d);
    } else {
      readouts.distance.value.textContent = '—';
    }
  }

  /** 从输入框读取坐标并定位；失败时给出可读的原因 */
  function locateFromInputs({ animate = true } = {}) {
    const smart = smartInput.value.trim();
    let lat = parseAngle(latInput.value, 'lat');
    let lon = parseAngle(lonInput.value, 'lon');

    // 两个分栏为空但有整串输入 → 走智能解析
    if ((lat === null || lon === null) && smart) {
      const parsed = parseLatLon(smart);
      if (parsed) {
        lat = parsed.lat;
        lon = parsed.lon;
        latInput.value = lat.toFixed(6);
        lonInput.value = lon.toFixed(6);
      }
    }

    if (lat === null && lon === null && !smart) {
      showInputError('请输入经纬度，或粘贴「纬度, 经度」。');
      return;
    }
    if (lat === null) { showInputError('纬度无法识别或超出 -90 ~ 90，例如 39.9087 或 39°54′31″N。'); return; }
    if (lon === null) { showInputError('经度无法识别或超出 -180 ~ 180，例如 116.3975 或 116°23′51″E。'); return; }

    if (!smart) smartInput.value = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
    setPoint(lat, lon, { animate });
  }

  function showInputError(message) {
    state.error = message;
    inputHint.textContent = message;
    inputHint.classList.add('is-error');
  }

  function clearAll() {
    latInput.value = '';
    lonInput.value = '';
    smartInput.value = '';
    inputHint.textContent = '';
    inputHint.classList.remove('is-error');
    state.lat = null;
    state.lon = null;
    state.country = null;
    map.setMarker(null);
    map.setView({ ...HOME }, { animate: true });
    emptyHint.hidden = false;
    for (const key of Object.keys(readouts)) {
      const node = readouts[key].value;
      clear(node);
      node.textContent = '—';
    }
    syncUrl({ replace: true, silent: true, clear: true });
  }

  /** URL 与视图状态同步：#/geo?lat=..&lon=..&z=.. */
  function syncUrl({ replace = true, silent = false, clear = false } = {}) {
    try {
      const params = new URLSearchParams();
      if (!clear && isValidLat(state.lat) && isValidLon(state.lon)) {
        params.set('lat', state.lat.toFixed(6));
        params.set('lon', state.lon.toFixed(6));
        params.set('z', state.zoom.toFixed(2));
      }
      const query = params.toString();
      const hash = query ? `#/geo?${query}` : '#/geo';
      if (window.location.hash === hash) return;
      // 显式用 window.history：本模块内的 historyList 与全局 history 只差几个字母，
      // 用短名极易写错，这里直接写全限定名，避免又踩一次「URL 不更新」的坑。
      if (replace) window.history.replaceState(null, '', hash);
      else window.location.hash = hash;
    } catch { /* 非浏览器环境忽略 */ }
    void silent;
  }

  /** 拖动/缩放过程中会高频触发，URL 同步做防抖，避免每帧一次 replaceState */
  const syncUrlDebounced = debounce(() => syncUrl({ replace: true, silent: true }), 250);

  /* ---------- 输入事件：输入即定位（防抖） ---------- */

  const autoLocate = debounce(() => {
    const hasAny = latInput.value.trim() || lonInput.value.trim() || smartInput.value.trim();
    if (!hasAny) { clearAll(); return; }
    locateFromInputs({ animate: true });
  }, 420);

  smartInput.addEventListener('input', () => {
    // 整串输入时实时回填两个分栏，让用户看到解析结果
    const parsed = parseLatLon(smartInput.value);
    if (parsed) {
      latInput.value = parsed.lat.toFixed(6);
      lonInput.value = parsed.lon.toFixed(6);
      inputHint.textContent = '';
      inputHint.classList.remove('is-error');
    }
    autoLocate();
  });

  for (const input of [latInput, lonInput]) {
    input.addEventListener('input', () => {
      const lat = parseAngle(latInput.value, 'lat');
      const lon = parseAngle(lonInput.value, 'lon');
      if (lat !== null && lon !== null) smartInput.value = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
      autoLocate();
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        autoLocate.cancel?.();
        locateFromInputs({ animate: true });
      }
    });
    // 粘贴一整串坐标到任一字段 → 自动拆成两栏
    input.addEventListener('paste', (e) => {
      const text = e.clipboardData?.getData('text') || '';
      const parsed = parseLatLon(text);
      if (!parsed) return;
      e.preventDefault();
      latInput.value = parsed.lat.toFixed(6);
      lonInput.value = parsed.lon.toFixed(6);
      smartInput.value = `${parsed.lat.toFixed(6)}, ${parsed.lon.toFixed(6)}`;
      autoLocate.cancel?.();
      setPoint(parsed.lat, parsed.lon, { animate: true });
    });
  }

  smartInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      autoLocate.cancel?.();
      locateFromInputs({ animate: true });
    }
  });

  /* ---------- 分享链接 ---------- */

  const shareBtn = createCopyButton(() => {
    if (!isValidLat(state.lat) || !isValidLon(state.lon)) return '';
    const base = `${window.location.origin}${window.location.pathname}`;
    return `${base}#/geo?lat=${state.lat.toFixed(6)}&lon=${state.lon.toFixed(6)}&z=${state.zoom.toFixed(2)}`;
  }, { label: '复制分享链接' });
  readoutHost.append(el('div.geo-readout.geo-readout--action', {}, [el('span.geo-readout-label', { text: '分享' }), shareBtn]));

  /* ================= 初始状态 ================= */

  renderHistory();
  updateMapStatus();

  // 先取出 URL 里的坐标：clearAll() 会把 hash 重置为 #/geo，
  // 若在它之后再读参数，深链接（#/geo?lat=…&lon=…）就永远恢复不了。
  const initialPoint = (() => {
    try {
      const params = new URLSearchParams(window.location.hash.split('?')[1] || '');
      const rawLat = params.get('lat');
      const rawLon = params.get('lon');
      // 必须先判断参数是否存在：Number(null) === 0 会把「没有参数」变成合法的 0°,
      // 于是打开 #/geo 会莫名其妙跳到几内亚湾 (0,0) 并放大到城市级 —— 实测踩过。
      if (rawLat === null || rawLon === null) return null;
      const lat = Number(rawLat);
      const lon = Number(rawLon);
      const rawZ = params.get('z');
      const z = rawZ === null ? NaN : Number(rawZ);
      if (isValidLat(lat) && isValidLon(lon)) {
        return { lat, lon, zoom: Number.isFinite(z) ? z : null };
      }
    } catch { /* 参数异常时保持空状态 */ }
    return null;
  })();

  clearAll();
  emptyHint.hidden = false;

  if (initialPoint) {
    latInput.value = initialPoint.lat.toFixed(6);
    lonInput.value = initialPoint.lon.toFixed(6);
    smartInput.value = `${initialPoint.lat.toFixed(6)}, ${initialPoint.lon.toFixed(6)}`;
    setPoint(initialPoint.lat, initialPoint.lon, { animate: false, remember: false });
    if (initialPoint.zoom !== null) {
      map.setView({ lat: initialPoint.lat, lon: initialPoint.lon, zoom: initialPoint.zoom }, { animate: false });
    }
  }

  /* ================= 生命周期 ================= */

  return {
    onShow() {
      // 从其他视图返回时画布尺寸可能变过，补一次重绘
      map.refresh();
    },
    unmount() {
      map.destroy();
      autoLocate.cancel?.();
      syncUrlDebounced.cancel?.();
    },
  };
}
