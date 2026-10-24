/**
 * geo/topojson.js — 极简 TopoJSON 解码器 + 点落国判定
 *
 * 为什么需要它：
 *   离线底图用的是 Natural Earth 110m 的 TopoJSON（107 KB，含 177 个国家/地区）。
 *   TopoJSON 通过「共享弧段 + 量化整数 + 增量编码」把体积压到 GeoJSON 的 1/5，
 *   代价是需要先解码才能画。这里的解码器只实现本项目用到的部分
 *   （Polygon / MultiPolygon / 弧段拼环），不追求覆盖完整规范。
 *
 * 为什么不用 CDN 上的 d3-geo / topojson-client：
 *   NFR-08 要求离线零外网请求，且 CSP 只允许 `script-src 'self'`；
 *   把 30 行解码逻辑写进来，比多带一个 200 KB 的依赖更划算。
 *
 * 数据来源：world-atlas（Natural Earth 1:110m，公有领域）
 */

/**
 * 把一条量化弧段还原成经纬度点列。
 * 弧段内是**增量**坐标：每个点相对前一个点，需要累加后再做线性变换。
 */
function arcPoints(arc, transform) {
  const [sx, sy] = transform.scale;
  const [tx, ty] = transform.translate;
  const pts = new Array(arc.length);
  let x = 0;
  let y = 0;
  for (let i = 0; i < arc.length; i += 1) {
    x += arc[i][0];
    y += arc[i][1];
    pts[i] = [x * sx + tx, y * sy + ty];
  }
  return pts;
}

/**
 * 按弧段索引列表拼出一个环（闭合线）。
 * 负索引表示「反向使用第 ~i 条弧段」（TopoJSON 的约定：-1 表示 arcs[0] 反向）。
 * 相邻弧段共享端点，拼接时要丢掉重复点，否则闭合判定会出问题。
 */
function ringFromArcs(list, arcs, transform) {
  const ring = [];
  for (let k = 0; k < list.length; k += 1) {
    const idx = list[k];
    const reversed = idx < 0;
    const pts = arcPoints(arcs[reversed ? ~idx : idx], transform);
    if (reversed) pts.reverse();
    for (let i = k === 0 ? 0 : 1; i < pts.length; i += 1) ring.push(pts[i]);
  }
  return ring;
}

/** 单个几何体 → 多边形数组（每个多边形 = 环数组，第一个是外环，其余是洞） */
function geometryToPolygons(geom, arcs, transform) {
  if (!geom || !geom.arcs) return [];
  if (geom.type === 'Polygon') {
    return [geom.arcs.map((ringArcs) => ringFromArcs(ringArcs, arcs, transform))];
  }
  if (geom.type === 'MultiPolygon') {
    return geom.arcs.map((poly) => poly.map((ringArcs) => ringFromArcs(ringArcs, arcs, transform)));
  }
  return [];
}

/**
 * 解码 TopoJSON 中的某个 object。
 * @param {object} topo 已 JSON.parse 的 TopoJSON
 * @param {string} objectName 例如 'countries'
 * @returns {Array<{name:string, polygons:Array<Array<Array<[number,number]>>>}>}
 */
export function decodeTopology(topo, objectName) {
  const object = topo?.objects?.[objectName];
  if (!object) return [];
  const transform = topo.transform || { scale: [1, 1], translate: [0, 0] };
  const geometries = object.type === 'GeometryCollection'
    ? object.geometries || []
    : [object];

  return geometries.map((geom, index) => ({
    name: geom.properties?.name || String(geom.id ?? index),
    polygons: geometryToPolygons(geom, topo.arcs || [], transform),
  })).filter((c) => c.polygons.length > 0);
}

/** 便捷方法：直接取国家/地区列表 */
export const decodeCountries = (topo) => decodeTopology(topo, 'countries');

/* ------------------------------------------------------------------ *
 * 点落国判定（离线逆地理：经纬度 → 国家/地区名）
 * ------------------------------------------------------------------ */

/** 射线法：点是否在环内（环为 [lon, lat] 序列，首尾不必闭合） */
export function pointInRing(ring, lon, lat) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
    const xi = ring[i][0];
    const yi = ring[i][1];
    const xj = ring[j][0];
    const yj = ring[j][1];
    if (((yi > lat) !== (yj > lat))
      && (lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi)) {
      inside = !inside;
    }
  }
  return inside;
}

/** 计算一个多边形的包围盒，用于快速排除（世界地图上大多数国家都不在候选范围内） */
function polygonBBox(polygon) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const ring of polygon) {
    for (const [x, y] of ring) {
      if (x < minX) minX = x;
      if (y < minY) minY = y;
      if (x > maxX) maxX = x;
      if (y > maxY) maxY = y;
    }
  }
  return [minX, minY, maxX, maxY];
}

/**
 * 为「点落国」查询建立索引：预计算每个多边形的包围盒，避免每次都遍历全部顶点。
 * 110m 数据量不大，但拖动地图时这个查询每帧都可能触发，索引值得建。
 * @param {Array} countries decodeTopology 的结果
 */
export function buildCountryIndex(countries) {
  const entries = [];
  for (const country of countries || []) {
    for (const polygon of country.polygons) {
      const bbox = polygonBBox(polygon);
      entries.push({ name: country.name, polygon, bbox });
    }
  }
  return entries;
}

/**
 * 查询坐标所在的国家/地区。
 * @returns {string|null} 国家/地区名（英文，来自 Natural Earth 的 name 字段）
 */
export function countryAt(index, lat, lon) {
  if (!Array.isArray(index) || !Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  for (const entry of index) {
    const [minX, minY, maxX, maxY] = entry.bbox;
    if (lon < minX || lon > maxX || lat < minY || lat > maxY) continue;
    const [outer, ...holes] = entry.polygon;
    if (!outer || !pointInRing(outer, lon, lat)) continue;
    // 洞（如莱索托之于南非）也要排除
    if (holes.some((hole) => pointInRing(hole, lon, lat))) continue;
    return entry.name;
  }
  return null;
}
