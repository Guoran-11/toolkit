/**
 * icons.js — 内联 SVG 图标集
 *
 * 为什么不用图标字体或 CDN：NFR-08 要求离线零外网请求；内联 SVG 还能跟随
 * currentColor 自动适配明暗主题，且无额外网络往返（对 LCP 友好）。
 * 风格统一为 24x24 viewBox、1.75 描边、圆角端点（见 components.css .icon）。
 */

const PATHS = {
  /* 导航 */
  convert: '<path d="M4 8h13l-3-3"/><path d="M20 16H7l3 3"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m8 10 2 2-2 2"/><path d="M13 14h4"/>',
  music: '<path d="M9 18V6l10-2v12"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/>',
  markdown: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 15V9l3 3 3-3v6"/><path d="M17 9v6"/><path d="m15 13 2 2 2-2"/>',
  video: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3z"/>',
  tasks: '<path d="M9 6h11"/><path d="M9 12h11"/><path d="M9 18h11"/><path d="m3 6 1.5 1.5L7 5"/><path d="m3 12 1.5 1.5L7 11"/><path d="m3 18 1.5 1.5L7 17"/>',
  map: '<path d="M9 3 3 6v15l6-3 6 3 6-3V3l-6 3-6-3z"/><path d="M9 3v15"/><path d="M15 6v15"/>',
  pin: '<path d="M12 21s7-6.1 7-11a7 7 0 1 0-14 0c0 4.9 7 11 7 11z"/><circle cx="12" cy="10" r="2.6"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/>',
  crosshair: '<circle cx="12" cy="12" r="8"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>',
  locate: '<path d="m3 11 18-8-8 18-2-8-8-2z"/>',
  minus: '<path d="M5 12h14"/>',

  /* 操作 */
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 9 5-5 5 5"/><path d="M12 4v12"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 11 5 5 5-5"/><path d="M12 16V4"/>',
  trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  close: '<path d="M18 6 6 18M6 6l12 12"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  clipboard: '<rect x="8" y="3" width="8" height="4" rx="1"/><path d="M16 5h2a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/>',
  play: '<path d="M6 4v16l14-8z"/>',
  pause: '<path d="M7 4h4v16H7zM13 4h4v16h-4z"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  filter: '<path d="M4 5h16l-6 8v6l-4-2v-4z"/>',
  moreVertical: '<circle cx="12" cy="5" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="12" cy="19" r="1.2"/>',
  externalLink: '<path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
  link: '<path d="M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1"/>',
  maximize: '<path d="M4 9V5a1 1 0 0 1 1-1h4"/><path d="M20 15v4a1 1 0 0 1-1 1h-4"/><path d="m4 4 6 6"/><path d="m20 20-6-6"/>',

  /* 状态 */
  checkCircle: '<circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 4.5-5"/>',
  alertCircle: '<circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><circle cx="12" cy="16" r="0.6" fill="currentColor"/>',
  alertTriangle: '<path d="M10.3 3.9 2.5 17a2 2 0 0 0 1.7 3h15.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><circle cx="12" cy="17" r="0.6" fill="currentColor"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4"/><circle cx="12" cy="8" r="0.6" fill="currentColor"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.2 8.4-8 9.6C7.2 20.4 4 16.5 4 12V6z"/><path d="m9 12 2 2 4-4"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/><path d="M12 8v4l3 2"/>',
  hash: '<path d="M5 9h14M5 15h14M10 4 8 20M16 4l-2 16"/>',

  /* 主题与设置 */
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M19 5l-1.5 1.5M6.5 17.5 5 19"/>',
  moon: '<path d="M20 13.5A8.5 8.5 0 1 1 10.5 4a7 7 0 0 0 9.5 9.5z"/>',
  monitor: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2 2 2 0 1 1-4 0 1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 2.9 15a2 2 0 1 1 0-4 1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 9.8 4.1a2 2 0 1 1 4 0A1.7 1.7 0 0 0 16.7 5.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9 2 2 0 1 1 0 4z"/>',

  /* 对象 */
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  fileText: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>',
  folder: '<path d="M4 7a2 2 0 0 1 2-2h3.5l2 2H18a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/>',
  archive: '<rect x="3" y="4" width="18" height="5" rx="1.5"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9"/><path d="M10 13h4"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m4 17 5-4 4 3 3-2.5 4 3.5"/>',
  database: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m10.8 12.2 7.2-7.2"/><path d="m17 5 2 2"/><path d="m14.5 7.5 2 2"/>',
  lock: '<rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  user: '<circle cx="12" cy="8" r="3.5"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>',
  activity: '<path d="M3 12h4l3 8 4-16 3 8h4"/>',
  zap: '<path d="M13 2 4 14h6l-1 8 9-12h-6z"/>',
  wave: '<path d="M3 12h3l2-6 3 12 3-9 2 3h5"/>',
  code: '<path d="m9 8-4 4 4 4"/><path d="m15 8 4 4-4 4"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  chevronRight: '<path d="m9 6 6 6-6 6"/>',
  chevronLeft: '<path d="m15 6-6 6 6 6"/>',
  arrowRight: '<path d="M4 12h15"/><path d="m14 7 5 5-5 5"/>',
  eye: '<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z"/><circle cx="12" cy="12" r="2.6"/>',
  rotateCw: '<path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v5h-5"/>',
  scissors: '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><path d="M20 4 8.1 15.9M8.1 8.1 20 20"/>',
  merge: '<path d="M7 3v6a3 3 0 0 0 3 3h7"/><path d="M7 21v-6a3 3 0 0 1 3-3h7"/><path d="m14 9 3 3-3 3"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  book: '<path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z"/><path d="M4 19a2 2 0 0 1 2-2h13"/>',
  listTree: '<path d="M4 5h4M4 12h4M4 19h4"/><path d="M10 5h10M10 12h10M10 19h10"/>',
  sliders: '<path d="M4 8h10M18 8h2M4 16h2M10 16h10"/><circle cx="16" cy="8" r="2"/><circle cx="8" cy="16" r="2"/>',
  package: '<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z"/><path d="m4 7.5 8 4.5 8-4.5"/><path d="M12 12v9"/>',
};

/**
 * 生成 SVG 图标元素。
 * @param {string} name  PATHS 中的键名
 * @param {string} [cls] 附加 class，默认 'icon'
 * @returns {SVGElement}
 */
export function icon(name, cls = 'icon') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.setAttribute('class', cls);
  svg.innerHTML = PATHS[name] || PATHS.file;
  return svg;
}

/** 生成图标 HTML 字符串（用于模板字符串场景；name 来自代码常量，非用户输入） */
export function iconHTML(name, cls = 'icon') {
  return `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" class="${cls}">${PATHS[name] || PATHS.file}</svg>`;
}

export const hasIcon = (name) => Object.prototype.hasOwnProperty.call(PATHS, name);
