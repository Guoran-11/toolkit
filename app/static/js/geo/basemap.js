/**
 * geo/basemap.js — 地图渲染引擎（Canvas，离线优先）
 *
 * 设计取舍：
 *   1. **不引入 Leaflet/OpenLayers**。项目要求离线可用（NFR-08）且 CSP 只允许
 *      `script-src 'self'`，为一张「输入经纬度看点」的地图背上 200 KB 依赖不划算；
 *      这里只实现本项目需要的那部分：Web 墨卡托投影 + 平移缩放 + 打点。
 *   2. **离线底图**用 Natural Earth 110m 国界（107 KB TopoJSON，随站点一起下发），
 *      零外网请求也能看到轮廓、经纬网与参考城市。
 *   3. **在线瓦片是可选项**，默认关闭。开启后按需请求 OSM 瓦片；
 *      任何一张瓦片失败都只是少一块，不会影响底图与标记（失败自动降级）。
 *
 * 精度说明：
 *   所有经纬度 → 屏幕坐标的换算都在 JS 里用 float64 完成，
 *   不依赖 canvas 变换矩阵（大缩放比下矩阵用 float32 存，会出现像素级抖动）。
 */

import { distanceKm, normalizeLon, wgs84ToGcj02 } from './coords.js';
import { PLACES } from './places.js';

const TILE = 256;
/** Web 墨卡托的纬度上限（±85.0511°），超出后 y 会发散 */
const MAX_LAT = 85.05112878;

/**
 * 在线瓦片源。CSP 的 img-src 必须包含这里的域名，见 app/main.py
 *
 * 为什么默认给高德：国内网络环境下 OSM 瓦片经常打不开，而高德瓦片带中文标注、
 * 道路级别足够细，且瓦片端点是公开的栅格服务。注意高德用的是 **GCJ-02** 坐标，
 * 与本站内部统一的 WGS-84 存在数百米偏移，绘制时需要整体平移对齐（见 drawTiles）。
 */
export const TILE_PROVIDERS = {
  amap: {
    id: 'amap',
    name: '高德地图',
    url: 'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
    subdomains: ['1', '2', '3', '4'],
    maxZoom: 18,
    attribution: '底图瓦片 © 高德地图',
    crs: 'gcj02',
  },
  osm: {
    id: 'osm',
    name: 'OpenStreetMap',
    url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    maxZoom: 19,
    attribution: '底图瓦片 © OpenStreetMap 贡献者',
    crs: 'wgs84',
  },
};

/** 默认瓦片源：高德（国内可用性最好），失败时视图会自动退回离线底图 */
export const DEFAULT_PROVIDER = 'amap';

/* ------------------------------------------------------------------ *
 * 投影与几何工具
 * ------------------------------------------------------------------ */

const worldSize = (zoom) => TILE * (2 ** zoom);

/** 纬度 → 墨卡托归一化 y（0 = 北，1 = 南） */
function mercY(lat) {
  const clamped = Math.max(-MAX_LAT, Math.min(MAX_LAT, Number(lat) || 0));
  const s = Math.sin(clamped * Math.PI / 180);
  return 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
}

/** 归一化 y → 纬度 */
function invMercY(y) {
  const n = Math.PI * (1 - 2 * y);
  return (180 / Math.PI) * Math.atan(Math.sinh(n));
}

/** 经纬度 → 世界像素坐标（zoom 层） */
function project(lat, lon, zoom) {
  const size = worldSize(zoom);
  return {
    x: ((normalizeLon(lon) + 180) / 360) * size,
    y: mercY(lat) * size,
  };
}

/** 世界像素坐标 → 经纬度 */
function unproject(x, y, zoom) {
  const size = worldSize(zoom);
  return {
    lon: normalizeLon((x / size) * 360 - 180),
    lat: invMercY(y / size),
  };
}

/** 把国家环列表转成归一化坐标的扁平数组（[x0,y0,x1,y1,...]，x/y ∈ 0..1） */
function flattenRings(countries) {
  const shapes = [];
  for (const country of countries || []) {
    for (const polygon of country.polygons) {
      const rings = [];
      let minX = Infinity;
      let minY = Infinity;
      let maxX = -Infinity;
      let maxY = -Infinity;
      for (const ring of polygon) {
        if (!ring || ring.length < 3) continue;
        const flat = new Float64Array(ring.length * 2);
        for (let i = 0; i < ring.length; i += 1) {
          const x = (ring[i][0] + 180) / 360;
          const y = mercY(ring[i][1]);
          flat[i * 2] = x;
          flat[i * 2 + 1] = y;
          if (x < minX) minX = x;
          if (y < minY) minY = y;
          if (x > maxX) maxX = x;
          if (y > maxY) maxY = y;
        }
        rings.push(flat);
      }
      if (rings.length) shapes.push({ name: country.name, rings, bbox: [minX, minY, maxX, maxY] });
    }
  }
  return shapes;
}

/** 从 CSS 变量读主题色（只在主题变化时重读，避免每帧 getComputedStyle） */
function readTheme(el) {
  const cs = getComputedStyle(el || document.documentElement);
  const pick = (name, fallback) => (cs.getPropertyValue(name) || '').trim() || fallback;
  return {
    sea: pick('--geo-sea', '#dbeafe'),
    land: pick('--geo-land', '#eef2f7'),
    border: pick('--geo-border', '#b9c3d1'),
    grid: pick('--geo-grid', 'rgba(100,116,139,0.28)'),
    gridText: pick('--geo-grid-text', '#64748b'),
    label: pick('--geo-label', '#334155'),
    labelHalo: pick('--geo-label-halo', 'rgba(255,255,255,0.85)'),
    marker: pick('--geo-marker', '#2563eb'),
    markerRing: pick('--geo-marker-ring', 'rgba(37,99,235,0.35)'),
    crosshair: pick('--geo-crosshair', 'rgba(37,99,235,0.55)'),
    scaleText: pick('--geo-label', '#334155'),
  };
}

/* ------------------------------------------------------------------ *
 * 地图控制器
 * ------------------------------------------------------------------ */

/**
 * 在 canvas 上创建地图控制器。
 * @param {HTMLCanvasElement} canvas
 * @param {object} [opts]
 * @param {{lat:number, lon:number}} [opts.center]
 * @param {number} [opts.zoom]
 * @param {number} [opts.minZoom]
 * @param {number} [opts.maxZoom]
 * @returns {object} 控制器
 */
export function createMap(canvas, opts = {}) {
  const ctx = canvas.getContext('2d');
  const listeners = new Map();
  const state = {
    lat: Number(opts.center?.lat ?? 34),
    lon: Number(opts.center?.lon ?? 108),
    zoom: Number(opts.zoom ?? 3),
    marker: null,
    online: false,
    provider: TILE_PROVIDERS[DEFAULT_PROVIDER],
    shapes: [],
    countryIndex: [],
    tileFailed: false,
    /** 本帧是否真的画上了在线瓦片（决定是否还需要矢量底图兜底） */
    tilesReady: false,
  };
  const minZoom = Number(opts.minZoom ?? 0.8);
  const maxZoom = Number(opts.maxZoom ?? 19);

  let theme = null;
  let themeKey = null;
  let rafId = 0;
  let flyToken = 0;
  let pulseUntil = 0;
  let disposed = false;
  let dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
  let width = 0;
  let height = 0;

  /** 当前视图参数（origin 为世界像素坐标到屏幕的偏移） */
  function view() {
    const size = worldSize(state.zoom);
    const center = project(state.lat, state.lon, state.zoom);
    return {
      size,
      center,
      originX: width / 2 - center.x,
      originY: height / 2 - center.y,
      /** 可见范围（归一化坐标） */
      minU: (0 - (width / 2 - center.x)) / size,
      minV: (0 - (height / 2 - center.y)) / size,
      maxU: (width - (width / 2 - center.x)) / size,
      maxV: (height - (height / 2 - center.y)) / size,
    };
  }

  /** 经纬度 → 屏幕坐标（CSS 像素） */
  function toScreen(lat, lon) {
    const v = view();
    return {
      x: ((normalizeLon(lon) + 180) / 360) * v.size + v.originX,
      y: mercY(lat) * v.size + v.originY,
    };
  }

  /** 屏幕坐标 → 经纬度 */
  function fromScreen(x, y) {
    const v = view();
    return unproject(x - v.originX, y - v.originY, state.zoom);
  }

  function emit(event, payload) {
    const fns = listeners.get(event);
    if (!fns) return;
    for (const fn of fns) {
      try { fn(payload); } catch (err) { console.error(`[geo] ${event} 回调异常：`, err); }
    }
  }

  const on = (event, fn) => {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(fn);
    return () => listeners.get(event)?.delete(fn);
  };

  /* ---------------- 尺寸与主题 ---------------- */

  function syncSize() {
    const w = Math.max(1, Math.round(canvas.clientWidth));
    const h = Math.max(1, Math.round(canvas.clientHeight));
    dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
    const dw = Math.round(w * dpr);
    const dh = Math.round(h * dpr);
    if (canvas.width !== dw || canvas.height !== dh) {
      canvas.width = dw;
      canvas.height = dh;
    }
    width = w;
    height = h;
  }

  function syncTheme() {
    const key = `${document.documentElement.getAttribute('data-theme') || 'auto'}|${window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'}`;
    if (key === themeKey && theme) return;
    themeKey = key;
    theme = readTheme(canvas);
  }

  /* ---------------- 在线瓦片 ---------------- */

  const tileCache = new Map(); // "z/x/y" → {img, ok, failed}

  function tileImage(provider, z, x, y) {
    const key = `${provider.id}/${z}/${x}/${y}`;
    let entry = tileCache.get(key);
    if (entry) return entry;
    const img = new Image();
    // 瓦片是跨域资源，但只用于 drawImage（不读像素），无需 CORS
    img.decoding = 'async';
    img.referrerPolicy = 'no-referrer';
    entry = { img, ok: false, failed: false };
    img.onload = () => {
      entry.ok = true;
      if (!disposed) scheduleDraw();
    };
    img.onerror = () => {
      entry.failed = true;
      if (state.online && !state.tileFailed) {
        state.tileFailed = true;
        emit('tilefail', { provider });
      }
    };
    const url = provider.url
      .replace('{z}', String(z))
      .replace('{x}', String(x))
      .replace('{y}', String(y))
      .replace('{s}', (provider.subdomains || [''])[(x + y) % (provider.subdomains || ['']).length]);
    img.src = url;
    tileCache.set(key, entry);
    // 缓存上限：超出后丢弃最早的条目，避免长时间拖图吃内存
    if (tileCache.size > 600) {
      const first = tileCache.keys().next().value;
      tileCache.delete(first);
    }
    return entry;
  }

  function drawTiles(v) {
    const provider = state.provider;
    if (!provider) return 0;

    // GCJ-02 瓦片（高德）需要整体平移，才能与本站的 WGS-84 坐标对齐：
    // 同一个物理位置在 GCJ-02 下的墨卡托像素比 WGS-84 偏数百米，
    // 把瓦片层反向平移这个差值，标记就落在正确的地物上。
    let shiftX = 0;
    let shiftY = 0;
    if (provider.crs === 'gcj02') {
      const gcj = wgs84ToGcj02(state.lat, state.lon);
      if (gcj) {
        const wgsPx = project(state.lat, state.lon, state.zoom);
        const gcjPx = project(gcj.lat, gcj.lon, state.zoom);
        shiftX = gcjPx.x - wgsPx.x;
        shiftY = gcjPx.y - wgsPx.y;
      }
    }

    const z = Math.max(0, Math.min(provider.maxZoom, Math.round(state.zoom)));
    const scale = 2 ** (state.zoom - z);
    const tileWorld = TILE * scale;
    const n = 2 ** z;

    const startX = Math.floor((0 - v.originX) / tileWorld);
    const endX = Math.floor((width - v.originX) / tileWorld);
    const startY = Math.floor((0 - v.originY) / tileWorld);
    const endY = Math.floor((height - v.originY) / tileWorld);

    let drawn = 0;
    for (let ty = startY; ty <= endY; ty += 1) {
      if (ty < 0 || ty >= n) continue;
      for (let tx = startX; tx <= endX; tx += 1) {
        const wrapped = ((tx % n) + n) % n;
        const entry = tileImage(provider, z, wrapped, ty);
        if (!entry.ok) continue;
        const sx = tx * tileWorld + v.originX - shiftX;
        const sy = ty * tileWorld + v.originY - shiftY;
        ctx.drawImage(entry.img, sx, sy, tileWorld, tileWorld);
        drawn += 1;
      }
    }
    return drawn;
  }

  /* ---------------- 离线底图 ---------------- */

  function drawBasemap(v) {
    if (!state.shapes.length) return;
    const { size, originX, originY } = v;
    // 只画真正与视口相交的世界副本：视口 1034px、世界 777px 时才需要左右各补一份；
    // 城市级缩放（世界几十万像素）时这里只剩 offset=0，避免无谓地把整幅世界
    // 用天文数字坐标丢给画布（软件光栅化下这种超大坐标会产生色带/错乱）。
    const minCopy = Math.floor((-originX - size) / size);
    const maxCopy = Math.ceil((width - originX + size) / size);
    const visible = { minU: v.minU, minV: v.minV, maxU: v.maxU, maxV: v.maxV };

    ctx.save();
    // 显式裁剪到视口：即使个别路径坐标很大，也只允许在画布范围内光栅化
    ctx.beginPath();
    ctx.rect(0, 0, width, height);
    ctx.clip();

    for (let copy = minCopy; copy <= maxCopy; copy += 1) {
      const offset = copy * size;
      const shift = copy;
      for (const shape of state.shapes) {
        const [bx0, by0, bx1, by1] = shape.bbox;
        if (bx1 + shift < visible.minU || bx0 + shift > visible.maxU) continue;
        if (by1 < visible.minV || by0 > visible.maxV) continue;

        ctx.beginPath();
        for (const flat of shape.rings) {
          for (let i = 0; i < flat.length; i += 2) {
            const x = flat[i] * size + originX + offset;
            const y = flat[i + 1] * size + originY;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
          }
          ctx.closePath();
        }
        ctx.fillStyle = theme.land;
        ctx.fill();
        ctx.strokeStyle = theme.border;
        ctx.lineWidth = state.zoom > 6 ? 0.6 : 0.8;
        ctx.stroke();
      }
    }
    ctx.restore();
  }

  /* ---------------- 经纬网 ---------------- */

  /** 按缩放级别选一个「好看」的经纬网间隔（度） */
  function gridStep(zoom) {
    const candidates = [30, 15, 10, 5, 2, 1, 0.5, 0.25, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001];
    // 目标：屏幕上相邻网格线间距 ≥ 72px
    for (const step of candidates) {
      const px = (step / 360) * worldSize(zoom);
      if (px >= 72) return step;
    }
    return candidates[candidates.length - 1];
  }

  function formatGridLabel(value, axis) {
    const abs = Math.abs(value);
    const txt = abs >= 1 ? abs.toFixed(abs % 1 === 0 ? 0 : 2) : abs.toFixed(3);
    if (value === 0) return axis === 'lat' ? '0°' : '0°';
    const hemi = axis === 'lat' ? (value > 0 ? 'N' : 'S') : (value > 0 ? 'E' : 'W');
    return `${txt}°${hemi}`;
  }

  function drawGraticule(v) {
    const step = gridStep(state.zoom);
    ctx.save();
    ctx.strokeStyle = theme.grid;
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    ctx.font = '11px system-ui, sans-serif';
    ctx.fillStyle = theme.gridText;

    // 纬线：只遍历可见纬度区间（缩放到街道级时 0.005° 的步长会产生大量无效迭代）
    const latTop = invMercY(v.minV);
    const latBottom = invMercY(v.maxV);
    const firstLat = Math.ceil(latBottom / step) * step;
    for (let lat = firstLat; lat <= latTop + step; lat += step) {
      if (lat < -85 || lat > 85) continue;
      const y = mercY(lat) * v.size + v.originY;
      if (y < -20 || y > height + 20) continue;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.stroke();
      if (step >= 0.05) {
        ctx.fillText(formatGridLabel(lat, 'lat'), 6, y - 3);
      }
    }
    // 经线：按可见范围只画需要的几条
    const startLon = Math.floor((v.minU * 360 - 180) / step) * step;
    const endLon = v.maxU * 360 - 180;
    for (let lon = startLon; lon <= endLon; lon += step) {
      const x = ((normalizeLon(lon) + 180) / 360) * v.size + v.originX;
      if (x < -20 || x > width + 20) continue;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, height);
      ctx.stroke();
      if (step >= 0.05) {
        ctx.fillText(formatGridLabel(lon, 'lon'), x + 4, height - 8);
      }
    }
    ctx.restore();
  }

  /* ---------------- 参考城市 ---------------- */

  function drawPlaces(v) {
    if (state.zoom < 2.2) return;
    const placed = [];
    const maxLabels = 28;
    ctx.save();
    ctx.font = '11px system-ui, sans-serif';
    ctx.textBaseline = 'middle';

    for (const place of PLACES) {
      if (placed.length >= maxLabels) break;
      const p = toScreen(place.lat, place.lon);
      if (p.x < -40 || p.x > width + 40 || p.y < -20 || p.y > height + 20) continue;
      // 简单的标签避让：与已放置标签的包围盒重叠就跳过
      const text = place.name;
      const tw = ctx.measureText(text).width;
      const box = { x: p.x + 6, y: p.y - 8, w: tw + 4, h: 16 };
      const overlap = placed.some((b) => !(box.x > b.x + b.w || box.x + box.w < b.x || box.y > b.y + b.h || box.y + box.h < b.y));
      if (overlap) continue;
      placed.push(box);

      ctx.beginPath();
      ctx.arc(p.x, p.y, 2.4, 0, Math.PI * 2);
      ctx.fillStyle = theme.label;
      ctx.fill();
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = theme.labelHalo;
      ctx.strokeText(text, p.x + 6, p.y);
      ctx.fillStyle = theme.label;
      ctx.fillText(text, p.x + 6, p.y);
    }
    ctx.restore();
  }

  /* ---------------- 标记与十字准星 ---------------- */

  function drawMarker(v) {
    if (!state.marker) return;
    const p = toScreen(state.marker.lat, state.marker.lon);
    const now = performance.now();
    const pulsing = now < pulseUntil;

    ctx.save();
    // 十字准星（贯穿画布的细虚线，便于和经纬网对齐读数）
    ctx.strokeStyle = theme.crosshair;
    ctx.lineWidth = 1;
    ctx.setLineDash([5, 5]);
    ctx.beginPath();
    ctx.moveTo(0, p.y);
    ctx.lineTo(width, p.y);
    ctx.moveTo(p.x, 0);
    ctx.lineTo(p.x, height);
    ctx.stroke();
    ctx.setLineDash([]);

    // 脉冲环（定位后的短暂反馈）
    if (pulsing) {
      const t = 1 - (pulseUntil - now) / 1100;
      ctx.beginPath();
      ctx.arc(p.x, p.y, 8 + t * 26, 0, Math.PI * 2);
      ctx.strokeStyle = theme.markerRing;
      ctx.lineWidth = 2;
      ctx.globalAlpha = Math.max(0, 1 - t);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // 标记点（外白圈保证在任何底图上都清晰）
    ctx.beginPath();
    ctx.arc(p.x, p.y, 7, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.fill();
    ctx.beginPath();
    ctx.arc(p.x, p.y, 5, 0, Math.PI * 2);
    ctx.fillStyle = theme.marker;
    ctx.fill();
    ctx.restore();
  }

  /* ---------------- 比例尺 ---------------- */

  function drawScaleBar(v) {
    const lat = state.lat;
    // 每像素代表多少米（256px 瓦片体系）
    const metersPerPixel = (156543.03392 * Math.cos(lat * Math.PI / 180)) / (2 ** state.zoom);
    const targetPx = 120;
    const rawMeters = metersPerPixel * targetPx;
    const nice = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000, 500000, 1000000, 2000000];
    let meters = nice[nice.length - 1];
    for (const candidate of nice) {
      if (candidate >= rawMeters * 0.5) { meters = candidate; break; }
    }
    const px = meters / metersPerPixel;
    const x = 12;
    const y = height - 16;

    ctx.save();
    ctx.strokeStyle = theme.scaleText;
    ctx.fillStyle = theme.scaleText;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x, y - 5);
    ctx.lineTo(x, y);
    ctx.lineTo(x + px, y);
    ctx.lineTo(x + px, y - 5);
    ctx.stroke();
    ctx.font = '11px system-ui, sans-serif';
    const label = meters >= 1000 ? `${(meters / 1000).toLocaleString('zh-CN')} km` : `${meters} m`;
    ctx.fillText(label, x, y - 8);
    ctx.restore();
  }

  function drawAttribution() {
    const parts = ['底图 © Natural Earth'];
    if (state.online && state.provider) parts.push(state.provider.attribution);
    ctx.save();
    ctx.font = '10px system-ui, sans-serif';
    ctx.fillStyle = theme.gridText;
    ctx.globalAlpha = 0.9;
    ctx.textAlign = 'right';
    ctx.fillText(parts.join(' · '), width - 8, height - 6);
    ctx.restore();
  }

  /* ---------------- 主绘制 ---------------- */

  function draw() {
    if (disposed) return;
    syncSize();
    syncTheme();
    const v = view();

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = theme.sea;
    ctx.fillRect(0, 0, width, height);

    if (state.online) {
      state.tilesReady = drawTiles(v) > 0;
    } else {
      state.tilesReady = false;
    }
    // 瓦片到位时不再叠加矢量底图/经纬网/参考城市：
    // 在线瓦片本身已含道路与地名，叠上去只会糊成一团。
    if (!state.tilesReady) {
      drawBasemap(v);
      drawGraticule(v);
      drawPlaces(v);
    }
    drawMarker(v);
    drawScaleBar(v);
    drawAttribution();

    if (performance.now() < pulseUntil) scheduleDraw();
  }

  function scheduleDraw() {
    if (rafId || disposed) return;
    rafId = requestAnimationFrame(() => {
      rafId = 0;
      draw();
    });
  }

  /* ---------------- 交互 ---------------- */

  const pointers = new Map();
  let dragState = null;
  let pinchState = null;

  function onPointerDown(e) {
    canvas.setPointerCapture?.(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinchState = { distance: Math.hypot(a.x - b.x, a.y - b.y), zoom: state.zoom };
      dragState = null;
    } else if (pointers.size === 1) {
      dragState = { x: e.clientX, y: e.clientY, moved: false, startLat: state.lat, startLon: state.lon };
    }
  }

  function onPointerMove(e) {
    if (!pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (pointers.size >= 2 && pinchState) {
      const [a, b] = [...pointers.values()];
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      if (pinchState.distance > 0) {
        setZoom(pinchState.zoom + Math.log2(distance / pinchState.distance), { silent: true });
        emit('move', { lat: state.lat, lon: state.lon, zoom: state.zoom, reason: 'pinch' });
      }
      return;
    }

    if (!dragState) return;
    const dx = e.clientX - dragState.x;
    const dy = e.clientY - dragState.y;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragState.moved = true;
    if (!dragState.moved) return;
    // 拖动 → 视图反向平移
    const center = project(dragState.startLat, dragState.startLon, state.zoom);
    const next = unproject(center.x - dx, center.y - dy, state.zoom);
    state.lat = Math.max(-MAX_LAT, Math.min(MAX_LAT, next.lat));
    state.lon = normalizeLon(next.lon);
    emit('move', { lat: state.lat, lon: state.lon, zoom: state.zoom, reason: 'drag' });
    scheduleDraw();
  }

  function onPointerUp(e) {
    const wasDrag = dragState;
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinchState = null;
    if (pointers.size === 0) {
      dragState = null;
      // 没有拖动 → 视为「点击取点」
      if (wasDrag && !wasDrag.moved) {
        const rect = canvas.getBoundingClientRect();
        const point = fromScreen(e.clientX - rect.left, e.clientY - rect.top);
        emit('pick', { ...point, source: 'click' });
      }
    }
  }

  function onWheel(e) {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    const before = fromScreen(px, py);
    const delta = -e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0022);
    setZoom(state.zoom + delta, { silent: true });
    const after = fromScreen(px, py);
    // 以光标为锚点：缩放后把锚点下的经纬度拉回原处
    state.lat = Math.max(-MAX_LAT, Math.min(MAX_LAT, state.lat + (before.lat - after.lat)));
    state.lon = normalizeLon(state.lon + (before.lon - after.lon));
    emit('move', { lat: state.lat, lon: state.lon, zoom: state.zoom, reason: 'zoom' });
    scheduleDraw();
  }

  function onKeyDown(e) {
    const stepPx = 60;
    const pan = (dx, dy) => {
      const center = project(state.lat, state.lon, state.zoom);
      const next = unproject(center.x + dx, center.y + dy, state.zoom);
      state.lat = Math.max(-MAX_LAT, Math.min(MAX_LAT, next.lat));
      state.lon = normalizeLon(next.lon);
      emit('move', { lat: state.lat, lon: state.lon, zoom: state.zoom, reason: 'key' });
      scheduleDraw();
    };
    if (e.key === 'ArrowLeft') { pan(-stepPx, 0); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { pan(stepPx, 0); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { pan(0, -stepPx); e.preventDefault(); }
    else if (e.key === 'ArrowDown') { pan(0, stepPx); e.preventDefault(); }
    else if (e.key === '+' || e.key === '=') { setZoom(state.zoom + 0.5); e.preventDefault(); }
    else if (e.key === '-' || e.key === '_') { setZoom(state.zoom - 0.5); e.preventDefault(); }
  }

  canvas.addEventListener('pointerdown', onPointerDown);
  canvas.addEventListener('pointermove', onPointerMove);
  canvas.addEventListener('pointerup', onPointerUp);
  canvas.addEventListener('pointercancel', onPointerUp);
  canvas.addEventListener('wheel', onWheel, { passive: false });
  canvas.addEventListener('dblclick', (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const before = fromScreen(e.clientX - rect.left, e.clientY - rect.top);
    setZoom(state.zoom + 1, { silent: true });
    const after = fromScreen(e.clientX - rect.left, e.clientY - rect.top);
    state.lat = Math.max(-MAX_LAT, Math.min(MAX_LAT, state.lat + (before.lat - after.lat)));
    state.lon = normalizeLon(state.lon + (before.lon - after.lon));
    emit('move', { lat: state.lat, lon: state.lon, zoom: state.zoom, reason: 'dblclick' });
    scheduleDraw();
  });
  canvas.addEventListener('keydown', onKeyDown);

  const resizeObserver = typeof ResizeObserver !== 'undefined'
    ? new ResizeObserver(() => scheduleDraw())
    : null;
  resizeObserver?.observe(canvas);

  /* ---------------- 对外方法 ---------------- */

  function setZoom(zoom, { silent = false } = {}) {
    const next = Math.max(minZoom, Math.min(maxZoom, zoom));
    if (Math.abs(next - state.zoom) < 1e-6) return;
    state.zoom = next;
    if (!silent) emit('move', { lat: state.lat, lon: state.lon, zoom: state.zoom, reason: 'zoom' });
    scheduleDraw();
  }

  function setView(next = {}, { animate = false, duration = 420 } = {}) {
    const target = {
      lat: Math.max(-MAX_LAT, Math.min(MAX_LAT, Number(next.lat ?? state.lat))),
      lon: normalizeLon(next.lon ?? state.lon),
      zoom: Math.max(minZoom, Math.min(maxZoom, Number(next.zoom ?? state.zoom))),
    };
    flyToken += 1;
    const token = flyToken;
    if (!animate) {
      Object.assign(state, target);
      // 非动画路径同样要广播：否则状态栏/URL 会停在旧缩放
      // （例如深链接 ?z=12 打开后仍显示 z8.5）
      emit('move', { lat: state.lat, lon: state.lon, zoom: state.zoom, reason: 'set' });
      scheduleDraw();
      return;
    }
    const from = { lat: state.lat, lon: state.lon, zoom: state.zoom };
    // 经度按最短路径插值，避免从 179° 飞到 -179° 时绕地球一圈
    let dLon = target.lon - from.lon;
    if (dLon > 180) dLon -= 360;
    if (dLon < -180) dLon += 360;
    const start = performance.now();
    const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - ((-2 * t + 2) ** 2) / 2);
    const step = (now) => {
      if (disposed || token !== flyToken) return;
      const t = Math.min(1, (now - start) / duration);
      const k = ease(t);
      state.lat = from.lat + (target.lat - from.lat) * k;
      state.lon = normalizeLon(from.lon + dLon * k);
      state.zoom = from.zoom + (target.zoom - from.zoom) * k;
      // 飞行过程中同步广播视图变化：否则状态栏/URL 会停留在旧值，
      // 出现「读数说 z8.5、画布还停在上一级」的不一致（实测踩过）。
      emit('move', { lat: state.lat, lon: state.lon, zoom: state.zoom, reason: 'fly' });
      scheduleDraw();
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  return {
    on,
    draw: scheduleDraw,
    /** 立刻重绘（尺寸/主题变化后调用） */
    refresh: () => { themeKey = null; scheduleDraw(); },
    setBasemap: (countries, index) => {
      state.shapes = flattenRings(countries);
      state.countryIndex = index || [];
      scheduleDraw();
    },
    setView,
    getView: () => ({ lat: state.lat, lon: state.lon, zoom: state.zoom }),
    zoomBy: (delta) => setZoom(state.zoom + delta),
    setMarker: (lat, lon, { pulse = true } = {}) => {
      if (Number.isFinite(lat) && Number.isFinite(lon)) {
        state.marker = { lat, lon };
        if (pulse) pulseUntil = performance.now() + 1100;
      } else {
        state.marker = null;
      }
      scheduleDraw();
    },
    getMarker: () => (state.marker ? { ...state.marker } : null),
    setOnline: (online) => {
      state.online = Boolean(online) && Boolean(state.provider);
      if (state.online) state.tileFailed = false;
      scheduleDraw();
    },
    isOnline: () => state.online,
    /**
     * 切换底图来源。
     * @param {'amap'|'osm'|'offline'} id
     * @returns {string} 实际生效的来源
     */
    setProvider: (id) => {
      if (id === 'offline') {
        state.online = false;
        state.tilesReady = false;
        scheduleDraw();
        return 'offline';
      }
      const provider = TILE_PROVIDERS[id];
      if (!provider) return 'offline';
      state.provider = provider;
      state.online = true;
      state.tileFailed = false;
      state.tilesReady = false;
      scheduleDraw();
      return provider.id;
    },
    getProvider: () => (state.online && state.provider ? state.provider.id : 'offline'),
    /** 当前是否只有离线底图在起作用（瓦片没到位） */
    isOfflineFallback: () => state.online && !state.tilesReady,
    toScreen,
    fromScreen,
    /** 屏幕像素 → 地面距离（用于「拖动取点」提示） */
    metersPerPixel: () => (156543.03392 * Math.cos(state.lat * Math.PI / 180)) / (2 ** state.zoom),
    distanceTo: (lat, lon) => (state.marker ? distanceKm(state.marker.lat, state.marker.lon, lat, lon) : null),
    destroy: () => {
      disposed = true;
      cancelAnimationFrame(rafId);
      resizeObserver?.disconnect();
      canvas.removeEventListener('pointerdown', onPointerDown);
      canvas.removeEventListener('pointermove', onPointerMove);
      canvas.removeEventListener('pointerup', onPointerUp);
      canvas.removeEventListener('pointercancel', onPointerUp);
      canvas.removeEventListener('wheel', onWheel);
      canvas.removeEventListener('keydown', onKeyDown);
      listeners.clear();
      tileCache.clear();
    },
  };
}
