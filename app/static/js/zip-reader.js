/**
 * zip-reader.js — 极简 ZIP 读取器（仅解压，纯前端）
 *
 * 用途：Markdown 文档常把「.md + 图片」打包成 ZIP 一起分发（FR-MD-06），
 * 需要在浏览器本地解压出图片资源，才能正确渲染相对路径引用（FR-MD-10）。
 *
 * 实现范围：
 *   - 解析中央目录（EOCD → Central Directory → Local Header）
 *   - 支持 Store（method 0，无压缩）与 Deflate（method 8，经 DecompressionStream）
 *   - 不支持 Zip64（条目 > 4 GB 或 > 65535 个文件）与加密条目，遇到时明确报错
 *
 * 说明：Deflate 解压依赖原生 DecompressionStream('deflate-raw')，
 * Chrome 103+ / Firefox 113+ / Safari 16.4+ 可用；不支持时降级为
 * 只处理未压缩条目，并返回提示，而不是整体失败。
 */

const SIG_EOCD = 0x06054b50;      // End of Central Directory
const SIG_CENTRAL = 0x02014b50;   // Central Directory File Header
const SIG_LOCAL = 0x04034b50;     // Local File Header

/** 在尾部 64 KB 内反向搜索 EOCD 签名（注释字段最长 65535 字节） */
function findEocd(view) {
  const maxComment = 0xffff;
  const start = Math.max(0, view.byteLength - maxComment - 22);
  for (let i = view.byteLength - 22; i >= start; i -= 1) {
    if (view.getUint32(i, true) === SIG_EOCD) return i;
  }
  return -1;
}

/** 判断是否支持原生 raw deflate 解压 */
export const supportsDeflate = typeof DecompressionStream === 'function';

/**
 * 读取 ZIP 中的全部文件。
 * @param {File|Blob} file
 * @param {{textOnly?:boolean, onProgress?:(done:number,total:number)=>void, maxEntries?:number}} [opts]
 * @returns {Promise<Map<string, {blob:Blob, size:number, name:string}>>}
 */
export async function readZip(file, opts = {}) {
  const maxEntries = opts.maxEntries ?? 500;   // 防止恶意 ZIP 撑爆内存
  const buffer = await file.arrayBuffer();
  const view = new DataView(buffer);

  const eocdOffset = findEocd(view);
  if (eocdOffset < 0) {
    throw new Error('不是有效的 ZIP 文件（未找到中央目录结束标记）。若为 .rar / .7z 请先转换为 ZIP。');
  }

  const entryCount = view.getUint16(eocdOffset + 10, true);
  const centralSize = view.getUint32(eocdOffset + 12, true);
  const centralOffset = view.getUint32(eocdOffset + 16, true);

  if (entryCount === 0xffff || centralOffset === 0xffffffff) {
    throw new Error('该 ZIP 使用 Zip64 扩展格式，暂不支持。请改用普通 ZIP 打包。');
  }
  if (centralOffset + centralSize > view.byteLength) {
    throw new Error('ZIP 文件结构损坏（中央目录越界），无法解析。');
  }
  if (entryCount > maxEntries) {
    throw new Error(`ZIP 内条目过多（${entryCount} 个，上限 ${maxEntries}），请拆分后重试。`);
  }

  const decoder = new TextDecoder('utf-8');
  const entries = new Map();
  let cursor = centralOffset;

  for (let i = 0; i < entryCount; i += 1) {
    if (view.getUint32(cursor, true) !== SIG_CENTRAL) {
      throw new Error(`ZIP 中央目录第 ${i + 1} 条记录签名异常，文件可能已损坏。`);
    }

    const flags = view.getUint16(cursor + 8, true);
    const method = view.getUint16(cursor + 10, true);
    const compressedSize = view.getUint32(cursor + 20, true);
    const uncompressedSize = view.getUint32(cursor + 24, true);
    const nameLen = view.getUint16(cursor + 28, true);
    const extraLen = view.getUint16(cursor + 30, true);
    const commentLen = view.getUint16(cursor + 32, true);
    const localOffset = view.getUint32(cursor + 42, true);

    const nameBytes = new Uint8Array(buffer, cursor + 46, nameLen);
    const rawName = decoder.decode(nameBytes);

    cursor += 46 + nameLen + extraLen + commentLen;

    // 跳过目录条目与 Mac 打包产生的元数据文件
    if (rawName.endsWith('/')) continue;
    if (rawName.startsWith('__MACOSX/') || rawName.split('/').pop().startsWith('._')) continue;

    if (flags & 0x1) {
      // 加密条目无法在浏览器解密，跳过并记录
      entries.set(rawName, { name: rawName, error: '该条目已加密，无法解压', blob: null, size: 0 });
      continue;
    }

    if (view.getUint32(localOffset, true) !== SIG_LOCAL) continue;

    const localNameLen = view.getUint16(localOffset + 26, true);
    const localExtraLen = view.getUint16(localOffset + 28, true);
    const dataStart = localOffset + 30 + localNameLen + localExtraLen;

    // 部分工具写入的中央目录大小为 0（数据描述符模式），此时用本地头大小兜底
    const compSize = compressedSize || (uncompressedSize || 0);
    if (dataStart + compSize > view.byteLength) continue;

    const dataBytes = new Uint8Array(buffer, dataStart, compSize);

    let blob;
    if (method === 0) {
      blob = new Blob([dataBytes]);
    } else if (method === 8) {
      if (!supportsDeflate) {
        entries.set(rawName, {
          name: rawName, error: '当前浏览器不支持原生解压该条目（缺少 DecompressionStream）', blob: null, size: 0,
        });
        continue;
      }
      try {
        const stream = new Blob([dataBytes]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
        blob = await new Response(stream).blob();
      } catch (err) {
        entries.set(rawName, { name: rawName, error: `解压失败：${err.message}`, blob: null, size: 0 });
        continue;
      }
    } else {
      entries.set(rawName, { name: rawName, error: `不支持的压缩方式（method=${method}）`, blob: null, size: 0 });
      continue;
    }

    entries.set(rawName, { name: rawName, blob, size: blob.size });
    opts.onProgress?.(entries.size, entryCount);
  }

  return entries;
}

/**
 * 从 ZIP 中提取图片资源，返回「相对路径 → objectURL」映射。
 * Markdown 中的引用写法可能是 `img/a.png`、`./img/a.png`，也可能只是 `a.png`，
 * 因此同时登记「完整路径」与「文件名」两种键，命中率更高。
 * @param {Map} entries readZip 的结果
 * @returns {Map<string,string>} 相对路径 → objectURL
 */
export function extractImages(entries) {
  const assets = new Map();
  const IMAGE_RE = /\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/i;
  const created = [];

  for (const entry of entries.values()) {
    if (!entry.blob || !IMAGE_RE.test(entry.name)) continue;

    const mime = guessMime(entry.name);
    const typed = entry.blob.type === mime ? entry.blob : new Blob([entry.blob], { type: mime });
    const url = URL.createObjectURL(typed);
    created.push(url);

    assets.set(entry.name, url);
    const base = entry.name.split('/').pop();
    // 仅当文件名唯一时才登记短键，避免同名文件互相覆盖
    if (!assets.has(base)) assets.set(base, url);
    else if (assets.get(base) !== url) assets.set(base, null);
  }

  // 清理已被覆盖的歧义短键
  for (const [key, value] of [...assets.entries()]) {
    if (value === null) assets.delete(key);
  }

  assets.revokeAll = () => created.forEach((u) => { try { URL.revokeObjectURL(u); } catch { /* 忽略 */ } });
  return assets;
}

function guessMime(name) {
  const ext = name.split('.').pop().toLowerCase();
  const map = {
    png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif',
    webp: 'image/webp', svg: 'image/svg+xml', avif: 'image/avif',
    bmp: 'image/bmp', ico: 'image/x-icon',
  };
  return map[ext] || 'application/octet-stream';
}

/** 列出 ZIP 内所有条目名（用于向用户反馈解压结果） */
export function listEntries(entries) {
  return [...entries.values()].map((e) => ({
    name: e.name,
    size: e.size,
    error: e.error || null,
  }));
}
