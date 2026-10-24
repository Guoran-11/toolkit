/**
 * mock.js — 演示模式后端（纯前端模拟）
 *
 * 存在的意义：
 * 后端（FastAPI）尚未实现时，前端仍能完整跑通「上传 → 排队 → 进度 → 下载」全链路，
 * 便于提前评审交互、验证无障碍与响应式，也便于演示给非技术方看。
 *
 * 产出的文件是真的可用文件，不是空壳：
 * - PDF   → 手工构造的最小合法 PDF（可被 iframe 与 PDF 阅读器打开）
 * - PNG/JPG → 用 canvas 真实渲染
 * - WAV   → 真实合成的正弦波音频，可被 <audio> 播放
 * - HTML  → 真实 HTML 文本
 * - 其它  → 带说明文本的占位文件
 *
 * 注意：本文件不参与生产构建路径，正式部署时 api.js 不会走这里。
 */

import { uid, sniffMagic, extOf, baseName, randInt } from './utils.js';
import { MATRIX, lookup, outputExt } from './matrix.js';

/* ------------------------------------------------------------------ *
 * 0. 内部状态
 * ------------------------------------------------------------------ */
/** @type {Map<string, object>} fileId → {blob, name, mime} */
const fileStore = new Map();
/** @type {Map<string, object>} taskId → task */
const tasks = new Map();
/** @type {Map<string, object>} sessionId → fake shell session */
const sessions = new Map();

const now = () => new Date().toISOString();
const clone = (v) => JSON.parse(JSON.stringify(v));

/* ------------------------------------------------------------------ *
 * 1. 文件产出：真实可用的小文件
 * ------------------------------------------------------------------ */

/**
 * 构造最小合法 PDF（单页，ASCII 文本）。
 * 手写 xref 表以确保偏移量正确，否则部分阅读器会拒绝打开。
 */
function buildPdf(lines = ['ToolKit Demo Output']) {
  const esc = (s) => String(s).replace(/([\\()])/g, '\\$1').replace(/[^\x20-\x7E]/g, '?');
  const content = [
    'BT',
    '/F1 20 Tf',
    '60 780 Td',
    `(${esc(lines[0] || 'ToolKit Demo Output')}) Tj`,
    '/F1 11 Tf',
    ...lines.slice(1).flatMap((line, i) => [`0 -22 Td`, `(${esc(line)}) Tj`]),
    'ET',
  ].join('\n');

  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] '
      + '/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
  ];

  let pdf = '%PDF-1.4\n';
  const offsets = [0];
  objects.forEach((body, i) => {
    offsets.push(pdf.length);
    pdf += `${i + 1} 0 obj\n${body}\nendobj\n`;
  });

  const xrefPos = pdf.length;
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (let i = 1; i <= objects.length; i += 1) {
    pdf += `${String(offsets[i]).padStart(10, '0')} 00000 n \n`;
  }
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefPos}\n%%EOF`;

  // 每个字符都是 ASCII，直接按字节写即可
  const bytes = new Uint8Array(pdf.length);
  for (let i = 0; i < pdf.length; i += 1) bytes[i] = pdf.charCodeAt(i) & 0xff;
  return new Blob([bytes], { type: 'application/pdf' });
}

/** 合成真实可播放的 WAV（正弦波，带淡入淡出防爆音） */
function buildWav({ seconds = 4, sampleRate = 44100, freqs = [440, 554.37, 659.25] } = {}) {
  const total = Math.floor(seconds * sampleRate);
  const buffer = new ArrayBuffer(44 + total * 2);
  const view = new DataView(buffer);

  const writeStr = (offset, str) => {
    for (let i = 0; i < str.length; i += 1) view.setUint8(offset + i, str.charCodeAt(i));
  };

  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + total * 2, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);        // PCM 子块大小
  view.setUint16(20, 1, true);         // 格式：PCM
  view.setUint16(22, 1, true);         // 单声道
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // 字节率
  view.setUint16(32, 2, true);         // 块对齐
  view.setUint16(34, 16, true);        // 位深
  writeStr(36, 'data');
  view.setUint32(40, total * 2, true);

  const segLen = Math.floor(total / freqs.length);
  for (let i = 0; i < total; i += 1) {
    const freq = freqs[Math.min(Math.floor(i / segLen), freqs.length - 1)];
    // 每段首尾各做 8% 的包络，避免切换音高时的爆音
    const posInSeg = (i % segLen) / segLen;
    const env = Math.min(1, posInSeg / 0.08, (1 - posInSeg) / 0.08);
    const sample = Math.sin((2 * Math.PI * freq * i) / sampleRate) * 0.28 * Math.max(0, env);
    view.setInt16(44 + i * 2, Math.round(sample * 32767), true);
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

/** 用 canvas 渲染一张真实的 PNG / JPG */
async function buildImage({ format = 'png', width = 1240, height = 1754, title = '' } = {}) {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');

  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, width, height);

  // 简单的版式示意：标题条 + 正文行
  ctx.fillStyle = '#2563eb';
  ctx.fillRect(0, 0, width, 116);
  ctx.fillStyle = '#ffffff';
  ctx.font = 'bold 42px sans-serif';
  ctx.fillText('ToolKit · 转换结果占位图', 56, 74);

  ctx.fillStyle = '#111827';
  ctx.font = '26px sans-serif';
  ctx.fillText(title || 'demo-output', 56, 190);

  ctx.fillStyle = '#9ca3af';
  for (let i = 0; i < 26; i += 1) {
    const w = width - 112 - randInt(0, Math.floor(width * 0.32));
    ctx.fillRect(56, 250 + i * 46, w, 14);
  }

  ctx.fillStyle = '#e5e7eb';
  ctx.fillRect(56, height - 140, width - 112, 2);
  ctx.fillStyle = '#6b7280';
  ctx.font = '22px sans-serif';
  ctx.fillText('本文件由演示模式生成，仅用于界面预览', 56, height - 90);

  return await new Promise((resolve) => {
    canvas.toBlob((blob) => resolve(blob), format === 'jpg' ? 'image/jpeg' : 'image/png', 0.92);
  });
}

/** 生成真实 HTML 文本 */
function buildHtml({ title, rows }) {
  return new Blob([`<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>${title}</title>
<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:48px auto;padding:0 20px;color:#111827}
h1{border-bottom:2px solid #2563eb;padding-bottom:12px}table{border-collapse:collapse;width:100%;margin-top:24px}
th,td{border:1px solid #d1d5db;padding:8px 12px;text-align:left}th{background:#f3f4f6}
.note{color:#6b7280;font-size:14px}</style></head>
<body><h1>${title}</h1>
<p class="note">本文件由全能工具站演示模式生成，用于验证下载与预览链路。</p>
<table><thead><tr><th>字段</th><th>值</th></tr></thead><tbody>
${rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('')}
</tbody></table></body></html>`], { type: 'text/html;charset=utf-8' });
}

/** 登记产出文件，返回 file 元信息 */
function registerFile(blob, name, mime) {
  const fileId = uid('f');
  fileStore.set(fileId, { blob, name, mime });
  return { file_id: fileId, name, size: blob.size, mime, url: `/api/download/${fileId}` };
}

/** 供下载接口取用 */
export function getMockFile(fileId) {
  return fileStore.get(fileId) || null;
}

/* ------------------------------------------------------------------ *
 * 2. 根据目标格式产出对应文件
 * ------------------------------------------------------------------ */
async function produceOutput({ target, source, inputs, params }) {
  const srcName = inputs[0]?.name || 'input';
  const stem = baseName(srcName) || 'output';
  const ext = outputExt(target, source);
  const outName = `${stem}.${ext}`;
  const rows = [
    ['源文件', srcName],
    ['源格式', source],
    ['目标格式', target],
    ['引擎', lookup(source, target)?.engine || '—'],
    ['参数', JSON.stringify(params || {})],
    ['生成时间', new Date().toLocaleString('zh-CN')],
  ];

  switch (target) {
    case 'pdf':
      return registerFile(
        buildPdf([
          'ToolKit · Conversion Result',
          '',
          `Source file : ${srcName}`,
          `Source type : ${source}`,
          `Engine      : ${lookup(source, target)?.engine || 'N/A'}`,
          `Generated   : ${new Date().toISOString()}`,
          '',
          'This file was produced by the toolkits demo mode.',
          'It is a real, openable PDF used to verify the download',
          'and preview pipeline. Replace with the real engine output',
          'once the Python backend is connected.',
        ]),
        outName,
        'application/pdf',
      );

    case 'png':
    case 'jpg': {
      // PDF → 图片时按页导出，这里模拟成 ZIP（与后端行为一致）
      if (source === 'pdf') {
        const zip = buildZipPlaceholder(`${ext.toUpperCase()} 图片序列`, rows);
        return registerFile(zip, `${stem}-pages.zip`, 'application/zip');
      }
      const blob = await buildImage({
        format: target === 'jpg' ? 'jpg' : 'png',
        title: srcName,
      });
      return registerFile(blob, outName, target === 'jpg' ? 'image/jpeg' : 'image/png');
    }

    case 'html':
      return registerFile(buildHtml({ title: `${stem} · 转换结果`, rows }), outName, 'text/html;charset=utf-8');

    default:
      // docx / xlsx / pptx 皆为 ZIP 容器，演示模式下产出带说明的占位文件
      return registerFile(
        new Blob([
          `全能工具站 · 演示模式占位文件\n`,
          `==============================\n\n`,
          `期望输出格式：.${ext}\n`,
          `源文件：${srcName}\n`,
          `转换引擎：${lookup(source, target)?.engine || '—'}\n`,
          `参数：${JSON.stringify(params)}\n\n`,
          `说明：后端未连接时，前端无法真正生成二进制 Office 文档。\n`,
          `接入 FastAPI 后端（LibreOffice / pdf2docx / openpyxl）后，\n`,
          `此文件将被替换为真实可编辑文档。\n`,
        ], { type: 'text/plain;charset=utf-8' }),
        outName,
        'text/plain;charset=utf-8',
      );
  }
}

/** 极简 ZIP（仅存储、无压缩），用于批量结果打包 —— 生成的是合法 ZIP */
function buildZipPlaceholder(title, rows) {
  const enc = new TextEncoder();
  const files = [{ name: 'README.txt', data: enc.encode(`${title}\n\n${rows.map((r) => r.join(': ')).join('\n')}\n`) }];

  const chunks = [];
  const central = [];
  let offset = 0;

  const crcTable = (() => {
    const table = new Uint32Array(256);
    for (let n = 0; n < 256; n += 1) {
      let c = n;
      for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      table[n] = c >>> 0;
    }
    return table;
  })();

  const crc32 = (bytes) => {
    let c = 0xffffffff;
    for (let i = 0; i < bytes.length; i += 1) c = crcTable[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };

  const u16 = (v) => [v & 0xff, (v >>> 8) & 0xff];
  const u32 = (v) => [v & 0xff, (v >>> 8) & 0xff, (v >>> 16) & 0xff, (v >>> 24) & 0xff];

  for (const file of files) {
    const nameBytes = enc.encode(file.name);
    const crc = crc32(file.data);
    const local = [
      ...u32(0x04034b50), ...u16(20), ...u16(0), ...u16(0), ...u16(0), ...u16(0),
      ...u32(crc), ...u32(file.data.length), ...u32(file.data.length),
      ...u16(nameBytes.length), ...u16(0),
    ];
    chunks.push(new Uint8Array(local), nameBytes, file.data);

    central.push([
      ...u32(0x02014b50), ...u16(20), ...u16(20), ...u16(0), ...u16(0), ...u16(0), ...u16(0),
      ...u32(crc), ...u32(file.data.length), ...u32(file.data.length),
      ...u16(nameBytes.length), ...u16(0), ...u16(0), ...u16(0), ...u16(0), ...u32(0),
      ...u32(offset), ...nameBytes,
    ]);
    offset += local.length + nameBytes.length + file.data.length;
  }

  const centralStart = offset;
  let centralSize = 0;
  for (const entry of central) {
    chunks.push(new Uint8Array(entry));
    centralSize += entry.length;
  }

  chunks.push(new Uint8Array([
    ...u32(0x06054b50), ...u16(0), ...u16(0), ...u16(files.length), ...u16(files.length),
    ...u32(centralSize), ...u32(centralStart), ...u16(0),
  ]));

  return new Blob(chunks, { type: 'application/zip' });
}

/** 供批量打包接口使用 */
export function buildTasksZip(taskList) {
  return buildZipPlaceholder('批量转换结果', taskList.map((t, i) => [
    `#${i + 1}`,
    `${t.inputs?.[0]?.name || 'unknown'} → ${t.output?.name || '未产出'}（${t.status}）`,
  ]));
}

/* ------------------------------------------------------------------ *
 * 3. 任务状态机（模拟排队 → 执行 → 完成）
 * ------------------------------------------------------------------ */
const TICK_MS = 450;

/** 判定该任务是否应当失败，用于演示错误态 */
function failReasonFor(inputs, source, target) {
  const names = inputs.map((f) => f.name.toLowerCase()).join(' ');
  if (/corrupt|损坏|broken/.test(names)) return '文件已损坏或不是有效的源文件，无法解析（演示用例）';
  if (source === 'xlsx' && target === 'pptx') return 'Excel 数据无法映射为幻灯片版面，该转换方向不支持';
  if (source === 'html' && target === 'pptx') return 'HTML 的流式布局无法映射为幻灯片版面，该转换方向不支持';
  return null;
}

let ticker = null;
function ensureTicker() {
  if (ticker) return;
  ticker = setInterval(() => {
    let alive = false;
    for (const task of tasks.values()) {
      if (task.status === 'pending') {
        alive = true;
        // 模拟排队耗时
        if (Date.now() - new Date(task.created_at).getTime() > randInt(700, 1600)) {
          task.status = 'running';
          task.progress = randInt(4, 12);
          task.started_at = now();
        }
      } else if (task.status === 'running') {
        alive = true;
        // 前 90% 快速推进，最后 10% 放慢，模拟"打包/写盘"阶段
        const step = task.progress < 80 ? randInt(9, 20) : randInt(2, 7);
        task.progress = Math.min(100, task.progress + step);
        if (task.progress >= 100) {
          task.progress = 100;
          if (task.__failReason) {
            task.status = 'failed';
            task.error = task.__failReason;
          } else {
            task.status = 'success';
            task.output = task.__output;
          }
          delete task.__failReason;
          delete task.__output;
          task.finished_at = now();
          task.expire_at = new Date(Date.now() + 24 * 3600 * 1000).toISOString();
        }
      }
    }
    // 没有进行中的任务时停掉定时器，避免空转消耗电量
    if (!alive) {
      clearInterval(ticker);
      ticker = null;
    }
  }, TICK_MS);
}

/** 创建任务并送入模拟状态机 */
function createTask({ module, operation, inputs, produce }) {
  const taskId = uid('t');
  const task = {
    task_id: taskId,
    module,
    operation,
    status: 'pending',
    progress: 0,
    inputs: inputs.map((f) => ({
      name: f.name,
      size: f.size,
      sha256: (f.__hash || 'demo0000').slice(0, 8),
      // 视频下载任务没有本地文件，source 记录作品长链（契约 §3.3）；
      // 其它模块的输入不带该字段，保持原行为不变。
      ...(f.source ? { source: f.source } : {}),
    })),
    output: null,
    error: null,
    // 契约 §3.3 的任务字段包含 error_code；演示模式的失败原因只有中文文案，机器码置 null。
    error_code: null,
    created_at: now(),
    started_at: null,
    finished_at: null,
    expire_at: null,
    demo: true,
  };
  tasks.set(taskId, task);

  // 预先算好结果与失败判定，状态机只负责推进进度
  const reason = produce.failReason || null;
  if (reason) {
    task.__failReason = reason;
  } else {
    // 产出过程异步，用占位对象承接
    produce.outputs().then((output) => { task.__output = output; }).catch((err) => {
      task.__failReason = err?.message || '转换失败';
    });
  }

  ensureTicker();
  return clone(task);
}

/* ------------------------------------------------------------------ *
 * 4. 对外接口（与 api.js 的调用签名一一对应）
 * ------------------------------------------------------------------ */

const ENGINES = {
  libreoffice: { state: 'ok', version: '7.6.7', path: 'runtime/libreoffice/program/soffice', hint: null },
  chromium:    { state: 'ok', version: '125.0.6422.60', path: 'runtime/chromium/chrome', hint: null },
  ffmpeg:      { state: 'warn', version: '6.1.1', path: 'runtime/ffmpeg', hint: '当前为演示模式，实际转码未执行' },
};

/* ------------------------------------------------------------------ *
 * 5. 视频下载（演示模式）
 *
 * 与后端契约（docs/视频下载模块-实现契约.md §3）保持同构：字段名、嵌套层级、
 * 分页结构完全一致，保证演示模式与真实模式共用同一套 UI 而无需分支。
 * 任务复用 createTask + ticker，产物用 registerFile 登记为真实可下载的 Blob；
 * 不额外实现 ticker、不模拟真实解析请求，也不访问任何外网。
 * ------------------------------------------------------------------ */

/** 平台元信息：hosts 与后端 adapters/registry.py 的白名单保持一致 */
const VIDEO_PLATFORM_META = {
  douyin: {
    label: '抖音',
    hosts: ['douyin.com', 'www.douyin.com', 'v.douyin.com', 'iesdouyin.com', 'www.iesdouyin.com'],
    capabilities: { video: true, image: true, multi_part: false },
  },
  bilibili: {
    label: 'B站',
    hosts: ['bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 'b23.tv'],
    capabilities: { video: true, image: false, multi_part: true },
  },
  kuaishou: {
    label: '快手',
    hosts: ['kuaishou.com', 'www.kuaishou.com', 'v.kuaishou.com', 'm.kuaishou.com'],
    capabilities: { video: true, image: true, multi_part: false },
  },
};

const VIDEO_URL_RE = /https?:\/\/[^\s"'<>（）()，。！？【】]+/g;

/** 清晰度排序（下标越小越高），与契约 §4.2 的归一化等级一致 */
const VIDEO_QUALITY_RANK = ['4K', '2K', '1080P60', '1080P', '720P', '540P', '480P', '360P', 'audio'];

/** 构造一条 VideoFormat（缺省字段与契约 §3.2 对齐，缺省值不用 undefined） */
function videoFormat(id, quality, width, height, fps, bitrate, size, extra = {}) {
  return {
    format_id: id,
    quality,
    width,
    height,
    fps,
    bitrate,
    container: 'mp4',
    size_bytes: size,
    need_login: false,
    need_vip: false,
    expire_at: null,
    note: null,
    ...extra,
  };
}

/** 三平台演示样例（字段与真实适配器输出同构） */
const VIDEO_SAMPLES = {
  douyin: {
    title: '晚风里的一百种蓝',
    author: '山野来信',
    duration_sec: 151,
    formats: [
      videoFormat('v-1080', '1080P', 1920, 1080, 30, 2500000, 47187500),
      videoFormat('v-720', '720P', 1280, 720, 30, 1200000, 22650000),
      videoFormat('v-540', '540P', 960, 540, 30, 800000, 15100000),
    ],
  },
  bilibili: {
    title: '示例稿件 · 城市观察',
    author: '观察者A',
    duration_sec: 622,
    formats: [
      // 会员专属清晰度：界面置灰、不可下载（契约 §4.4.6）
      videoFormat('120', '4K', 3840, 2160, 60, 12000000, 335544320, {
        need_vip: true, note: '会员专属清晰度，仅展示不下载',
      }),
      videoFormat('80', '1080P', 1920, 1080, 30, 2600000, 92274688),
      videoFormat('64', '720P', 1280, 720, 30, 1300000, 46137344),
      videoFormat('32', '480P', 854, 480, 30, 600000, 20971520),
    ],
  },
  kuaishou: {
    title: '老街的清晨',
    author: '北方小城',
    duration_sec: 87,
    formats: [
      videoFormat('ks-1080', '1080P', 1920, 1080, 30, 2300000, 25000000),
      videoFormat('ks-720', '720P', 1280, 720, 30, 1100000, 12000000),
      videoFormat('ks-480', '480P', 854, 480, 30, 600000, 6500000),
    ],
  },
};

/** 演示封面：内联 SVG data URI（离线可用，且不产生任何外网请求） */
function videoCoverDataUrl(platform, title) {
  const label = VIDEO_PLATFORM_META[platform]?.label || platform || '视频';
  const text = String(title || '演示封面').slice(0, 14)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">'
    + '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    + '<stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#8b5cf6"/>'
    + '</linearGradient></defs>'
    + '<rect width="320" height="180" fill="url(#g)"/>'
    + '<circle cx="160" cy="76" r="27" fill="rgba(255,255,255,0.2)"/>'
    + '<path d="M152 63l22 13-22 13z" fill="#ffffff"/>'
    + `<text x="16" y="152" fill="#ffffff" font-family="sans-serif" font-size="14">${label} · ${text}</text>`
    + '</svg>';
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/** 按 host 精确匹配平台（与后端域名白名单同构，不做子域通配） */
function videoPlatformOf(url) {
  let host = '';
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch {
    return null;
  }
  for (const [name, meta] of Object.entries(VIDEO_PLATFORM_META)) {
    if (meta.hosts.includes(host)) return name;
  }
  return null;
}

/** 从链接中推断作品 ID（演示用，尽量贴近真实 ID 形态） */
function videoItemIdOf(url, platform) {
  try {
    if (platform === 'bilibili') {
      const bv = String(url).match(/BV[0-9A-Za-z]+/);
      if (bv) return bv[0];
    }
    if (platform === 'douyin') {
      const m = String(url).match(/video\/(\d+)/);
      if (m) return m[1];
    }
    const parts = new URL(url).pathname.split('/').filter(Boolean);
    const last = parts[parts.length - 1] || '';
    return last.replace(/[^\w-]/g, '').slice(0, 40) || `demo-${Date.now().toString(36)}`;
  } catch {
    return `demo-${Date.now().toString(36)}`;
  }
}

/** 默认清晰度：优先用户偏好，否则取 ≤1080P 的最高可用档（绝不默认 4K） */
function pickVideoDefaultFormat(formats, preferQuality) {
  const usable = formats.filter((f) => !f.need_login && !f.need_vip);
  const preferred = usable.find((f) => f.quality === preferQuality);
  if (preferred) return preferred.format_id;
  const capIndex = VIDEO_QUALITY_RANK.indexOf('1080P');
  const capped = usable.filter((f) => VIDEO_QUALITY_RANK.indexOf(f.quality) >= capIndex);
  const chosen = capped[0] || usable[0] || formats[0];
  return chosen ? chosen.format_id : null;
}

/** 构造解析项（部分成功时也带 item，供前端降级下载） */
function buildVideoItem(platform, url, { needLogin = false, preferQuality = '1080P' } = {}) {
  const sample = VIDEO_SAMPLES[platform];
  const formats = sample.formats.map((f) => ({ ...f }));

  if (needLogin) {
    // 演示「需要登录才可下载更高清晰度」：最高非会员档置 need_login，保留 720P 可下载
    const target = formats.find((f) => !f.need_vip);
    if (target) {
      target.need_login = true;
      target.note = '需要登录后可用（请先在设置中填写 Cookie）';
    }
  }

  const usable = formats.filter((f) => !f.need_login && !f.need_vip);
  const defaultFormat = needLogin
    ? (usable.find((f) => f.quality === '720P') || usable[0] || null)
    : null;
  const defaultFormatId = needLogin
    ? (defaultFormat ? defaultFormat.format_id : null)
    : pickVideoDefaultFormat(formats, preferQuality);

  return {
    platform,
    item_id: videoItemIdOf(url, platform),
    url,
    media_type: 'video',
    title: sample.title,
    description: '',
    author: { name: sample.author, id: `demo-${platform}-author`, avatar_url: '' },
    cover_url: videoCoverDataUrl(platform, sample.title),
    duration_sec: sample.duration_sec,
    published_at: new Date(Date.now() - 3 * 24 * 3600 * 1000).toISOString(),
    stats: {
      like: randInt(120, 9800),
      comment: randInt(8, 320),
      share: randInt(2, 120),
      play: randInt(10000, 480000),
    },
    formats,
    images: [],
    piece_count: 1,
    warnings: needLogin ? ['LOGIN_REQUIRED'] : [],
    default_format_id: defaultFormatId,
    parse_source: 'api',
  };
}

/** fileId → 产物文件名（历史记录只存 file_id，不存媒体直链） */
const videoFileNames = new Map();
/** 解析缓存：platform:item_id → item，供下载时补齐 media_type 等字段 */
const videoParseCache = new Map();

/** 演示设置与历史的内存存储（不落盘，刷新页面即重置） */
const videoState = {
  settings: {
    enabled: true,
    prefer_quality: '1080P',
    prefer_format: 'mp4',
    max_concurrent: 2,
    download_segments: 4,
    max_file_mb: 2048,
    platforms: {
      douyin: { enabled: true, cookie: { configured: false, masked: null } },
      bilibili: { enabled: true, cookie: { configured: false, masked: null } },
      kuaishou: { enabled: true, cookie: { configured: false, masked: null } },
    },
  },
  history: [],
};

/** 生成一条历史记录（契约 §3.4 字段） */
function makeVideoHistoryItem({
  platform, title, author, quality, sizeMb, minutesAgo, status = 'success', reused = false,
}) {
  const created = new Date(Date.now() - minutesAgo * 60 * 1000).toISOString();
  return {
    platform,
    item_id: `demo-${platform}-${minutesAgo}`,
    title,
    author,
    cover_url: videoCoverDataUrl(platform, title),
    quality,
    size_bytes: sizeMb * 1024 * 1024,
    status,
    task_id: `t_demo_${platform}_${minutesAgo}`,
    file_id: null,
    created_at: created,
    expire_at: new Date(new Date(created).getTime() + 24 * 3600 * 1000).toISOString(),
    ...(reused ? { reused: true } : {}),
  };
}

// 预置几条历史，保证演示模式下抽屉一打开就有内容（file_id 为空 → 不触发复用）
videoState.history = [
  makeVideoHistoryItem({
    platform: 'douyin', title: '晚风里的一百种蓝', author: '山野来信',
    quality: '1080P', sizeMb: 45, minutesAgo: 35, reused: true,
  }),
  makeVideoHistoryItem({
    platform: 'bilibili', title: '示例稿件 · 城市观察', author: '观察者A',
    quality: '720P', sizeMb: 44, minutesAgo: 130,
  }),
  makeVideoHistoryItem({
    platform: 'kuaishou', title: '老街的清晨', author: '北方小城',
    quality: '1080P', sizeMb: 23, minutesAgo: 300,
  }),
  makeVideoHistoryItem({
    platform: 'douyin', title: '雨后的巷口', author: '青石巷',
    quality: '720P', sizeMb: 18, minutesAgo: 1500,
  }),
  makeVideoHistoryItem({
    platform: 'bilibili', title: '城市夜跑记录', author: '跑者阿良',
    quality: '480P', sizeMb: 9, minutesAgo: 2900, status: 'failed',
  }),
];

/** 大端 32 位整数 */
function u32be(value) {
  return new Uint8Array([
    (value >>> 24) & 0xff, (value >>> 16) & 0xff, (value >>> 8) & 0xff, value & 0xff,
  ]);
}

/** 构造结构合法的最小 MP4（ftyp + mdat），用于验证下载链路（不可播放但可下载） */
function buildMp4Placeholder({ title, platform, quality }) {
  const text = '全能工具站 · 演示模式视频占位文件\n'
    + '====================================\n'
    + `平台：${VIDEO_PLATFORM_META[platform]?.label || platform || '—'}\n`
    + `标题：${title || '未命名作品'}\n`
    + `清晰度：${quality || '1080P'}\n`
    + '说明：后端未连接，此文件仅用于验证「任务 → 产物 → 下载」链路。\n';
  const payload = new TextEncoder().encode(text);
  const ftyp = new Uint8Array([
    0, 0, 0, 0x20, 0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6f, 0x6d,
    0, 0, 2, 0, 0x69, 0x73, 0x6f, 0x6d, 0x69, 0x73, 0x6f, 0x32,
    0x61, 0x76, 0x63, 0x31, 0x6d, 0x70, 0x34, 0x31,
  ]);
  const mdat = new Uint8Array([...u32be(payload.length + 8), 0x6d, 0x64, 0x61, 0x74]);
  return new Blob([ftyp, mdat, payload], { type: 'video/mp4' });
}

/** 仅音频产物占位文件 */
function buildAudioPlaceholder({ title, format }) {
  const text = '全能工具站 · 演示模式音频产物占位文件\n'
    + `标题：${title || '未命名作品'}\n`
    + `格式：${format || 'mp3'}\n`
    + '说明：演示模式未执行真实转码，此文件仅用于验证下载链路。\n';
  return new Blob([new TextEncoder().encode(text)], {
    type: format === 'm4a' ? 'audio/mp4' : 'audio/mpeg',
  });
}

/** 清洗文件名片段（与后端 naming.py 的清洗规则保持一致） */
function sanitizeVideoPart(text, max = 40) {
  return String(text || '')
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, max);
}

/** 产物命名：{platform}-{author}-{title}-{quality}.{ext}（总长 ≤120） */
function buildVideoFileName(item) {
  const platform = VIDEO_PLATFORM_META[item.platform]?.label || item.platform || '视频';
  const author = sanitizeVideoPart(item.author, 20) || '未知作者';
  const title = sanitizeVideoPart(item.title, 48) || '未命名作品';
  const quality = sanitizeVideoPart(item.quality, 12) || '原画';
  const isImage = item.media_type === 'image';
  const ext = isImage ? 'zip' : (item.output === 'mp3' || item.output === 'm4a' ? item.output : 'mp4');
  return `${platform}-${author}-${title}-${quality}.${ext}`.slice(0, 120);
}

/** Cookie 脱敏：接口与界面都只能看到这个结果（契约 §3.5） */
function maskVideoCookie(platform, raw) {
  const value = String(raw || '').trim();
  if (!value) return null;
  if (platform === 'bilibili') {
    const match = value.match(/SESSDATA=([^;]+)/i);
    const tail = (match ? match[1] : value).slice(-4);
    return `SESSDATA=****${tail}`;
  }
  const names = value.split(';').map((s) => s.trim()).filter(Boolean)
    .map((pair) => pair.split('=')[0]).filter(Boolean).slice(0, 4);
  return `${names.length ? `${names.join(';')};` : ''}****${value.slice(-4)}`;
}

/** 追加历史（FIFO 上限 200，与后端 history_limit 对齐） */
function pushVideoHistory(entry) {
  videoState.history.unshift(entry);
  if (videoState.history.length > 200) videoState.history.length = 200;
  return entry;
}

/** 24h 内同平台同作品且产物未过期 → 可复用 */
function findReusableVideoHistory(item) {
  const nowTime = Date.now();
  return videoState.history.find((entry) => (
    entry.platform === item.platform
    && entry.item_id === item.item_id
    && entry.status === 'success'
    && Boolean(entry.file_id)
    && (!entry.expire_at || new Date(entry.expire_at).getTime() > nowTime)
  ));
}

/** 命中复用：新建一个「立即成功」的任务（契约 §3.3 的去重语义） */
function createReusedVideoTask(item, entry) {
  const taskId = uid('t');
  const task = {
    task_id: taskId,
    module: 'video',
    operation: 'download',
    status: 'success',
    progress: 100,
    inputs: [{
      name: item.title || '视频',
      size: 0,
      sha256: 'demo0000',
      source: item.url || '',
    }],
    output: {
      file_id: entry.file_id,
      name: videoFileNames.get(entry.file_id) || buildVideoFileName(item),
      size: entry.size_bytes || 0,
      mime: 'video/mp4',
      url: `/api/download/${entry.file_id}`,
    },
    error: null,
    error_code: null,
    created_at: now(),
    started_at: now(),
    finished_at: now(),
    expire_at: entry.expire_at,
    demo: true,
    reused: true,
    reused_from: entry.created_at,
  };
  tasks.set(taskId, task);
  entry.reused = true;
  return clone(task);
}

export const mock = {
  /** 健康检查 */
  async healthz() {
    return {
      ok: true,
      version: '1.0.0-demo',
      demo: true,
      disk_free_bytes: 42 * 1024 ** 3,
      engines: ENGINES,
    };
  },

  /** 引擎自检 */
  async engines() {
    return { engines: ENGINES, runtime: { platform: 'demo', python: '3.13' } };
  },

  /** 登录：演示模式接受任意非空密码 */
  async login({ username, password }) {
    if (!username || !password) {
      const err = new Error('用户名与密码不能为空');
      err.status = 400;
      throw err;
    }
    return {
      token: `demo.${btoa(`${username}:${Date.now()}`)}`,
      user: { username, role: 'admin' },
      expires_in: 86400,
    };
  },

  /** 格式矩阵（直接返回前端静态定义，结构与后端约定一致） */
  async matrix() {
    return {
      sources: ['html', 'docx', 'xlsx', 'pptx', 'pdf'],
      targets: ['docx', 'xlsx', 'pptx', 'pdf', 'html', 'png'],
      matrix: clone(MATRIX),
      engines: ENGINES,
    };
  },

  /**
   * 文档转换：单文件或批量
   * @param {File[]} files
   * @param {{target:string, source:string, params:object}} options
   */
  async convert(files, options) {
    const { target, source, params } = options;

    if (!lookup(source, target)?.grade) {
      const err = new Error(`不支持 ${source.toUpperCase()} → ${target.toUpperCase()} 的转换`);
      err.status = 400;
      err.code = 'UNSUPPORTED_PAIR';
      throw err;
    }

    const valid = files.filter((f) => f && f.size > 0);
    if (valid.length === 0) {
      const err = new Error('请先选择要转换的文件');
      err.status = 400;
      throw err;
    }

    return valid.map((file) => createTask({
      module: 'converter',
      operation: `${source}2${target}`,
      inputs: [file],
      produce: {
        failReason: failReasonFor([file], source, target),
        outputs: () => produceOutput({ target, source, inputs: [file], params }),
      },
    }));
  },

  /** 批量转换（同一目标格式） */
  async convertBatch(files, options) {
    return this.convert(files, options);
  },

  /** 音频嗅探 */
  async audioProbe(files) {
    const results = [];
    for (const file of files) {
      let head = null;
      try {
        const buf = await file.slice(0, 32).arrayBuffer();
        head = new Uint8Array(buf);
      } catch { /* 读取失败时退化为仅按扩展名判断 */ }

      const sniffed = head ? sniffMagic(head) : null;
      const ext = extOf(file.name);
      const proprietary = ['ncm', 'qmc0', 'qmc3', 'qmcflac', 'qmcogg', 'mgg', 'mflac', 'kgm', 'vpr', 'kwm', 'mg3d'];
      const isProprietary = proprietary.includes(ext);

      results.push({
        name: file.name,
        size: file.size,
        detected_type: sniffed?.type || ext || 'unknown',
        detected_label: sniffed?.label || (ext ? `.${ext} 文件` : '无法识别'),
        decryptable: isProprietary ? true : Boolean(sniffed),
        container: isProprietary ? ext : (sniffed?.type || null),
        duration_sec: null,
        note: isProprietary
          ? '检测到平台专有加密格式，将在服务端执行解封装'
          : '通用音频格式，直接转码',
      });
    }
    return { results };
  },

  /** 音频转换 */
  async audioConvert(files, options) {
    const { outputFormat, bitrate } = options;
    const valid = files.filter((f) => f && f.size > 0);
    if (valid.length === 0) {
      const err = new Error('请先选择音频文件');
      err.status = 400;
      throw err;
    }

    return valid.map((file) => {
      const seconds = randInt(3, 6);
      return createTask({
        module: 'audio',
        operation: `${extOf(file.name) || 'audio'}2${outputFormat}`,
        inputs: [file],
        produce: {
          failReason: /corrupt|损坏/.test(file.name.toLowerCase())
            ? '该文件不是可识别的音频容器，可能已损坏或受 DRM 严格保护'
            : null,
          outputs: async () => {
            // 演示模式不做真实编码：统一产出可播放的 WAV 合成音，
            // 以便完整验证「下载 → 播放器 → 时长展示」链路。
            // 文件名保留用户请求的扩展名（便于校验命名逻辑），MIME 如实为 audio/wav。
            const blob = buildWav({ seconds });
            const stem = baseName(file.name);
            const targetExt = outputFormat === 'mp4' ? 'm4a' : outputFormat;
            return registerFile(blob, `${stem}.${targetExt}`, 'audio/wav');
          },
        },
      });
    });
  },

  /* -------------------- 视频下载 -------------------- */

  /** 平台状态（契约 §3.1） */
  async videoPlatforms() {
    const s = videoState.settings;
    return {
      enabled: s.enabled,
      items: Object.entries(VIDEO_PLATFORM_META).map(([name, meta]) => {
        const conf = s.platforms[name] || { enabled: true, cookie: { configured: false, masked: null } };
        return {
          name,
          label: meta.label,
          enabled: conf.enabled !== false,
          hosts: [...meta.hosts],
          capabilities: { ...meta.capabilities },
          cookie: clone(conf.cookie || { configured: false, masked: null }),
        };
      }),
      max_urls: 20,
      prefer_quality: s.prefer_quality,
      prefer_format: s.prefer_format,
      engine: {
        ffmpeg: { state: 'warn', version: '6.1.1', hint: '演示模式未执行真实合并' },
        ytdlp: { state: 'fail', version: null, hint: '演示模式未安装 yt-dlp，已跳过该兜底通道' },
      },
    };
  },

  /**
   * 解析（契约 §3.2）：逐条返回，单条失败不抛异常。
   * 演示样例按序轮转：成功 → 需登录（部分成功）→ 硬失败，便于一次看到三种状态。
   */
  async videoParse(urls, preferQuality) {
    const prefer = preferQuality || videoState.settings.prefer_quality || '1080P';
    const clean = [];
    const seen = new Set();
    for (const raw of [].concat(urls || [])) {
      const text = String(raw || '');
      const found = text.match(VIDEO_URL_RE) || (text ? [text] : []);
      for (const candidate of found) {
        const url = candidate.replace(/[.,;:!?]+$/, '').replace(/^http:\/\//i, 'https://');
        if (!/^https?:\/\//i.test(url) || seen.has(url)) continue;
        seen.add(url);
        clean.push(url);
      }
    }

    if (clean.length === 0) {
      const err = new Error('没有识别到作品链接，请粘贴完整分享文案或链接');
      err.status = 400;
      err.code = 'LINK_UNRECOGNIZED';
      throw err;
    }

    const results = clean.map((url, index) => {
      const platform = videoPlatformOf(url);
      if (!platform) {
        return {
          ok: false,
          url,
          error: { code: 'PLATFORM_UNSUPPORTED', detail: '暂不支持该平台，当前支持抖音、B站、快手' },
        };
      }

      const mode = index % 3;
      if (mode === 2) {
        return {
          ok: false,
          url,
          error: { code: 'PARSE_FAILED', detail: '解析失败：平台返回内容不完整，请稍后重试（演示用例）' },
        };
      }

      const needLogin = mode === 1;
      const item = buildVideoItem(platform, url, { needLogin, preferQuality: prefer });
      videoParseCache.set(`${platform}:${item.item_id}`, item);

      if (!needLogin) return { ok: true, url, cached: false, item };
      return {
        ok: false,
        url,
        error: { code: 'LOGIN_REQUIRED', detail: '该清晰度需要登录，当前仅可下载 720P' },
        item,
      };
    });

    return { results, elapsed_ms: 900 + randInt(100, 700) };
  },

  /** 提交下载（契约 §3.3）：返回任务数组，任务复用现有 createTask/ticker */
  async videoDownload(items) {
    const list = [].concat(items || []);
    if (list.length === 0) {
      const err = new Error('请先选择要下载的作品');
      err.status = 400;
      err.code = 'VALIDATION_ERROR';
      throw err;
    }

    const results = [];
    for (const raw of list) {
      const item = raw || {};
      const cached = videoParseCache.get(`${item.platform}:${item.item_id}`) || {};
      const merged = { ...cached, ...item };

      // 24h 内同作品且产物未过期 → 秒回，前端据此提示「已为你复用」
      const reusable = findReusableVideoHistory(merged);
      if (reusable) {
        results.push(createReusedVideoTask(merged, reusable));
        continue;
      }

      const fileName = buildVideoFileName(merged);
      const createOne = createTask({
        module: 'video',
        operation: 'download',
        inputs: [{ name: merged.title || fileName, size: 0, source: merged.url || '' }],
        produce: {
          outputs: async () => {
            let blob;
            let mime;
            if (merged.media_type === 'image') {
              blob = buildZipPlaceholder('图文作品', [
                ['标题', merged.title || '未命名作品'],
                ['平台', VIDEO_PLATFORM_META[merged.platform]?.label || merged.platform || '—'],
              ]);
              mime = 'application/zip';
            } else if (merged.output === 'mp3' || merged.output === 'm4a') {
              blob = buildAudioPlaceholder({ title: merged.title, format: merged.output });
              mime = merged.output === 'm4a' ? 'audio/mp4' : 'audio/mpeg';
            } else {
              blob = buildMp4Placeholder({
                title: merged.title, platform: merged.platform, quality: merged.quality,
              });
              mime = 'video/mp4';
            }

            const out = registerFile(blob, fileName, mime);
            videoFileNames.set(out.file_id, out.name);
            // 产物登记后写历史。createOne 要等 createTask 返回后才可用，
            // 因此延到宏任务里再取 task_id（不阻塞产物登记）。
            setTimeout(() => {
              pushVideoHistory({
                platform: merged.platform,
                item_id: merged.item_id,
                title: merged.title,
                author: merged.author,
                cover_url: videoCoverDataUrl(merged.platform, merged.title),
                quality: merged.quality,
                size_bytes: out.size,
                status: 'success',
                task_id: createOne?.task_id || null,
                file_id: out.file_id,
                created_at: now(),
                expire_at: new Date(Date.now() + 24 * 3600 * 1000).toISOString(),
              });
            }, 0);
            return out;
          },
        },
      });
      results.push(createOne);
    }
    return results;
  },

  /** 历史列表（契约 §3.4）：平台筛选 + 标题/作者搜索 + 分页 */
  async videoHistory(params = {}) {
    const page = Math.max(1, Number(params.page) || 1);
    const pageSize = Math.min(100, Math.max(1, Number(params.pageSize ?? params.page_size) || 20));
    let list = [...videoState.history];

    if (params.platform) list = list.filter((entry) => entry.platform === params.platform);
    const keyword = String(params.keyword || '').trim().toLowerCase();
    if (keyword) {
      list = list.filter((entry) => (
        String(entry.title || '').toLowerCase().includes(keyword)
        || String(entry.author || '').toLowerCase().includes(keyword)
      ));
    }

    list.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    const start = (page - 1) * pageSize;
    return {
      total: list.length,
      page,
      page_size: pageSize,
      items: clone(list.slice(start, start + pageSize)),
    };
  },

  async removeVideoHistory(itemId, platform) {
    const before = videoState.history.length;
    videoState.history = videoState.history.filter((entry) => !(
      entry.item_id === itemId && (!platform || entry.platform === platform)
    ));
    return { ok: true, removed: before - videoState.history.length };
  },

  async clearVideoHistory() {
    const removed = videoState.history.length;
    videoState.history = [];
    return { ok: true, removed };
  },

  /** 读取设置（Cookie 只回显脱敏值） */
  async videoSettings() {
    return clone(videoState.settings);
  },

  /** 更新设置（部分字段；cookies 空字符串表示清除、缺省表示不修改） */
  async videoUpdateSettings(patch = {}) {
    const s = videoState.settings;
    if (patch.prefer_quality) s.prefer_quality = patch.prefer_quality;
    if (patch.prefer_format) s.prefer_format = patch.prefer_format;
    if (patch.max_concurrent !== undefined) {
      s.max_concurrent = Math.min(4, Math.max(1, Number(patch.max_concurrent) || 2));
    }
    if (patch.download_segments !== undefined) {
      s.download_segments = Math.min(8, Math.max(1, Number(patch.download_segments) || 4));
    }
    if (patch.max_file_mb !== undefined) {
      s.max_file_mb = Math.min(10240, Math.max(1, Number(patch.max_file_mb) || 2048));
    }
    if (patch.enabled !== undefined) s.enabled = Boolean(patch.enabled);

    if (patch.cookies && typeof patch.cookies === 'object') {
      for (const [platform, value] of Object.entries(patch.cookies)) {
        if (!s.platforms[platform]) continue;
        const raw = String(value ?? '').trim();
        if (raw === '') {
          s.platforms[platform].cookie = { configured: false, masked: null };
        } else {
          s.platforms[platform].cookie = {
            configured: true,
            masked: maskVideoCookie(platform, raw),
            updated_at: now(),
          };
        }
      }
    }

    if (patch.platforms && typeof patch.platforms === 'object') {
      for (const [platform, conf] of Object.entries(patch.platforms)) {
        if (!s.platforms[platform] || !conf || typeof conf !== 'object') continue;
        if (conf.enabled !== undefined) s.platforms[platform].enabled = Boolean(conf.enabled);
      }
    }
    return clone(s);
  },

  /** Markdown 导出 */
  async mdExport({ html, filename, format }) {
    if (!html) {
      const err = new Error('导出内容为空');
      err.status = 400;
      throw err;
    }
    const stem = baseName(filename || 'document');

    // HTML 导出无需排队，直接返回文件
    if (format === 'html') {
      const out = registerFile(
        new Blob([html], { type: 'text/html;charset=utf-8' }),
        `${stem}.html`,
        'text/html;charset=utf-8',
      );
      return {
        task_id: uid('t'),
        module: 'markdown',
        operation: 'md2html',
        status: 'success',
        progress: 100,
        inputs: [{ name: filename || 'document.md', size: html.length, sha256: 'demo0000' }],
        output: out,
        error: null,
        created_at: now(),
        finished_at: now(),
        expire_at: new Date(Date.now() + 24 * 3600 * 1000).toISOString(),
      };
    }

    // PDF 导出走 Playwright 通道，需要排队
    const fakeFile = { name: filename || 'document.md', size: html.length };
    return createTask({
      module: 'markdown',
      operation: 'md2pdf',
      inputs: [fakeFile],
      produce: {
        outputs: async () => registerFile(
          buildPdf([
            'ToolKit · Markdown Export',
            '',
            `Document : ${filename || 'document.md'}`,
            `Rendered : ${new Date().toISOString()}`,
            '',
            'Demo mode: the real pipeline renders the full HTML via',
            'Playwright + Chromium, preserving tables, code blocks,',
            'math formulas and mermaid diagrams.',
          ]),
          `${stem}.pdf`,
          'application/pdf',
        ),
      },
    });
  },

  /** 任务列表 */
  async listTasks({ status, page = 1, pageSize = 50 } = {}) {
    let all = [...tasks.values()];
    if (status && status !== 'all') all = all.filter((t) => t.status === status);
    all.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    const start = (page - 1) * pageSize;
    return {
      total: all.length,
      page,
      page_size: pageSize,
      items: clone(all.slice(start, start + pageSize)),
    };
  },

  /** 任务详情 */
  async getTask(taskId) {
    const task = tasks.get(taskId);
    if (!task) {
      const err = new Error('任务不存在或已被清理');
      err.status = 404;
      throw err;
    }
    return clone(task);
  },

  /** 取消任务 */
  async cancelTask(taskId) {
    const task = tasks.get(taskId);
    if (!task) {
      const err = new Error('任务不存在');
      err.status = 404;
      throw err;
    }
    if (task.status === 'pending' || task.status === 'running') {
      task.status = 'cancelled';
      task.finished_at = now();
    }
    return clone(task);
  },

  /** 删除任务与产物 */
  async deleteTask(taskId) {
    const task = tasks.get(taskId);
    if (task?.output?.file_id) fileStore.delete(task.output.file_id);
    tasks.delete(taskId);
    return { ok: true };
  },

  /** 下载产物 */
  async download(fileId) {
    const file = fileStore.get(fileId);
    if (!file) {
      const err = new Error('文件不存在或已过期（默认保留 24 小时）');
      err.status = 404;
      throw err;
    }
    return file.blob;
  },

  /** 批量打包 */
  async batchZip(taskIds) {
    const list = taskIds.map((id) => tasks.get(id)).filter(Boolean);
    return buildTasksZip(list);
  },

  /* -------------------- SSH -------------------- */

  /** 建立会话（演示模式不做真实连接） */
  async sshConnect({ host, port, username, authMethod }) {
    if (!host || !username) {
      const err = new Error('主机地址与用户名不能为空');
      err.status = 400;
      throw err;
    }
    // 模拟握手耗时
    await new Promise((r) => setTimeout(r, 500));

    const sessionId = uid('ssh');
    sessions.set(sessionId, {
      sessionId, host, port: port || 22, username, authMethod,
      cols: 80, rows: 24,
      createdAt: now(),
    });
    return {
      session_id: sessionId,
      host, port: port || 22, username,
      host_key: 'SHA256:demoFingerprintAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
      server_banner: `SSH-2.0-OpenSSH_8.9p1 (演示模式)`,
    };
  },

  async sshDisconnect(sessionId) {
    sessions.delete(sessionId);
    return { ok: true };
  },

  /** 演示用伪 shell：支持少量常见命令，足以验证终端渲染、配色与中文显示 */
  createSshChannel(sessionId, { onData, onClose }) {
    const session = sessions.get(sessionId);
    if (!session) {
      onClose?.({ code: 1000, reason: '会话不存在' });
      return { send() {}, resize() {}, close() {} };
    }

    let cwd = `/home/${session.username}`;
    let buffer = '';

    const promptText = () => `\x1b[1;32m${session.username}@${session.host}\x1b[0m:\x1b[1;34m${cwd}\x1b[0m$ `;

    // 用定时器模拟网络往返延迟，让终端体验更真实
    const emit = (text, delay = 0) => setTimeout(() => onData?.(text), delay);

    emit(`\x1b[33m演示模式终端\x1b[0m —— 未建立真实 SSH 连接，仅用于验证 xterm.js 渲染、\r\n`);
    emit(`配色主题、中文 UTF-8 显示与窗口尺寸同步。\r\n`);
    emit(`输入 \x1b[1mhelp\x1b[0m 查看可用命令。\r\n\r\n`);
    emit(promptText(), 220);

    const COMMANDS = {
      help: () =>
        `可用命令（演示）：\r\n`
        + `  help        显示本帮助\r\n`
        + `  ls          列出目录内容\r\n`
        + `  pwd         显示当前目录\r\n`
        + `  whoami      显示当前用户\r\n`
        + `  uname -a    系统信息\r\n`
        + `  date        当前时间\r\n`
        + `  echo <text> 回显文本\r\n`
        + `  df -h       磁盘占用\r\n`
        + `  free -h     内存占用\r\n`
        + `  top         进程列表（静态快照）\r\n`
        + `  clear       清屏\r\n`
        + `  exit        断开会话\r\n`,

      ls: () =>
        `\x1b[1;34mdocuments\x1b[0m  \x1b[1;34mdownloads\x1b[0m  `
        + `\x1b[1;34mprojects\x1b[0m  \x1b[1;32mdeploy.sh\x1b[0m  `
        + `\x1b[1;32mnotes.md\x1b[0m  .bashrc\r\n`,

      pwd: () => `${cwd}\r\n`,

      whoami: () => `${session.username}\r\n`,

      'uname -a': () =>
        `Linux ${session.host} 6.1.0-13-amd64 #1 SMP PREEMPT_DYNAMIC x86_64 GNU/Linux\r\n`,

      date: () => `${new Date().toString()}\r\n`,

      'df -h': () =>
        `Filesystem      Size  Used Avail Use% Mounted on\r\n`
        + `/dev/vda1        80G   31G   45G  41% /\r\n`
        + `tmpfs           3.9G  1.2M  3.9G   1% /dev/shm\r\n`
        + `/dev/vdb1       200G   88G  102G  47% /data\r\n`,

      'free -h': () =>
        `               total        used        free      shared  buff/cache   available\r\n`
        + `Mem:           7.8Gi       2.9Gi       1.1Gi        18Mi       3.8Gi       4.6Gi\r\n`
        + `Swap:          2.0Gi       128Mi       1.9Gi\r\n`,

      top: () =>
        `top - ${new Date().toLocaleTimeString('en-GB')} up 42 days,  3:18,  2 users,  load average: 0.14, 0.09, 0.06\r\n`
        + `Tasks: 128 total,   1 running, 127 sleeping,   0 stopped,   0 zombie\r\n`
        + `%Cpu(s):  1.7 us,  0.6 sy,  0.0 ni, 97.3 id,  0.3 wa,  0.0 hi,  0.1 si\r\n`
        + `MiB Mem :   7982.4 total,   2976.1 free,   1128.4 used,   3877.9 buff/cache\r\n\r\n`
        + `  PID USER      PR  NI    VIRT    RES  %CPU  %MEM     TIME+ COMMAND\r\n`
        + ` 1284 root      20   0  902.1m  62.3m   2.3   0.8  18:42.31 python3\r\n`
        + `  892 root      20   0  724.5m  41.8m   1.1   0.5  33:07.55 nginx\r\n`
        + ` 1043 mysql     20   0 1288.7m 188.2m   0.7   2.4  71:23.88 mysqld\r\n`
        + ` 2210 www       20   0  601.2m  35.1m   0.3   0.4   4:11.02 node\r\n`,
    };

    const runCommand = (raw) => {
      const line = raw.trim();
      if (!line) return { out: '', close: false };

      if (line === 'clear') return { out: '\x1b[2J\x1b[H', clear: true };
      if (line === 'exit') return { out: 'logout\r\n', close: true };

      if (line.startsWith('echo ')) return { out: `${line.slice(5)}\r\n` };
      if (line.startsWith('cd')) {
        const target = line.slice(2).trim();
        if (!target || target === '~') cwd = `/home/${session.username}`;
        else if (target === '..') cwd = cwd.split('/').slice(0, -1).join('/') || '/';
        else if (target.startsWith('/')) cwd = target.replace(/\/+$/, '') || '/';
        else cwd = `${cwd}/${target}`.replace(/\/+/g, '/');
        return { out: '' };
      }

      const handler = COMMANDS[line] || COMMANDS[line.split(/\s+/)[0]];
      if (handler) return { out: handler() };

      return { out: `bash: ${line.split(/\s+/)[0]}: command not found（演示模式仅支持有限命令）\r\n` };
    };

    return {
      send(data) {
        // 云端返回的可能是单字符或整行，这里做最小行缓冲
        for (const ch of data) {
          if (ch === '\r' || ch === '\n') {
            const raw = buffer;
            buffer = '';
            onData?.('\r\n');
            const { out, close } = runCommand(raw);
            if (out) emit(out, 40);
            if (close) {
              setTimeout(() => onClose?.({ code: 1000, reason: '用户主动断开' }), 160);
              return;
            }
            emit(promptText(), 60);
          } else if (ch === '\u007f' || ch === '\b') {
            // 退格：本地回显处理
            if (buffer.length > 0) {
              buffer = buffer.slice(0, -1);
              onData?.('\b \b');
            }
          } else if (ch >= ' ') {
            buffer += ch;
            onData?.(ch);
          }
        }
      },
      resize(cols, rows) {
        const s = sessions.get(sessionId);
        if (s) { s.cols = cols; s.rows = rows; }
      },
      close() {
        sessions.delete(sessionId);
        onClose?.({ code: 1000, reason: '客户端关闭' });
      },
    };
  },

  /** 会话列表（演示模式返回内存中的会话，供顶栏会话标签使用） */
  async sshSessions() {
    return { items: [...sessions.values()] };
  },
};
