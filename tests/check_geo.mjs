/**
 * tests/check_geo.mjs — 地图定位模块的纯逻辑自检（无需浏览器）
 *
 * 运行方式：
 *     node tests/check_geo.mjs
 *
 * 为什么单独写一个 Node 脚本：
 *   坐标解析与换算（度分秒、UTM、GCJ-02、TopoJSON 解码、点落国判定）
 *   全是纯函数，出错时表现是「数字悄悄偏了几百米」，肉眼看不出来。
 *   把它们从 DOM 里摘出来单测，比事后拿地图对图可靠得多。
 *
 * 说明：app/static/js/ 下的模块是浏览器 ESM，而仓库根目录没有 package.json
 *   （Node 会把 .js 当 CommonJS），因此这里把源码以 data: URL 方式导入，
 *   避免为了测试给整个项目加一个前端构建配置。
 */

import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

/** 以 ESM 方式导入一个浏览器模块（源码里不含相对导入时才可用） */
async function importModule(relPath) {
  const abs = join(ROOT, relPath);
  const src = await readFile(abs, 'utf8');
  const url = `data:text/javascript;base64,${Buffer.from(src, 'utf8').toString('base64')}`;
  return import(url);
}

const PASS = [];
const FAIL = [];

function check(name, ok, detail = '') {
  (ok ? PASS : FAIL).push(name);
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? `  -> ${detail}` : ''}`);
}

const near = (a, b, eps = 1e-6) => Math.abs(Number(a) - Number(b)) <= eps;

const {
  parseLatLon, parseAngle, formatDMS, toUTM, maidenhead,
  wgs84ToGcj02, gcj02ToWgs84, outOfChina, distanceKm, formatDistance,
  isValidLat, isValidLon, normalizeLon,
} = await importModule('app/static/js/geo/coords.js');

const { decodeCountries, buildCountryIndex, countryAt } = await importModule('app/static/js/geo/topojson.js');
const { nearestPlaces, translateCountry } = await importModule('app/static/js/geo/places.js');

console.log('\n== 1. 坐标解析 ==');

const p1 = parseLatLon('39.9087, 116.3975');
check('十进制度（纬度在前）', p1 && near(p1.lat, 39.9087) && near(p1.lon, 116.3975), JSON.stringify(p1));

const p2 = parseLatLon(`39°54'31"N 116°23'51"E`);
check('度分秒 + 半球后置', p2 && near(p2.lat, 39.908611, 1e-5) && near(p2.lon, 116.3975, 1e-5), JSON.stringify(p2));

const p3 = parseLatLon('北纬39.9087 东经116.3975');
check('中文「北纬/东经」', p3 && near(p3.lat, 39.9087) && near(p3.lon, 116.3975), JSON.stringify(p3));

const p4 = parseLatLon('N39.9087 E116.3975');
check('半球字母前置', p4 && near(p4.lat, 39.9087) && near(p4.lon, 116.3975), JSON.stringify(p4));

const p5 = parseLatLon('116.3975, 39.9087');
check('经度在前时自动纠正', p5 && near(p5.lat, 39.9087) && near(p5.lon, 116.3975), JSON.stringify(p5));

const p6 = parseLatLon('39°54′31″N，116°23′51″E');
check('全角逗号 + 中文引号', p6 && near(p6.lat, 39.908611, 1e-5), JSON.stringify(p6));

const p7 = parseLatLon('南纬33.8688 东经151.2093');
check('南纬/东经（负纬度）', p7 && near(p7.lat, -33.8688) && near(p7.lon, 151.2093), JSON.stringify(p7));

const p8 = parseLatLon('39 54 31 N 116 23 51 E');
check('空格分隔的度分秒', p8 && near(p8.lat, 39.908611, 1e-5) && near(p8.lon, 116.3975, 1e-5), JSON.stringify(p8));

const p9 = parseLatLon('N 39 54 31, E 116 23 51');
check('半球字母前置 + 空格度分秒', p9 && near(p9.lat, 39.908611, 1e-5), JSON.stringify(p9));

check('非法输入返回 null（纯文字）', parseLatLon('天安门') === null);
check('非法输入返回 null（超出范围）', parseLatLon('91.5, 200.3') === null);
check('只有一个数时返回 null', parseLatLon('39.9087') === null);
check('两个纬度无法解释时返回 null', parseLatLon('39.9N 40.1N') === null);

check('parseAngle 轴向校验（纬度字段填 E）', parseAngle('116.3975E', 'lat') === null);
check('parseAngle 正常解析', near(parseAngle('39°54′31″N', 'lat'), 39.908611, 1e-5));

console.log('\n== 2. 格式化与换算 ==');

check('formatDMS 北纬', formatDMS(39.9087, 'lat') === '39°54′31.3″N', formatDMS(39.9087, 'lat'));
check('formatDMS 西经补零', formatDMS(-33.8688, 'lon') === '33°52′07.7″W', formatDMS(-33.8688, 'lon'));
check('formatDMS 进位（59.96″ 不能出现 60″）', formatDMS(39 + 59 / 60 + 59.96 / 3600, 'lat').includes('60.0″') === false,
  formatDMS(39 + 59 / 60 + 59.96 / 3600, 'lat'));

const utm0 = toUTM(0, 3);
check('UTM 赤道/本初子午线东 3°（31N，东偏 500000）',
  utm0 && utm0.zone === 31 && utm0.band === 'N' && utm0.easting === 500000 && utm0.northing === 0,
  JSON.stringify(utm0));

const utmB = toUTM(39.9087, 116.3975);
check('UTM 北京（50S 带）', utmB && utmB.zone === 50 && utmB.band === 'S'
  && utmB.easting > 400000 && utmB.easting < 500000
  && utmB.northing > 4400000 && utmB.northing < 4500000, JSON.stringify(utmB));

check('Maidenhead 北京前缀 OM89', String(maidenhead(39.9087, 116.3975)).startsWith('OM89'), maidenhead(39.9087, 116.3975));

const dist = distanceKm(39.9087, 116.3975, 31.2304, 121.4737);
check('北京→上海 大圆距离 ≈ 1067 km', dist > 1000 && dist < 1120, `${dist.toFixed(1)} km`);
check('formatDistance 单位切换', formatDistance(0.42) === '420 m' && formatDistance(12.34) === '12.3 km',
  `${formatDistance(0.42)} / ${formatDistance(12.34)}`);

check('经纬度范围校验', isValidLat(90) && !isValidLat(90.1) && isValidLon(180) && !isValidLon(180.5));
check('经度归一化', normalizeLon(190) === -170 && normalizeLon(-190) === 170);

console.log('\n== 3. GCJ-02 火星坐标换算 ==');

check('境外判定（巴黎为境外）', outOfChina(48.8566, 2.3522) === true);
check('境内判定（北京为境内）', outOfChina(39.9087, 116.3975) === false);

const gcj = wgs84ToGcj02(39.9087, 116.3975);
const offsetKm = distanceKm(39.9087, 116.3975, gcj.lat, gcj.lon);
check('WGS-84 → GCJ-02 偏移量在 300~900 m', offsetKm > 0.3 && offsetKm < 0.9, `${(offsetKm * 1000).toFixed(0)} m`);

const back = gcj02ToWgs84(gcj.lat, gcj.lon);
check('GCJ-02 → WGS-84 迭代逆算可还原（< 1e-6°）',
  near(back.lat, 39.9087, 1e-6) && near(back.lon, 116.3975, 1e-6),
  `${back.lat.toFixed(8)}, ${back.lon.toFixed(8)}`);

const gcjOutside = wgs84ToGcj02(48.8566, 2.3522);
check('境外不做偏移', near(gcjOutside.lat, 48.8566) && near(gcjOutside.lon, 2.3522));

console.log('\n== 4. 离线底图解码与点落国判定 ==');

const topo = JSON.parse(await readFile(join(ROOT, 'app/static/vendor/geo/countries-110m.json'), 'utf8'));
const countries = decodeCountries(topo);
check('TopoJSON 解码出 177 个国家/地区', countries.length === 177, `${countries.length} 个`);

const totalRings = countries.reduce((sum, c) => sum + c.polygons.reduce((s, p) => s + p.length, 0), 0);
check('多边形环数量合理（> 200）', totalRings > 200, `${totalRings} 个环`);

const firstPoint = countries[0]?.polygons?.[0]?.[0]?.[0];
check('坐标已还原为经纬度（经度在 ±180 内）',
  Array.isArray(firstPoint) && Math.abs(firstPoint[0]) <= 180 && Math.abs(firstPoint[1]) <= 90,
  JSON.stringify(firstPoint));

const index = buildCountryIndex(countries);
check('点落国：北京 → China', countryAt(index, 39.9087, 116.3975) === 'China', String(countryAt(index, 39.9087, 116.3975)));
check('点落国：巴黎 → France', countryAt(index, 48.8566, 2.3522) === 'France', String(countryAt(index, 48.8566, 2.3522)));
check('点落国：纽约 → United States of America',
  countryAt(index, 40.7128, -74.0060) === 'United States of America', String(countryAt(index, 40.7128, -74.0060)));
check('点落国：太平洋 → null', countryAt(index, 0, -140) === null, String(countryAt(index, 0, -140)));
check('点落国：洞中洞（莱索托独立于南非）',
  countryAt(index, -29.3, 27.5) === 'Lesotho', String(countryAt(index, -29.3, 27.5)));

console.log('\n== 5. 参考地点与国名翻译 ==');

const nearest = nearestPlaces(39.9087, 116.3975, 1);
check('最近地点为天安门且距离接近 0',
  nearest.length === 1 && nearest[0].name.includes('天安门') && nearest[0].distanceKm < 0.5,
  JSON.stringify(nearest));

const nearShanghai = nearestPlaces(31.2304, 121.4737, 2);
check('上海坐标的最近地点含上海', nearShanghai.some((p) => p.name.includes('上海')), JSON.stringify(nearShanghai.map((p) => p.name)));

check('国名翻译', translateCountry('China') === '中国' && translateCountry('Nowhere') === 'Nowhere');

console.log('\n' + '='.repeat(68));
console.log(`结果：${PASS.length} 项通过，${FAIL.length} 项失败`);
if (FAIL.length) {
  console.log('失败项：');
  for (const name of FAIL) console.log(`  · ${name}`);
}
console.log('='.repeat(68));
process.exit(FAIL.length ? 1 : 0);
