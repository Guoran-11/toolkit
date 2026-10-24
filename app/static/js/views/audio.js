/**
 * views/audio.js — 模块三：音频格式转换
 *
 * 覆盖需求：FR-AU-01 ~ FR-AU-08
 *   - 文件头魔数嗅探（不信任扩展名）
 *   - 平台专有格式解封装 → 通用容器
 *   - 输出格式与编码参数可选
 *   - 保留封面与 ID3 标签（可选）
 *   - 批量转换 + ZIP 打包
 *
 * 合规约束（§5.3.4，不可协商）：
 *   NFR-AU-01 不提供任何平台的下载 / 抓取 / 搜索能力
 *   NFR-AU-02 仅处理用户本地已有的文件，全程本地闭环
 *   NFR-AU-03 不实现任何绕过付费 / 破解授权 / 解除 DRM 的功能
 *   NFR-AU-04 页面显著位置展示免责声明，首次使用需勾选确认
 *   NFR-AU-05 不提供批量分发 / 分享 / 云盘转存能力
 */

import { el, clear, $, formatBytes, extOf, validateFile, formatDuration, sniffMagic } from '../utils.js';
import { icon } from '../icons.js';
import { CFG, runtime } from '../config.js';
import { audioApi, api, ApiError, pollTask } from '../api.js';
import {
  toast, createDropzone, fileItem, createProgress, statusPill, emptyState,
  confirmDialog, tag, modal,
} from '../ui.js';
import { upsertTask, getTasks, loadAudioGate, setAudioGate } from '../store.js';

/* ------------------------------------------------------------------ *
 * 常量
 * ------------------------------------------------------------------ */
/** 输出格式（FR-AU-03） */
const OUTPUT_FORMATS = [
  { id: 'mp4',  label: 'MP4',  desc: 'AAC 音频轨道容器，兼容性广', badge: '推荐' },
  { id: 'mp3',  label: 'MP3',  desc: '最通用的音频格式，全设备支持' },
  { id: 'm4a',  label: 'M4A',  desc: 'AAC 封装，体积小音质好' },
  { id: 'flac', label: 'FLAC', desc: '无损压缩，音质零损失' },
  { id: 'wav',  label: 'WAV',  desc: '未压缩原始音频，体积最大' },
  { id: 'ogg',  label: 'OGG',  desc: '开源有损格式，适合网页播放' },
];

/** 有损格式才需要比特率 */
const LOSSY_FORMATS = ['mp4', 'mp3', 'm4a', 'ogg'];

const BITRATES = [
  { value: '128', label: '128 kbps（省空间）' },
  { value: '192', label: '192 kbps' },
  { value: '256', label: '256 kbps（推荐）' },
  { value: '320', label: '320 kbps（最高）' },
];

/** 平台专有格式（§5.3.2）——仅用于嗅探识别与提示，不提供任何下载能力 */
const PROPRIETARY = {
  ncm:    { platform: '网易云音乐', level: 'M' },
  qmc0:   { platform: 'QQ 音乐', level: 'M' },
  qmc3:   { platform: 'QQ 音乐', level: 'M' },
  qmcflac:{ platform: 'QQ 音乐', level: 'M' },
  qmcogg: { platform: 'QQ 音乐', level: 'M' },
  mgg:    { platform: 'QQ 音乐', level: 'M' },
  mflac:  { platform: 'QQ 音乐', level: 'M' },
  kgm:    { platform: '酷狗音乐', level: 'S' },
  vpr:    { platform: '酷狗音乐', level: 'S' },
  kwm:    { platform: '酷我音乐', level: 'S' },
  mg3d:   { platform: '咪咕音乐', level: 'C' },
};

const COMMON_AUDIO = ['mp3', 'flac', 'wav', 'm4a', 'aac', 'ogg', 'opus', 'wma', 'ape'];

/* ------------------------------------------------------------------ *
 * 视图工厂
 * ------------------------------------------------------------------ */
export default async function createAudioView(container) {
  /* ---------------- 状态 ---------------- */
  const state = {
    files: [],
    probes: new Map(),     // fileName → 嗅探结果
    probing: false,
    outputFormat: 'mp4',
    bitrate: '256',
    sampleRate: 'original',
    channels: 'original',
    keepMetadata: true,
    jobs: [],
    uploading: false,
  };

  let abortController = null;
  const disposers = [];

  // 用 classList 增删而非赋值 className：路由依赖容器上的 `is-active` 控制可见性
  container.classList.remove('view--single');
  container.classList.add('view', 'view--split');
  container.innerHTML = '';

  /* ================= 左面板 ================= */
  const gateHost = el('div');
  const workbench = el('div.col.gap-5');

  const dropzone = createDropzone({
    iconName: 'music',
    title: '拖拽音频文件到此处，或点击选择',
    hint: `支持 mp3 / flac / wav / m4a / ogg / aac，以及 NCM / QMC / KGM 等本地专有格式；单文件不超过 ${CFG.limits.maxAudioUploadMb} MB`,
    inputAccept: [...COMMON_AUDIO, ...Object.keys(PROPRIETARY)].map((e) => `.${e}`).join(','),
    onFiles: (files) => addFiles(files),
  });

  const fileListEl = el('div.file-list');
  const probeHost = el('div.col.gap-2');

  /* ---- 输出格式 ---- */
  const formatGrid = el('div.format-picker');
  const paramHost = el('div.col.gap-3');

  const convertBtn = el('button.btn.btn--primary.btn--block.btn--lg', {
    type: 'button',
    on: { click: () => submit() },
  }, [icon('wave'), el('span', { text: '开始转换' })]);

  const progressHost = el('div.col.gap-2');

  // 合规声明（常驻显示，不因已勾选而消失 —— NFR-AU-04 要求"显著位置"）
  const complianceNotice = el('div.alert.alert--warning', {}, [
    icon('shield'),
    el('div.alert-body', {}, [
      el('div.alert-title', { text: '使用边界' }),
      el('div', {
        text: '本工具仅对您本机已有的音频文件做格式转换，全程在本地服务完成，不上传任何第三方。'
          + '本工具不提供、也不允许用于：从音乐平台下载或抓取音频、绕过付费、破解 DRM 授权、批量转存与分发。',
      }),
    ]),
  ]);

  workbench.append(
    el('div.form-section', {}, [
      el('div.form-section-title', {}, [icon('upload'), el('span', { text: '音频文件' })]),
      dropzone,
      fileListEl,
      probeHost,
    ]),
    el('div.form-section', {}, [
      el('div.form-section-title', {}, [icon('package'), el('span', { text: '输出格式' })]),
      formatGrid,
    ]),
    el('div.form-section#audio-params', {}, [
      el('div.form-section-title', {}, [icon('sliders'), el('span', { text: '编码参数' })]),
      paramHost,
    ]),
    complianceNotice,
  );

  const leftPanel = el('div.panel.panel--left', {}, [
    el('div.panel-header', {}, [
      el('h2', {}, [icon('music'), el('span', { text: '音频工具' })]),
      el('div.panel-actions', {}, [
        el('button.btn.btn--sm.btn--ghost', {
          type: 'button',
          on: { click: () => showFormatHelp() },
        }, [icon('listTree'), el('span', { text: '支持格式' })]),
      ]),
    ]),
    el('div.panel-body.panel-body--grid', {}, [gateHost, workbench]),
    el('div.panel-footer.col.gap-3', {}, [
      progressHost,
      convertBtn,
    ]),
  ]);

  /* ---- 分隔条 ---- */
  const splitter = el('div.splitter', {
    role: 'separator',
    tabindex: '0',
    'aria-orientation': 'vertical',
    'aria-label': '调整面板宽度（使用左右方向键）',
    'aria-valuemin': '320',
    'aria-valuemax': '620',
    'aria-valuenow': '420',
  });

  /* ---- 右面板 ---- */
  const resultBody = el('div.panel-body.panel-body--flush.col');
  const resultActions = el('div.panel-actions');

  const rightPanel = el('div.panel.panel--right', {}, [
    el('div.panel-header', {}, [
      el('h2', {}, [icon('play'), el('span', { text: '转换结果' })]),
      resultActions,
    ]),
    resultBody,
  ]);

  container.append(leftPanel, splitter, rightPanel);

  /* ================= 合规闸门 ================= */
  function renderGate() {
    clear(gateHost);
    if (loadAudioGate()) {
      workbench.classList.remove('hidden');
      return;
    }
    workbench.classList.add('hidden');

    const acceptCheck = el('input', { type: 'checkbox', id: 'audio-gate-accept' });
    const acceptBtn = el('button.btn.btn--primary', {
      type: 'button',
      disabled: true,
      on: {
        click: () => {
          setAudioGate(true);
          renderGate();
          renderResults();
          toast('success', '已确认使用声明', '现在可以开始转换本地音频文件了');
          dropzone.focus?.();
        },
      },
    }, [icon('check'), el('span', { text: '确认并继续' })]);

    acceptCheck.addEventListener('change', () => { acceptBtn.disabled = !acceptCheck.checked; });

    gateHost.append(el('div.gate', { role: 'region', 'aria-label': '使用声明' }, [
      el('div.gate-title', {}, [icon('alertTriangle'), el('span', { text: '使用前请确认' })]),
      // 注意：不要在这里写 Markdown 的 ** 标记——text 是纯文本节点，星号会原样显示。
      // 需要强调时用 <strong> 元素包裹。
      el('ul.gate-list', {}, [
        el('li', {}, [
          el('span', { text: '本模块仅处理您' }),
          el('strong', { text: '本机已有的、合法获得的' }),
          el('span', { text: '音频文件，转换全程在本地完成，不上传任何第三方服务。' }),
        ]),
        el('li', {}, [
          el('span', { text: '本模块' }),
          el('strong', { text: '不提供' }),
          el('span', { text: '从任何音乐平台下载、搜索、抓取音频的能力。' }),
        ]),
        el('li', {}, [
          el('span', { text: '本模块' }),
          el('strong', { text: '不实现' }),
          el('span', { text: '任何绕过付费墙、破解授权、解除 DRM 保护的功能。对于受严格 DRM 保护、本地无可解密载荷的文件，将明确返回「不支持」而不尝试破解。' }),
        ]),
        el('li', { text: '输出文件仅供您个人合理使用；本模块不提供批量分发、分享或云盘转存能力。' }),
        el('li', { text: '请自行确保您的使用行为符合所在地法律法规及相关平台的用户协议。' }),
      ]),
      el('label.check', { for: 'audio-gate-accept' }, [
        acceptCheck,
        el('span', { text: '我已阅读并理解上述声明，承诺仅将本工具用于处理本人合法拥有的本地文件' }),
      ]),
      el('div.gate-actions', {}, [
        el('span.t-xs.t-muted', { text: '该确认仅保存在本机浏览器，可随时清除站点数据重置' }),
        acceptBtn,
      ]),
    ]));
  }

  /* ================= 渲染：输出格式 ================= */
  function renderFormats() {
    clear(formatGrid);
    for (const fmt of OUTPUT_FORMATS) {
      formatGrid.append(el('button.format-option', {
        type: 'button',
        'aria-pressed': String(state.outputFormat === fmt.id),
        'data-tip': fmt.desc,
        on: {
          click: () => {
            state.outputFormat = fmt.id;
            renderFormats();
            renderParams();
          },
        },
      }, [
        el('span.format-ext', { text: fmt.label }),
        fmt.badge ? el('span.format-grade', { text: fmt.badge }) : el('span.format-grade', { text: '' }),
      ]));
    }
  }

  /* ================= 渲染：编码参数 ================= */
  function renderParams() {
    clear(paramHost);
    const paramsHost = $('#audio-params', container);

    const items = [];

    // 比特率：仅对无损格式隐藏
    if (LOSSY_FORMATS.includes(state.outputFormat)) {
      const sel = el('select.select', { id: 'audio-bitrate' });
      for (const b of BITRATES) {
        sel.append(el('option', { value: b.value, text: b.label, selected: state.bitrate === b.value }));
      }
      sel.addEventListener('change', () => { state.bitrate = sel.value; });
      items.push(el('div.field', {}, [
        el('label.label', { for: 'audio-bitrate', text: '比特率' }),
        sel,
        el('div.help', { text: '越高音质越好、文件越大。320 kbps 已达到有损编码的听感上限。' }),
      ]));
    } else {
      items.push(el('div.alert.alert--info', {}, [
        icon('info'),
        el('div.alert-body', {}, [el('div', { text: `${state.outputFormat.toUpperCase()} 为无损/未压缩格式，不需要设置比特率。` })]),
      ]));
    }

    // 采样率
    const srSel = el('select.select', { id: 'audio-samplerate' });
    for (const opt of [
      { value: 'original', label: '保持原始采样率（推荐）' },
      { value: '44100', label: '44.1 kHz（CD 标准）' },
      { value: '48000', label: '48 kHz（视频标准）' },
      { value: '96000', label: '96 kHz（高解析）' },
    ]) {
      srSel.append(el('option', { value: opt.value, text: opt.label, selected: state.sampleRate === opt.value }));
    }
    srSel.addEventListener('change', () => { state.sampleRate = srSel.value; });

    // 声道
    const chSel = el('select.select', { id: 'audio-channels' });
    for (const opt of [
      { value: 'original', label: '保持原始声道' },
      { value: '2', label: '立体声（2 声道）' },
      { value: '1', label: '单声道（音质换取体积）' },
    ]) {
      chSel.append(el('option', { value: opt.value, text: opt.label, selected: state.channels === opt.value }));
    }
    chSel.addEventListener('change', () => { state.channels = chSel.value; });

    items.push(el('div.field-row', {}, [
      el('div.field', {}, [el('label.label', { for: 'audio-samplerate', text: '采样率' }), srSel]),
      el('div.field', {}, [el('label.label', { for: 'audio-channels', text: '声道' }), chSel]),
    ]));

    // 元数据
    const metaCheck = el('input', { type: 'checkbox', id: 'audio-keep-meta', checked: state.keepMetadata });
    metaCheck.addEventListener('change', () => { state.keepMetadata = metaCheck.checked; });
    items.push(el('div.field', {}, [
      el('label.check', { for: 'audio-keep-meta' }, [
        metaCheck,
        el('span', { text: '保留封面与 ID3 标签（标题 / 艺术家 / 专辑）' }),
      ]),
      el('div.help', { text: '关闭后输出文件更小，但播放器中将不显示歌曲信息与专辑封面。' }),
    ]));

    // PDF 之外，音频模块不涉及 OCR 等重依赖，参数区较短，此处直接展开
    paramsHost?.classList.remove('hidden');
    for (const item of items) paramHost.append(item);
  }

  /* ================= 文件处理 ================= */
  async function addFiles(incoming) {
    if (!loadAudioGate()) {
      toast('warning', '请先确认使用声明', '阅读并勾选左侧顶部声明后即可添加文件');
      return;
    }

    const errors = [];
    for (const file of incoming) {
      const check = validateFile(file, { maxMb: CFG.limits.maxAudioUploadMb });
      if (!check.ok) { errors.push(`${file.name}：${check.error}`); continue; }

      const ext = extOf(file.name);
      const known = COMMON_AUDIO.includes(ext) || Object.prototype.hasOwnProperty.call(PROPRIETARY, ext);
      if (!known) {
        errors.push(`${file.name}：不认识的扩展名 .${ext || '(无)'}，可能是压缩包或非音频文件`);
        continue;
      }
      if (state.files.length >= CFG.limits.maxBatchFiles) {
        errors.push(`已达批量上限 ${CFG.limits.maxBatchFiles} 个`);
        break;
      }
      if (state.files.some((f) => f.name === file.name && f.size === file.size)) {
        errors.push(`${file.name}：重复文件已跳过`);
        continue;
      }
      state.files.push(file);
    }

    if (errors.length > 0) {
      toast('warning', `部分文件未加入（${errors.length} 项）`, errors.slice(0, 3).join('；'));
    }

    renderFiles();
    await probeFiles();
    updateSubmitState();
  }

  function renderFiles() {
    clear(fileListEl);
    state.files.forEach((file, index) => {
      const probe = state.probes.get(file.name);
      fileListEl.append(fileItem(file, {
        status: probe?.decryptable === false ? 'error' : probe?.decryptable ? 'ok' : 'pending',
        message: probe ? probe.detected_label : '',
        onRemove: () => {
          state.files.splice(index, 1);
          state.probes.delete(file.name);
          renderFiles();
          renderProbeResults();
          updateSubmitState();
        },
      }));
    });
  }

  /* ================= 格式嗅探（FR-AU-01） ================= */
  async function probeFiles() {
    if (state.files.length === 0) { renderProbeResults(); return; }

    state.probing = true;
    clear(probeHost);
    probeHost.append(el('div.row.gap-2.t-sm.t-muted', {}, [
      icon('activity'),
      el('span', { text: '正在按文件头嗅探格式…' }),
    ]));

    try {
      const data = await audioApi.probe(state.files);
      state.probes.clear();
      for (const item of data?.results || []) {
        state.probes.set(item.name, item);
      }
    } catch (err) {
      // 后端嗅探不可用时，前端用本地魔数兜底，保证功能不中断
      for (const file of state.files) {
        try {
          const head = new Uint8Array(await file.slice(0, 32).arrayBuffer());
          const sniffed = sniffMagic(head);
          const ext = extOf(file.name);
          state.probes.set(file.name, {
            name: file.name,
            detected_type: sniffed?.type || ext,
            detected_label: sniffed?.label || `.${ext} 文件（本地兜底识别）`,
            decryptable: Object.prototype.hasOwnProperty.call(PROPRIETARY, ext) || Boolean(sniffed),
            note: err?.message ? `服务端嗅探不可用，已使用本地识别：${err.message}` : '',
          });
        } catch {
          state.probes.set(file.name, {
            name: file.name, detected_type: 'unknown', detected_label: '无法识别',
            decryptable: false, note: '读取文件头失败',
          });
        }
      }
    } finally {
      state.probing = false;
      renderFiles();
      renderProbeResults();
    }
  }

  function renderProbeResults() {
    clear(probeHost);
    if (state.files.length === 0) return;

    for (const file of state.files) {
      const probe = state.probes.get(file.name);
      if (!probe) continue;

      const ext = extOf(file.name);
      const prop = PROPRIETARY[ext];

      probeHost.append(el('div.probe-result', {}, [
        el('div.probe-badge', {
          'data-decryptable': String(Boolean(probe.decryptable)),
          text: prop ? ext.slice(0, 4) : (probe.detected_type || '?').slice(0, 4),
        }),
        el('div.col.gap-1.grow', {}, [
          el('div.row.gap-2.wrap', {}, [
            el('span.t-clamp-1', { text: file.name, title: file.name, style: { fontWeight: 'var(--fw-medium)' } }),
            prop ? tag(prop.platform, 'accent') : null,
            probe.decryptable
              ? tag('可转换', 'success')
              : tag('不支持', 'danger'),
          ]),
          el('div.t-xs.t-muted', { text: probe.detected_label + (prop ? ` · 扩展名 .${ext}` : '') }),
          !probe.decryptable
            ? el('div.t-xs.t-danger', {
                text: '该文件本地不存在可解密载荷（可能为受 DRM 严格保护的格式或文件已损坏），本工具不会尝试破解，请更换文件。',
              })
            : null,
          probe.note ? el('div.t-xs.t-muted', { text: probe.note }) : null,
        ]),
      ]));
    }

    // 不可转换文件存在时给出明确的阻断提示
    const blocked = [...state.probes.values()].filter((p) => !p.decryptable).length;
    if (blocked > 0) {
      probeHost.append(el('div.alert.alert--danger', { role: 'alert' }, [
        icon('alertCircle'),
        el('div.alert-body', {}, [
          el('div.alert-title', { text: `有 ${blocked} 个文件无法转换` }),
          el('div', { text: '这些文件缺少可解密的音频载荷。按产品合规红线，本工具不会尝试任何破解手段，请移除后再开始转换。' }),
        ]),
      ]));
    }
  }

  function updateSubmitState() {
    const blocked = [...state.probes.values()].filter((p) => !p.decryptable).length;
    const ready = state.files.length > 0 && blocked === 0 && !state.uploading && !state.probing;
    convertBtn.disabled = !ready;
    convertBtn.classList.toggle('is-loading', state.uploading);
    const label = convertBtn.querySelector('span');
    if (label && !state.uploading) {
      label.textContent = state.files.length > 1
        ? `开始转换（${state.files.length} 个文件）`
        : '开始转换';
    } else if (label) {
      label.textContent = '处理中…';
    }
  }

  /* ================= 提交 ================= */
  async function submit() {
    if (state.uploading) return;
    if (state.files.length === 0) {
      toast('warning', '请先添加音频文件');
      dropzone.focus?.();
      return;
    }
    const blocked = [...state.probes.values()].filter((p) => !p.decryptable);
    if (blocked.length > 0) {
      toast('danger', '存在无法转换的文件', `${blocked.map((b) => b.name).join('、')} 缺少可解密载荷，请先移除`);
      return;
    }

    if (runtime.demo) {
      toast('info', '当前为演示模式', '尚未连接 Python 后端，将生成可播放的合成音频用于验证播放器链路');
    }

    state.uploading = true;
    state.jobs = [];
    updateSubmitState();
    renderResults();

    clear(progressHost);
    const uploadProgress = createProgress();
    const wrap = el('div.col.gap-2', {}, [
      el('div.row.between', {}, [
        el('span.t-sm.t-muted', { text: '上传中…' }),
        el('span.t-sm.t-mono', { text: '0%' }),
      ]),
      uploadProgress,
    ]);
    progressHost.append(wrap);

    abortController = new AbortController();

    try {
      const created = await audioApi.convert(state.files, {
        outputFormat: state.outputFormat,
        bitrate: state.bitrate,
        sampleRate: state.sampleRate,
        channels: state.channels,
        keepMetadata: state.keepMetadata,
      }, (loaded, total, percent) => {
        uploadProgress.setValue(percent);
        const pct = wrap.querySelector('.t-mono');
        if (pct) pct.textContent = `${percent}%`;
      });

      wrap.remove();
      renderProgressArea();

      for (const task of created) {
        upsertTask(task);
        state.jobs.push({
          id: task.task_id,
          name: task.inputs?.[0]?.name || '未知文件',
          status: task.status,
          progress: task.progress || 0,
          error: null,
          output: null,
        });
      }
      renderResults();

      const results = await Promise.allSettled(created.map((task) => pollTask(task.task_id, {
        signal: abortController.signal,
        onUpdate: (t) => {
          upsertTask(t);
          const job = state.jobs.find((j) => j.id === t.task_id);
          if (job) {
            job.status = t.status;
            job.progress = t.progress;
            job.error = t.error;
            job.output = t.output;
          }
          renderResults();
          renderProgressArea();
        },
      })));

      const ok = results.filter((r) => r.status === 'fulfilled' && r.value.status === 'success').length;
      const failed = results.length - ok;
      if (failed === 0) toast('success', `转换完成（${ok} 个文件）`, '可在右侧试听并下载');
      else if (ok === 0) toast('danger', '全部转换失败', '请查看失败原因');
      else toast('warning', `部分成功（成功 ${ok} / 失败 ${failed}）`, '失败项已标注原因');
      renderResults();
    } catch (err) {
      if (err?.code !== 'ABORTED') {
        toast('danger', '转换请求失败', err instanceof ApiError ? err.message : String(err?.message || err));
      }
    } finally {
      state.uploading = false;
      abortController = null;
      updateSubmitState();
      renderProgressArea();
    }
  }

  function renderProgressArea() {
    clear(progressHost);
    const active = state.jobs.filter((j) => j.status === 'pending' || j.status === 'running');
    if (active.length === 0) return;

    const done = state.jobs.length - active.length;
    const overall = Math.round(((done + active.reduce((s, j) => s + j.progress / 100, 0)) / state.jobs.length) * 100);

    const bar = createProgress();
    bar.setValue(overall);
    bar.setLabel('音频转换总进度');

    progressHost.append(el('div.col.gap-2', {}, [
      el('div.row.between', {}, [
        el('span.t-sm.t-muted', { text: `转换中 ${done}/${state.jobs.length}` }),
        el('span.t-sm.t-mono', { text: `${overall}%` }),
      ]),
      bar,
      el('button.btn.btn--sm.btn--danger-ghost', {
        type: 'button',
        on: {
          click: async () => {
            abortController?.abort();
            await Promise.allSettled(state.jobs
              .filter((j) => j.status === 'pending' || j.status === 'running')
              .map((j) => api.tasks.cancel(j.id)));
            toast('info', '已请求取消全部任务');
          },
        },
      }, [icon('stop'), el('span', { text: '取消全部' })]),
    ]));
  }

  /* ================= 结果区 ================= */
  function renderResults() {
    clear(resultBody);
    clear(resultActions);

    if (!loadAudioGate()) {
      resultBody.append(emptyState({
        iconName: 'shield',
        title: '需先确认使用声明',
        desc: '音频模块涉及版权合规要求，请先在左侧阅读并勾选确认，然后即可开始转换本地音频文件。',
      }));
      return;
    }

    if (state.jobs.length === 0) {
      resultBody.append(emptyState({
        iconName: 'music',
        title: '暂无转换结果',
        desc: '添加本地音频文件后点击「开始转换」。转换完成后可在此试听效果并下载，支持批量打包为 ZIP。',
      }));
      return;
    }

    const succeeded = state.jobs.filter((j) => j.status === 'success' && j.output);

    if (succeeded.length > 1) {
      resultActions.append(el('button.btn.btn--sm', {
        type: 'button',
        on: { click: () => downloadZip() },
      }, [icon('archive'), el('span', { text: '打包下载 ZIP' })]));
    }
    resultActions.append(el('button.btn.btn--sm.btn--icon.btn--ghost', {
      type: 'button',
      'aria-label': '清空结果列表',
      'data-tip': '清空结果列表',
      on: {
        click: async () => {
          if (!(await confirmDialog({
            title: '清空结果列表',
            message: '仅从页面移除记录，服务端产物仍保留至过期时间。',
            confirmLabel: '清空',
          }))) return;
          state.jobs = [];
          renderResults();
        },
      },
    }, [icon('trash')]));

    const wrap = el('div.col.gap-4', { style: { padding: 'var(--sp-4)' } });

    for (const job of state.jobs) {
      const card = el('div.audio-player', { 'data-job': job.id });

      const head = el('div.row.gap-3.wrap', {}, [
        el('div.col.gap-1.grow', {}, [
          el('div.row.gap-2.wrap', {}, [
            el('span.t-clamp-1', { text: job.name, title: job.name, style: { fontWeight: 'var(--fw-medium)' } }),
            statusPill(job.status),
            job.output ? tag(extOf(job.output.name).toUpperCase(), 'mono') : null,
            job.output ? tag(formatBytes(job.output.size), 'mono') : null,
          ]),
          el('div.t-xs.t-muted', {
            text: `${state.outputFormat.toUpperCase()} · ${LOSSY_FORMATS.includes(state.outputFormat) ? state.bitrate + ' kbps' : '无损'} · ${state.keepMetadata ? '保留元数据' : '不含元数据'}`,
          }),
        ]),
      ]);
      card.append(head);

      if (job.status === 'pending' || job.status === 'running') {
        const bar = createProgress({ size: 'sm' });
        if (job.status === 'pending') bar.setIndeterminate();
        else bar.setValue(job.progress);
        bar.setLabel(`${job.name} 转换进度`);
        card.append(bar);
        card.append(el('div.t-xs.t-muted', {
          text: job.status === 'pending' ? '排队等待执行…' : `转换中 ${job.progress}%`,
        }));
      }

      if (job.status === 'failed') {
        card.append(el('div.alert.alert--danger', {}, [
          icon('alertCircle'),
          el('div.alert-body', {}, [
            el('div.alert-title', { text: '转换失败' }),
            el('div', { text: job.error || '未知原因' }),
          ]),
        ]));
      }

      if (job.status === 'success' && job.output) {
        const url = api.fileUrl(job.output.file_id);

        // 简易波形示意（FR-AU-08，C 级优先级：先用静态条形表示，不引入 canvas 依赖）
        const wave = el('div.waveform', { 'aria-hidden': 'true' });
        for (let i = 0; i < 48; i += 1) {
          const h = 18 + Math.round(Math.abs(Math.sin(i * 0.7)) * 34);
          wave.append(el('div.waveform-bar', { style: { height: `${h}%` } }));
        }
        card.append(wave);

        const audio = el('audio', {
          controls: true,
          preload: 'metadata',
          src: url,
          'aria-label': `${job.output.name} 试听`,
        });
        card.append(audio);
        audio.addEventListener('error', () => {
          card.append(el('div.t-xs.t-danger', { text: '浏览器无法播放该文件（格式不受支持或链接已过期）' }));
        });
        // 播放时点亮波形条形，制造"正在播放"的视觉反馈
        audio.addEventListener('play', () => wave.querySelectorAll('.waveform-bar').forEach((b, i) => {
          setTimeout(() => b.classList.add('is-played'), i * 18);
        }));
        audio.addEventListener('ended', () => wave.querySelectorAll('.waveform-bar').forEach((b) => b.classList.remove('is-played')));
        audio.addEventListener('loadedmetadata', () => {
          const dur = card.querySelector('[data-duration]');
          if (dur && Number.isFinite(audio.duration)) dur.textContent = `时长 ${formatDuration(audio.duration)}`;
        });

        card.append(el('div.row.between.wrap.gap-2', {}, [
          el('div.t-xs.t-muted', { 'data-duration': '', text: '时长读取中…' }),
          el('div.row.gap-1', {}, [
            el('button.btn.btn--sm.btn--icon', {
              type: 'button',
              'aria-label': `下载 ${job.output.name}`,
              'data-tip': '下载',
              on: { click: () => downloadOne(job) },
            }, [icon('download')]),
            el('button.btn.btn--sm.btn--icon.btn--ghost', {
              type: 'button',
              'aria-label': '从列表移除',
              'data-tip': '移除记录',
              on: {
                click: () => {
                  try { URL.revokeObjectURL(url); } catch { /* 忽略 */ }
                  state.jobs = state.jobs.filter((j) => j.id !== job.id);
                  renderResults();
                },
              },
            }, [icon('trash')]),
          ]),
        ]));
      }

      wrap.append(card);
    }

    resultBody.append(wrap);
    resultBody.append(el('div', { style: { padding: '0 var(--sp-4) var(--sp-4)' } }, [
      el('div.help', {
        text: '输出文件默认保留 24 小时。请仅在法律与平台协议允许的范围内使用转换结果。',
      }),
    ]));
  }

  async function downloadOne(job) {
    if (!job.output) return;
    try {
      await api.download(job.output.file_id, job.output.name);
      toast('success', '已开始下载', job.output.name);
    } catch (err) {
      toast('danger', '下载失败', err?.message || '文件可能已过期');
    }
  }

  async function downloadZip() {
    const ids = state.jobs.filter((j) => j.status === 'success').map((j) => j.id);
    if (ids.length === 0) return;
    try {
      await api.batchZip(ids, `audio-${ids.length}files.zip`);
      toast('success', '打包下载已开始', `包含 ${ids.length} 个音频文件`);
    } catch (err) {
      toast('danger', '打包失败', err?.message || '请稍后重试');
    }
  }

  /* ================= 支持格式说明 ================= */
  function showFormatHelp() {
    const body = el('div.col.gap-4');

    const commonTable = el('table.table');
    commonTable.append(
      el('thead', {}, [el('tr', {}, [
        el('th', { text: '通用格式' }), el('th', { text: '说明' }), el('th', { text: '处理方式' }),
      ])]),
      el('tbody', {}, [
        ['mp3', '最常见的 MP3 音频', 'FFmpeg 直接转码'],
        ['flac', '无损压缩音频', 'FFmpeg 直接转码'],
        ['wav', '未压缩波形音频', 'FFmpeg 直接转码'],
        ['m4a / aac', 'AAC 音频', 'FFmpeg 直接转码'],
        ['ogg / opus', '开源有损格式', 'FFmpeg 直接转码'],
        ['wma / ape', 'Windows / Monkey 音频', 'FFmpeg 直接转码'],
      ].map((row) => el('tr', {}, row.map((c) => el('td', { text: c }))))),
    );

    const propTable = el('table.table');
    propTable.append(
      el('thead', {}, [el('tr', {}, [
        el('th', { text: '平台' }), el('th', { text: '专有格式' }), el('th', { text: '优先级' }),
      ])]),
      el('tbody', {}, Object.entries(PROPRIETARY).map(([ext, info]) => el('tr', {}, [
        el('td', { text: info.platform }),
        el('td.t-mono', { text: `.${ext}` }),
        el('td', {}, [tag(info.level === 'M' ? '本期支持' : info.level === 'S' ? '计划支持' : '可选', info.level === 'M' ? 'success' : info.level === 'S' ? 'accent' : '')]),
      ]))),
    );

    body.append(
      el('div.alert.alert--accent', {}, [
        icon('info'),
        el('div.alert-body', {}, [
          el('div.alert-title', { text: '识别依据是文件头，不是扩展名' }),
          el('div', { text: '格式判定读取文件开头的魔数（magic bytes），因此把 .ncm 改名成 .mp3 也能被正确识别。' }),
        ]),
      ]),
      el('div.form-section-title', { text: '通用音频格式' }),
      el('div.table-wrap', {}, [commonTable]),
      el('div.form-section-title', { text: '平台专有格式（仅解封装本地文件）' }),
      el('div.table-wrap', {}, [propTable]),
      el('div.alert.alert--warning', {}, [
        icon('shield'),
        el('div.alert-body', {}, [
          el('div.alert-title', { text: '合规红线' }),
          el('div', {
            text: '本工具仅对您本机已有的文件执行格式解封装与转码，不提供任何下载、抓取、搜索能力，'
              + '也不实现绕过付费或解除 DRM 的功能。对本地无可解密载荷的文件会明确返回「不支持」。',
          }),
        ]),
      ]),
    );

    modal({ title: '音频支持格式', size: 'wide', body, actions: [{ label: '关闭', variant: 'secondary' }] });
  }

  /* ================= 初始化 ================= */
  renderGate();
  renderFormats();
  renderParams();
  renderResults();
  updateSubmitState();

  const { attachSplitter } = await import('../ui.js');
  const { loadPanelWidth, savePanelWidth } = await import('../store.js');
  const savedWidth = loadPanelWidth();
  if (savedWidth) container.style.setProperty('--panel-w', `${savedWidth}px`);
  attachSplitter(splitter, container, {
    min: 320, max: 620, initial: savedWidth || 420, onChange: (w) => savePanelWidth(w),
  });

  /* ================= 生命周期 ================= */
  return {
    onShow() {
      // 从任务中心返回时同步任务状态
      const tasks = getTasks();
      for (const job of state.jobs) {
        const t = tasks.find((x) => x.task_id === job.id);
        if (t) {
          job.status = t.status;
          job.progress = t.progress;
          job.error = t.error;
          job.output = t.output;
        }
      }
      renderResults();
      renderGate();
      updateSubmitState();
    },
    async unmount() {
      abortController?.abort();
      for (const dispose of disposers) {
        try { dispose(); } catch { /* 忽略 */ }
      }
      disposers.length = 0;
      // 释放 audio 元素持有的 blob URL，防止内存泄漏
      container.querySelectorAll('audio').forEach((a) => {
        a.pause();
        a.removeAttribute('src');
        a.load();
      });
    },
  };
}
