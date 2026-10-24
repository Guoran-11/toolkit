/**
 * matrix.js — 格式支持矩阵（规格书 §5.1.2 的代码化）
 *
 * 这里的定义会与后端 `GET /api/convert/matrix` 的返回做合并：
 * 后端可用时以后端为准（后端可根据当前引擎可用性动态禁用某些路径），
 * 后端不可用时以前端这份静态定义渲染，保证演示与评审可用。
 *
 * 质量等级语义（务必在 UI 上如实呈现，避免用户预期落差 —— 风险 R5）：
 *   A 类：结构级还原，输出可编辑且版式准确
 *   B 类：尽力还原，可能存在版式偏差
 *   C 类：视觉保真，输出为图片/整页对象，不可逐字编辑
 *   ×   ：不支持
 *   —   ：源格式与目标格式相同，无需转换
 */

/** 前端可识别的源格式（含 MIME 用于 input accept） */
export const SOURCE_FORMATS = [
  { id: 'html', label: '网页 / HTML', exts: ['html', 'htm', 'mhtml'], mime: 'text/html', icon: 'code' },
  { id: 'docx', label: 'Word 文档',   exts: ['docx'], mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', icon: 'fileText' },
  { id: 'xlsx', label: 'Excel 表格',  exts: ['xlsx'], mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', icon: 'database' },
  { id: 'pptx', label: 'PPT 演示',    exts: ['pptx'], mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation', icon: 'layers' },
  { id: 'pdf',  label: 'PDF 文档',    exts: ['pdf'],  mime: 'application/pdf', icon: 'file' },
];

/** 可作为目标的格式 */
export const TARGET_FORMATS = [
  { id: 'pdf',  label: 'PDF',  exts: ['pdf'],  icon: 'file',  desc: '通用、版式固定' },
  { id: 'docx', label: 'Word', exts: ['docx'], icon: 'fileText', desc: '可编辑文档' },
  { id: 'xlsx', label: 'Excel',exts: ['xlsx'], icon: 'database', desc: '可编辑表格' },
  { id: 'pptx', label: 'PPT',  exts: ['pptx'], icon: 'layers', desc: '可编辑演示' },
  { id: 'html', label: 'HTML', exts: ['html'], icon: 'code', desc: '网页格式' },
  { id: 'png',  label: '图片', exts: ['png', 'jpg'], icon: 'image', desc: '按页导出' },
];

/**
 * 支持矩阵：MATRIX[source][target] = { grade, engine, note }
 * grade 为 null 表示不支持。
 */
export const MATRIX = {
  html: {
    docx: { grade: 'B', engine: 'mammoth', note: '提取正文与基础样式，复杂 CSS 布局会简化' },
    xlsx: { grade: 'B', engine: '表格区域识别', note: '识别页面中的表格并转为工作表，非表格内容将丢弃' },
    pptx: { grade: null, engine: null, note: 'HTML 的流式布局无法映射为幻灯片版面，不支持' },
    pdf:  { grade: 'A', engine: 'Playwright + Chromium', note: '与浏览器打印预览高度一致，保留打印 CSS、图片与字体' },
    html: { grade: null, engine: null, note: '' },
    png:  { grade: 'A', engine: 'Chromium 截图', note: '整页光栅化，长页面会按视口分段截图' },
  },
  docx: {
    docx: { grade: null, engine: null, note: '' },
    xlsx: { grade: 'B', engine: '表格提取', note: '表格转为工作表，正文段落另存一表；公式与图表不保留' },
    pptx: { grade: 'C', engine: 'LibreOffice → 逐页导出', note: '每页一张图片，文字不可编辑' },
    pdf:  { grade: 'A', engine: 'LibreOffice headless', note: '版面还原度高，页眉页脚与分页保留' },
    html: { grade: 'A', engine: 'mammoth', note: '结构清晰、语义化 HTML，样式经过简化' },
    png:  { grade: 'C', engine: 'LibreOffice → PDF → 光栅化', note: '经 PDF 中转，输出为图片，不可编辑' },
  },
  xlsx: {
    docx: { grade: 'B', engine: '经 HTML 中转', note: '工作表转为表格，公式与图表不保留' },
    xlsx: { grade: null, engine: null, note: '' },
    pptx: { grade: null, engine: null, note: '表格数据无法映射为幻灯片版面，不支持' },
    pdf:  { grade: 'A', engine: 'LibreOffice headless', note: '保留列宽、分页与打印区域设置' },
    html: { grade: 'A', engine: 'openpyxl', note: '每个工作表输出为一个 HTML 表格' },
    png:  { grade: 'C', engine: 'LibreOffice → PDF → 光栅化', note: '经 PDF 中转，长表格会分页输出为多张图片' },
  },
  pptx: {
    docx: { grade: 'B', engine: '备注 + 图片', note: '仅导出备注文字与每页缩略图，版式不保留' },
    xlsx: { grade: 'B', engine: '表格提取', note: '幻灯片表格与文本分表导出，版式不保留' },
    pptx: { grade: null, engine: null, note: '' },
    pdf:  { grade: 'A', engine: 'LibreOffice headless', note: '与放映效果基本一致，动画与切换效果不保留' },
    html: { grade: 'C', engine: '逐页图片', note: '每页一张图片，文字不可选中' },
    png:  { grade: 'C', engine: 'LibreOffice → PDF → 光栅化', note: '每页输出一张图片' },
  },
  pdf: {
    docx: { grade: 'B', engine: 'pdf2docx', note: '重建段落与表格结构，可在 Word 中编辑；扫描件需先 OCR' },
    xlsx: { grade: 'B', engine: 'pdfplumber 表格识别', note: '自动识别表格区域，非表格内容会丢失；扫描件需先 OCR' },
    pptx: { grade: 'C', engine: 'PyMuPDF 逐页光栅化', note: '⚠ 输出为每页一张图片，文字不可编辑' },
    pdf:  { grade: null, engine: null, note: '' },
    html: { grade: 'B', engine: 'pdfminer.six', note: '保留可选中文本与基本布局，复杂版面会重排' },
    png:  { grade: 'A', engine: 'PyMuPDF', note: '按页光栅化，DPI 可选，输出可打包 ZIP' },
  },
};

/** 等级的中文释义与变体（供 UI 显示） */
export const GRADE_INFO = {
  A: { label: 'A 类', variant: 'success', desc: '结构级还原，输出可编辑且版式准确' },
  B: { label: 'B 类', variant: 'warning', desc: '尽力还原，可能存在版式偏差' },
  C: { label: 'C 类', variant: 'info',    desc: '视觉保真，输出为图片，不可逐字编辑' },
  X: { label: '不支持', variant: 'danger', desc: '该转换方向不在支持范围内' },
  '—': { label: '无需转换', variant: '',      desc: '源格式与目标格式相同' },
};

/** 取某个转换组合的规格；无返回 null 表示不支持 */
export function lookup(source, target) {
  if (!source || !target) return null;
  const row = MATRIX[source];
  if (!row) return null;
  return row[target] || null;
}

/** 某源格式支持的全部目标格式 id */
export function supportedTargets(source) {
  const row = MATRIX[source];
  if (!row) return [];
  return Object.entries(row)
    .filter(([, v]) => v && v.grade)
    .map(([k]) => k);
}

/**
 * 生成"预期质量"提示文案（§5.1.2 强制要求页面给出提示）。
 * @returns {{ level:'info'|'warning'|'danger', title:string, body:string }|null}
 */
export function qualityHint(source, target) {
  const cell = lookup(source, target);
  if (!cell) {
    return {
      level: 'danger',
      title: '不支持该转换方向',
      body: '请更换源文件类型或目标格式。矩阵中标记为 × 的组合不在本期支持范围内。',
    };
  }
  if (!cell.grade) return null;

  const level = cell.grade === 'C' ? 'warning' : cell.grade === 'B' ? 'info' : 'success';
  const title = `预期质量：${GRADE_INFO[cell.grade].label}`;
  const body = `${cell.note}（引擎：${cell.engine}）`;
  return { level, title, body };
}

/** 估算产物（用于结果卡片上的占位文案） */
export function outputExt(target, source) {
  if (target === 'png') return source === 'pdf' ? 'zip' : 'png';
  return target;
}

/**
 * 各目标格式的参数定义（驱动左侧表单动态渲染）。
 * type: select | number | checkbox
 */
export const TARGET_PARAMS = {
  pdf: [
    {
      id: 'paper_size', label: '纸张大小', type: 'select', default: 'A4',
      options: [
        { value: 'A4', label: 'A4（210 × 297 mm）' },
        { value: 'Letter', label: 'Letter（216 × 279 mm）' },
        { value: 'A3', label: 'A3（297 × 420 mm）' },
        { value: 'A5', label: 'A5（148 × 210 mm）' },
      ],
      appliesTo: ['html'],
    },
    {
      id: 'orientation', label: '方向', type: 'select', default: 'portrait',
      options: [
        { value: 'portrait', label: '纵向' },
        { value: 'landscape', label: '横向' },
      ],
      appliesTo: ['html'],
    },
    {
      id: 'margin_mm', label: '页边距', type: 'number', default: 12, min: 0, max: 50, step: 1, unit: 'mm',
      help: '四边统一页边距',
      appliesTo: ['html'],
    },
    {
      id: 'print_background', label: '打印背景色与图片', type: 'checkbox', default: true,
      help: '关闭后背景色与背景图将不输出，可减少墨水消耗',
      appliesTo: ['html'],
    },
    {
      id: 'scale', label: '页面缩放', type: 'number', default: 100, min: 50, max: 200, step: 5, unit: '%',
      appliesTo: ['html'],
    },
  ],
  png: [
    {
      id: 'dpi', label: '输出分辨率', type: 'select', default: '150',
      options: [
        { value: '96', label: '96 DPI（屏幕预览）' },
        { value: '150', label: '150 DPI（推荐）' },
        { value: '300', label: '300 DPI（印刷级，耗时长）' },
      ],
    },
    {
      id: 'image_format', label: '图片格式', type: 'select', default: 'png',
      options: [
        { value: 'png', label: 'PNG（无损，体积大）' },
        { value: 'jpg', label: 'JPG（有损，体积小）' },
      ],
    },
    {
      id: 'quality', label: 'JPG 质量', type: 'number', default: 90, min: 40, max: 100, step: 5, unit: '%',
      help: '仅在选择 JPG 时生效',
    },
    {
      id: 'page_range', label: '页面范围', type: 'text', default: 'all', placeholder: 'all 或 1-3,5,8',
      help: '留空或填 all 表示全部页面',
    },
  ],
  docx: [
    {
      id: 'keep_layout', label: '尽量保留原版面', type: 'checkbox', default: true,
      help: '关闭后输出更接近纯文本流，兼容性更好',
    },
  ],
  xlsx: [
    {
      id: 'table_mode', label: '表格识别模式', type: 'select', default: 'auto',
      options: [
        { value: 'auto', label: '自动识别' },
        { value: 'lattice', label: '有线框表格' },
        { value: 'stream', label: '无线框表格' },
      ],
    },
    {
      id: 'one_sheet_per_table', label: '每个表格一个工作表', type: 'checkbox', default: true,
    },
  ],
  pptx: [
    {
      id: 'slide_size', label: '幻灯片尺寸', type: 'select', default: '16:9',
      options: [
        { value: '16:9', label: '16:9 宽屏' },
        { value: '4:3', label: '4:3 传统' },
      ],
    },
    {
      id: 'note_mode', label: '附带标注', type: 'checkbox', default: true,
      help: '在图片模式下把原始文字放入备注，便于检索',
    },
  ],
  html: [
    {
      id: 'inline_css', label: '内联样式', type: 'checkbox', default: true,
      help: '将样式内联到标签上，便于单独分发',
    },
    {
      id: 'embed_images', label: '内嵌图片（Base64）', type: 'checkbox', default: false,
      help: '开启后输出为单文件，但体积会显著增大',
    },
  ],
};

/** 取某转换组合下需要展示的参数项（过滤 appliesTo） */
export function paramsFor(source, target) {
  const list = TARGET_PARAMS[target] || [];
  return list.filter((p) => !p.appliesTo || p.appliesTo.includes(source));
}

/** 组装参数默认值对象 */
export function defaultParams(source, target) {
  const out = {};
  for (const p of paramsFor(source, target)) out[p.id] = p.default;
  return out;
}
