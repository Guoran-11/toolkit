/**
 * geo/coords.js — 经纬度的解析、格式化与坐标换算（纯函数，零依赖）
 *
 * 为什么单独成文件：
 *   经纬度有四种常见写法（十进制度、度分秒、半球字母前置/后置、中文「北纬/东经」），
 *   还有三套坐标基准（WGS-84 / GCJ-02 / UTM）会被混着贴进来。
 *   把「字符串 → 数值」「数值 → 各种展示形式」全部收在这一层，
 *   视图层就只剩渲染，测试也可以脱离浏览器直接跑（见 tests/check_geo.mjs）。
 *
 * 约定：
 *   · 本文件**不得**引用 window / document，否则无法在 Node 下做单元测试；
 *   · 所有函数对非法输入一律返回 null 或占位符「—」，绝不抛异常
 *     （用户手输的内容永远不可信，一个多余的字符不该让整个视图崩掉）。
 */

/* ------------------------------------------------------------------ *
 * 1. 基础校验与归一化
 * ------------------------------------------------------------------ */

/**
 * 纬度是否合法（-90 ~ 90）。
 * 注意必须显式排除 null / '' / 布尔：`Number(null) === 0`、`Number('') === 0`，
 * 只判断 Number.isFinite 会把「空值」当成合法的 0°（实测踩过：未定位时读数显示 0,0）。
 */
export const isValidLat = (v) => isRealNumber(v) && Math.abs(Number(v)) <= 90;

/** 经度是否合法（-180 ~ 180） */
export const isValidLon = (v) => isRealNumber(v) && Math.abs(Number(v)) <= 180;

/** 是否为「真正的数字」：排除 null / undefined / '' / 布尔 / NaN */
function isRealNumber(v) {
  if (v === null || v === undefined || typeof v === 'boolean') return false;
  if (typeof v === 'string' && v.trim() === '') return false;
  return Number.isFinite(Number(v));
}

/** 把经度折算到 [-180, 180]（跨 180° 经线平移地图时需要） */
export function normalizeLon(lon) {
  let v = Number(lon);
  if (!Number.isFinite(v)) return 0;
  while (v > 180) v -= 360;
  while (v < -180) v += 360;
  return v;
}

/** 半球字母/中文 → 轴向（'lat' | 'lon'） */
export function axisOfHemisphere(hemi) {
  const h = String(hemi || '').toUpperCase();
  if (h === 'N' || h === 'S') return 'lat';
  if (h === 'E' || h === 'W') return 'lon';
  return null;
}

/**
 * 输入归一化：全角转半角、中文方位词转字母、各类分隔符统一。
 * 这一步是「粘贴即用」的关键 —— 从网页、聊天记录里复制来的坐标
 * 经常带全角逗号、中文书名号式的括号和「北纬/东经」字样。
 */
export function normalizeInput(input) {
  let s = String(input ?? '');
  // 全角字符 → 半角（含全角逗号、全角空格）
  s = s.replace(/[\uFF01-\uFF5E]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0));
  s = s.replace(/\u3000/g, ' ');
  // 中文方位词（放在字母替换之前，长词优先）
  s = s
    .replace(/北纬|北緯/g, 'N')
    .replace(/南纬|南緯/g, 'S')
    .replace(/东经|東經/g, 'E')
    .replace(/西经|西經/g, 'W')
    .replace(/纬度|緯度|经度|經度/g, ' ')
    .replace(/度/g, '°')
    .replace(/分/g, "'")
    .replace(/秒/g, '"');
  // 中文标点与各种引号统一成半角
  s = s
    .replace(/[，、；;：]/g, ',')
    .replace(/[（(]/g, ' ')
    .replace(/[）)]/g, ' ')
    .replace(/[′’]/g, "'")
    .replace(/[″”]/g, '"')
    .replace(/[—–]/g, '-');
  return s;
}

/* ------------------------------------------------------------------ *
 * 2. 解析：字符串 → 数值
 * ------------------------------------------------------------------ */

/**
 * 匹配一个「坐标片段」：可选前置半球字母 + 度（+ 可选分、秒）+ 可选后置半球字母。
 * 例：`39.9087` / `39°54'31"N` / `N39.9087` / `E116°23'51"`
 *
 * 后置半球字母后面跟一个负向断言 `(?![\w])` 很关键：
 * 输入 `N39.9087 E116.3975` 时，若不加这个断言，第一段的「后置半球」会把
 * 第二段的 `E` 吃掉（`\s*` 先吞掉空格，再匹配 E），于是第一段被当成经度、
 * 第二段丢了半球标记，最终解析失败。实测就是这么翻车的。
 */
const TOKEN_RE = /([NSEWnsew])?\s*([+-]?\d+(?:\.\d+)?)\s*(?:°\s*(?:([+-]?\d+(?:\.\d+)?)\s*'?\s*)?(?:([+-]?\d+(?:\.\d+)?)\s*"?\s*)?)?(?:([NSEWnsew])(?![\w]))?/g;

/**
 * 把「空格分隔的度分秒」改写成带符号的标准写法。
 * `39 54 31 N` → `39°54'31"N`；`N 39 54 31` → `N39°54'31"`。
 * 这种写法在纸质地图和测绘记录里很常见，但分词器会把它当成三个独立数字。
 */
function expandSpaceDMS(text) {
  let s = text;
  // 半球在后：39 54 31 N
  s = s.replace(/(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s*([NSEWnsew])(?![\w])/g,
    (_m, d, m2, sec, hemi) => `${d}°${m2}'${sec}"${hemi}`);
  // 半球在前：N 39 54 31
  s = s.replace(/([NSEWnsew])\s*(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)(?![\d.])/g,
    (_m, hemi, d, m2, sec) => `${hemi}${d}°${m2}'${sec}"`);
  return s;
}

/**
 * 把归一化后的文本切成坐标片段数组。
 * @param {string} text 已归一化的文本
 * @returns {Array<{value:number, hemi:string|null, dms:boolean, int:boolean}>}
 */
function tokensFrom(text) {
  const out = [];
  TOKEN_RE.lastIndex = 0;
  let m;
  while ((m = TOKEN_RE.exec(text)) !== null) {
    if (m.index === TOKEN_RE.lastIndex) TOKEN_RE.lastIndex += 1; // 空匹配保护，避免死循环
    const [, pre, deg, min, sec, post] = m;
    const d = Number(deg);
    if (!Number.isFinite(d)) continue;
    const hasMin = min !== undefined && min !== '';
    const hasSec = sec !== undefined && sec !== '';
    const mm = hasMin ? Number(min) : 0;
    const ss = hasSec ? Number(sec) : 0;
    // 分/秒本身不带符号，符号只由度或半球决定
    let value = Math.abs(d) + Math.abs(mm) / 60 + Math.abs(ss) / 3600;
    if (d < 0 || /^-/.test(deg)) value = -value;
    const hemi = String(post || pre || '').toUpperCase() || null;
    if (hemi === 'S' || hemi === 'W') value = -Math.abs(value);
    if (hemi === 'N' || hemi === 'E') value = Math.abs(value);
    out.push({ value, hemi, dms: hasMin || hasSec, int: !deg.includes('.') });
  }
  return out;
}

/**
 * 解析单个轴向的坐标值（用于「纬度」「经度」两个独立输入框）。
 * @param {string} text 用户输入
 * @param {'lat'|'lon'} axis 期望轴向；半球字母与轴向冲突时返回 null
 * @returns {number|null}
 */
export function parseAngle(text, axis) {
  const tokens = tokensFrom(expandSpaceDMS(normalizeInput(text)));
  if (!tokens.length) return null;
  const t = tokens[0];
  if (t.hemi && axis && axisOfHemisphere(t.hemi) !== axis) return null;
  const v = t.value;
  if (axis === 'lat' && !isValidLat(v)) return null;
  if (axis === 'lon' && !isValidLon(v)) return null;
  return v;
}

/**
 * 解析一对经纬度。支持：
 *   `39.9087, 116.3975`            —— 十进制度（默认「纬度在前」）
 *   `39°54'31"N 116°23'51"E`       —— 度分秒
 *   `N39.9087 E116.3975`           —— 半球字母前置
 *   `北纬39.9087 东经116.3975`      —— 中文写法
 *   `116.3975, 39.9087`            —— 经度在前（按数值范围自动纠正）
 *
 * @param {string} input 任意含两个坐标的文本
 * @returns {{lat:number, lon:number}|null}
 */
export function parseLatLon(input) {
  const text = expandSpaceDMS(normalizeInput(input));
  if (!text.trim()) return null;
  const tokens = tokensFrom(text);
  if (tokens.length < 2) return null;

  const [a, b] = tokens;
  const axisA = a.hemi ? axisOfHemisphere(a.hemi) : null;
  const axisB = b.hemi ? axisOfHemisphere(b.hemi) : null;
  if (axisA && axisB && axisA === axisB) return null; // 两个都是纬度或都是经度，无法解释

  let lat;
  let lon;
  if (axisA || axisB) {
    if (axisA === 'lat') { lat = a.value; lon = b.value; }
    else if (axisA === 'lon') { lon = a.value; lat = b.value; }
    else if (axisB === 'lat') { lat = b.value; lon = a.value; }
    else { lon = b.value; lat = a.value; }
  } else if (Math.abs(a.value) > 90 && Math.abs(b.value) <= 90) {
    // 第一个数不可能是纬度 —— 用户写的是「经度, 纬度」
    lon = a.value;
    lat = b.value;
  } else {
    lat = a.value;
    lon = b.value;
  }

  if (!isValidLat(lat) || !isValidLon(lon)) return null;
  return { lat, lon };
}

/* ------------------------------------------------------------------ *
 * 3. 格式化：数值 → 展示字符串
 * ------------------------------------------------------------------ */

/** 十进制度（固定小数位，带正负号语义的补零处理） */
export function formatDecimal(value, digits = 6) {
  const v = Number(value);
  if (!Number.isFinite(v)) return '—';
  return v.toFixed(digits);
}

/**
 * 度分秒：`39°54′31.3″N`
 * @param {number} value 十进制度（负数自动带 S / W）
 * @param {'lat'|'lon'} axis 决定半球字母
 * @param {number} digits 秒的小数位
 */
export function formatDMS(value, axis = 'lat', digits = 1) {
  const v = Number(value);
  if (!Number.isFinite(v)) return '—';
  const hemi = axis === 'lat' ? (v >= 0 ? 'N' : 'S') : (v >= 0 ? 'E' : 'W');
  const abs = Math.abs(v);
  let d = Math.floor(abs);
  let m = Math.floor((abs - d) * 60);
  let s = (((abs - d) * 60) - m) * 60;
  s = Number(s.toFixed(digits));
  // 进位处理：59.96″ 四舍五入后必须进到分，否则会出现 60″ 这种非法写法
  if (s >= 60) { s -= 60; m += 1; }
  if (m >= 60) { m -= 60; d += 1; }
  const secStr = s.toFixed(digits).padStart(digits > 0 ? 3 + digits : 2, '0');
  return `${d}°${String(m).padStart(2, '0')}′${secStr}″${hemi}`;
}

/** `39.908700, 116.397500` */
export function formatPair(lat, lon, digits = 6) {
  if (!isValidLat(lat) || !isValidLon(lon)) return '—';
  return `${Number(lat).toFixed(digits)}, ${Number(lon).toFixed(digits)}`;
}

/** 经纬度 → UTM 带号（含纬度带字母），用于测绘/野外作业对照 */
export function toUTM(lat, lon) {
  const latN = Number(lat);
  const lonN = Number(lon);
  if (!isValidLat(latN) || !isValidLon(lonN)) return null;

  const a = 6378137.0;
  const f = 1 / 298.257223563;
  const e2 = 2 * f - f * f;
  const ep2 = e2 / (1 - e2);
  const k0 = 0.9996;

  const zone = Math.floor((lonN + 180) / 6) + 1;
  const lon0 = ((zone - 1) * 6 - 180 + 3) * Math.PI / 180;
  const phi = latN * Math.PI / 180;
  const lam = lonN * Math.PI / 180;

  const N = a / Math.sqrt(1 - e2 * Math.sin(phi) ** 2);
  const T = Math.tan(phi) ** 2;
  const C = ep2 * Math.cos(phi) ** 2;
  const A = Math.cos(phi) * (lam - lon0);
  const M = a * (
    (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * phi
    - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * Math.sin(2 * phi)
    + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * Math.sin(4 * phi)
    - (35 * e2 ** 3 / 3072) * Math.sin(6 * phi)
  );

  const easting = k0 * N * (
    A + (1 - T + C) * A ** 3 / 6
    + (5 - 18 * T + T * T + 72 * C - 58 * ep2) * A ** 5 / 120
  ) + 500000;

  let northing = k0 * (M + N * Math.tan(phi) * (
    A ** 2 / 2
    + (5 - T + 9 * C + 4 * C * C) * A ** 4 / 24
    + (61 - 58 * T + T * T + 600 * C - 330 * ep2) * A ** 6 / 720
  ));
  if (latN < 0) northing += 10000000;

  const BANDS = 'CDEFGHJKLMNPQRSTUVWX';
  const band = latN >= 84 ? 'X' : (latN < -80 ? 'C' : BANDS[Math.max(0, Math.min(19, Math.floor((latN + 80) / 8)))]);

  return {
    zone,
    band,
    hemisphere: latN >= 0 ? 'N' : 'S',
    easting: Math.round(easting),
    northing: Math.round(northing),
    label: `${zone}${band}`,
  };
}

/** 经纬度 → Maidenhead 网格定位（业余无线电常用，如 `OM89`） */
export function maidenhead(lat, lon) {
  const latN = Number(lat);
  const lonN = Number(lon);
  if (!isValidLat(latN) || !isValidLon(lonN)) return null;
  const A = 'A'.charCodeAt(0);
  const lonAdj = lonN + 180;
  const latAdj = latN + 90;
  const f1 = Math.floor(lonAdj / 20);
  const f2 = Math.floor(latAdj / 10);
  const s1 = Math.floor((lonAdj % 20) / 2);
  const s2 = Math.floor(latAdj % 10);
  const t1 = Math.floor(((lonAdj % 2) * 60) / 5);
  const t2 = Math.floor(((latAdj % 1) * 60) / 2.5);
  // 注意：数字位必须用模板串拼，不能塞进 fromCharCode —— 传 '8' 会按字符码 8 生成控制字符
  return `${String.fromCharCode(A + f1)}${String.fromCharCode(A + f2)}${s1}${s2}`
    + `${String.fromCharCode(A + t1)}${String.fromCharCode(A + t2)}`;
}

/* ------------------------------------------------------------------ *
 * 4. 球面距离
 * ------------------------------------------------------------------ */

const EARTH_R = 6371.0088; // 平均地球半径（km）

/** 两点大圆距离（km） */
export function distanceKm(lat1, lon1, lat2, lon2) {
  const toRad = (x) => Number(x) * Math.PI / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_R * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** 距离的人读写法：<1km 用米，<100km 保留 1 位小数 */
export function formatDistance(km) {
  const v = Number(km);
  if (!Number.isFinite(v)) return '—';
  if (v < 1) return `${Math.round(v * 1000)} m`;
  if (v < 100) return `${v.toFixed(1)} km`;
  return `${Math.round(v).toLocaleString('zh-CN')} km`;
}

/* ------------------------------------------------------------------ *
 * 5. 坐标基准换算：WGS-84 ⇄ GCJ-02（火星坐标）
 * ------------------------------------------------------------------ */

/**
 * 中国大陆境内使用 GCJ-02（国测局坐标），与 GPS/WGS-84 有数百米偏移。
 * 用户从国内地图 App 复制的坐标多数是 GCJ-02，直接当作 WGS-84 打点会偏。
 * 这里提供两个方向的换算，仅用于**显示对照**，不改变用户输入本身。
 *
 * 算法为公开的经典实现（非线性偏移 + 克拉索夫斯基椭球参数），
 * 在境外直接返回原值（境外不存在 GCJ-02 偏移）。
 */
const GCJ_A = 6378245.0;
const GCJ_EE = 0.00669342162296594323;

/** 是否在中国大陆范围之外（含港澳台之外判定为境外，与常见实现保持一致） */
export function outOfChina(lat, lon) {
  const latN = Number(lat);
  const lonN = Number(lon);
  return !(lonN > 73.66 && lonN < 135.05 && latN > 3.86 && latN < 53.55);
}

function transformLat(x, y) {
  let ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
  ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
  ret += (20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin(y / 3.0 * Math.PI)) * 2.0 / 3.0;
  ret += (160.0 * Math.sin(y / 12.0 * Math.PI) + 320 * Math.sin(y * Math.PI / 30.0)) * 2.0 / 3.0;
  return ret;
}

function transformLon(x, y) {
  let ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
  ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
  ret += (20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin(x / 3.0 * Math.PI)) * 2.0 / 3.0;
  ret += (150.0 * Math.sin(x / 12.0 * Math.PI) + 300.0 * Math.sin(x / 30.0 * Math.PI)) * 2.0 / 3.0;
  return ret;
}

/** WGS-84 → GCJ-02 */
export function wgs84ToGcj02(lat, lon) {
  const latN = Number(lat);
  const lonN = Number(lon);
  if (!isValidLat(latN) || !isValidLon(lonN)) return null;
  if (outOfChina(latN, lonN)) return { lat: latN, lon: lonN };
  let dLat = transformLat(lonN - 105.0, latN - 35.0);
  let dLon = transformLon(lonN - 105.0, latN - 35.0);
  const radLat = latN / 180.0 * Math.PI;
  let magic = Math.sin(radLat);
  magic = 1 - GCJ_EE * magic * magic;
  const sqrtMagic = Math.sqrt(magic);
  dLat = (dLat * 180.0) / ((GCJ_A * (1 - GCJ_EE)) / (magic * sqrtMagic) * Math.PI);
  dLon = (dLon * 180.0) / (GCJ_A / sqrtMagic * Math.cos(radLat) * Math.PI);
  return { lat: latN + dLat, lon: lonN + dLon };
}

/**
 * GCJ-02 → WGS-84（用迭代逼近求逆，误差 < 1e-7°，约 1 cm 级）。
 * 解析解在数学上不存在（偏移量本身依赖结果坐标），迭代 3 次足够收敛。
 */
export function gcj02ToWgs84(lat, lon) {
  const latN = Number(lat);
  const lonN = Number(lon);
  if (!isValidLat(latN) || !isValidLon(lonN)) return null;
  if (outOfChina(latN, lonN)) return { lat: latN, lon: lonN };
  let wgsLat = latN;
  let wgsLon = lonN;
  for (let i = 0; i < 4; i += 1) {
    const g = wgs84ToGcj02(wgsLat, wgsLon);
    if (!g) break;
    wgsLat += latN - g.lat;
    wgsLon += lonN - g.lon;
  }
  return { lat: wgsLat, lon: wgsLon };
}
