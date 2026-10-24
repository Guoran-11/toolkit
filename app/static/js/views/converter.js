/**
 * views/converter.js — 模块一：文档格式转换
 *
 * 覆盖需求：FR-DC-01 ~ FR-DC-11
 *   - 拖拽/点选上传，单文件 100 MB 上限（前端先校验，后端再校验）
 *   - 矩阵驱动的目标格式选择：不支持的方向直接禁用并说明原因
 *   - 每次选择组合都给出「预期质量」提示（§5.1.2 强制要求）
 *   - 批量转换 + ZIP 打包下载
 *   - 实时进度、失败原因、结果预览、有效期提示
 */

import { el, clear, $$, $, formatBytes, extOf, validateFile, shortHash, copyText, uid } from '../utils.js';
import { icon } from '../icons.js';
import { CFG, runtime } from '../config.js';
import { api, ApiError, pollTask } from '../api.js';
import {
  SOURCE_FORMATS, TARGET_FORMATS, GRADE_INFO, MATRIX,
  lookup, supportedTargets, qualityHint, paramsFor, defaultParams,
} from '../matrix.js';
import {
  toast, createDropzone, fileItem, createProgress, statusPill, gradeBadge,
  emptyState, confirmDialog, createCopyButton, tag, modal,
} from '../ui.js';
import { getTasks, upsertTask, subscribe } from '../store.js';

/* ------------------------------------------------------------------ *
 * 视图工厂
 * ------------------------------------------------------------------ */
export default async function createConverterView(container) {
  /* ---------------- 组件状态 ---------------- */
  const state = {
    files: [],            // 选中的文件
    source: null,         // 源格式 id
    target: null,         // 目标格式 id
    params: {},           // 转换参数
    uploading: false,
    uploadPercent: 0,
    /** @type {Array<{id:string, name:string, status:string, progress:number, error:string|null, output:object|null}>} */
    jobs: [],
    selectedJobId: null,
    zipReady: false,
  };

  let abortController = null;
  const disposers = [];

  /* ================= 骨架 ================= */
  // 注意：必须用 classList 增删而不是赋值 className —— 路由会在容器上挂
  // `is-active` 控制可见性，直接赋值 className 会把它抹掉，导致整个视图不可见。
  container.classList.remove('view--single');
  container.classList.add('view', 'view--split');
  container.innerHTML = '';

  // ---------- 左侧面板 ----------
  const dropzone = createDropzone({
    iconName: 'upload',
    title: '拖拽文件到此处，或点击选择',
    hint: `支持 HTML / DOCX / XLSX / PPTX / PDF，单文件不超过 ${CFG.limits.maxUploadMb} MB，批量最多 ${CFG.limits.maxBatchFiles} 个`,
    inputAccept: SOURCE_FORMATS.flatMap((f) => f.exts.map((e) => `.${e}`)).join(','),
    onFiles: (files) => addFiles(files),
  });

  const fileListEl = el('div.file-list');
  const fileSection = el('div.form-section', {}, [
    el('div.form-section-title', {}, [icon('upload'), el('span', { text: '源文件' })]),
    dropzone,
    fileListEl,
  ]);

  const sourceSelect = el('select.select#conv-source');
  sourceSelect.append(el('option', { value: '', text: '自动识别…' }));
  for (const fmt of SOURCE_FORMATS) {
    sourceSelect.append(el('option', { value: fmt.id, text: `${fmt.label}（.${fmt.exts[0]}）` }));
  }
  sourceSelect.addEventListener('change', () => {
    state.source = sourceSelect.value || detectSource(state.files);
    onSourceChanged(true);
  });

  const detectHint = el('div.help');
  const targetPicker = el('div.format-picker');
  const paramHost = el('div.col.gap-3');
  const qualityHost = el('div');

  const convertBtn = el('button.btn.btn--primary.btn--block.btn--lg', {
    type: 'button',
    on: { click: () => submit() },
  }, [icon('convert'), el('span', { text: '开始转换' })]);

  const progressHost = el('div.col.gap-2');

  const leftPanel = el('div.panel.panel--left', {}, [
    el('div.panel-header', {}, [
      el('h2', {}, [icon('convert'), el('span', { text: '转换中心' })]),
      el('div.panel-actions', {}, [
        el('button.btn.btn--sm.btn--ghost', {
          type: 'button',
          'data-tip': '查看完整支持矩阵',
          on: { click: () => showMatrixDialog() },
        }, [icon('listTree'), el('span', { text: '支持矩阵' })]),
      ]),
    ]),
    el('div.panel-body.panel-body--grid', {}, [
      fileSection,
      el('div.form-section', {}, [
        el('div.form-section-title', {}, [icon('file'), el('span', { text: '格式识别' })]),
        el('div.field', {}, [
          el('label.label', { for: 'conv-source', text: '源格式' }),
          sourceSelect,
          detectHint,
        ]),
      ]),
      el('div.form-section', {}, [
        el('div.form-section-title', {}, [icon('package'), el('span', { text: '目标格式' })]),
        targetPicker,
      ]),
      el('div.form-section#conv-params', {}, [
        el('div.form-section-title', {}, [icon('sliders'), el('span', { text: '转换参数' })]),
        paramHost,
      ]),
      qualityHost,
    ]),
    el('div.panel-footer.col.gap-3', {}, [
      progressHost,
      convertBtn,
      el('div.help.t-center', {
        text: '文件仅在本地服务处理，不上传任何第三方',
      }),
    ]),
  ]);

  // ---------- 分隔条 ----------
  const splitter = el('div.splitter', {
    role: 'separator',
    tabindex: '0',
    'aria-orientation': 'vertical',
    'aria-label': '调整面板宽度（使用左右方向键）',
    'aria-valuemin': '320',
    'aria-valuemax': '620',
    'aria-valuenow': '400',
  });

  // ---------- 右侧结果面板 ----------
  const resultBody = el('div.panel-body.panel-body--flush.col');
  const resultHeaderActions = el('div.panel-actions');

  const rightPanel = el('div.panel.panel--right', {}, [
    el('div.panel-header', {}, [
      el('h2', {}, [icon('eye'), el('span', { text: '转换结果' })]),
      resultHeaderActions,
    ]),
    resultBody,
  ]);

  container.append(leftPanel, splitter, rightPanel);

  /* ================= 初始化 ================= */
  renderFiles();
  renderTargets();
  renderParams();
  renderResults();
  renderQuality();

  // 面板宽度记忆
  const { attachSplitter } = await import('../ui.js');
  const { loadPanelWidth, savePanelWidth } = await import('../store.js');
  const savedWidth = loadPanelWidth();
  if (savedWidth) container.style.setProperty('--panel-w', `${savedWidth}px`);
  attachSplitter(splitter, container, {
    min: 320,
    max: 620,
    initial: savedWidth || 400,
    onChange: (w) => savePanelWidth(w),
  });

  // 订阅任务中心变化：任务在别处被取消/清理时同步本视图
  disposers.push(subscribe(['tasks'], (s) => {
    let touched = false;
    for (const job of state.jobs) {
      const task = s.tasks[job.id];
      if (!task) continue;
      if (task.status !== job.status || task.progress !== job.progress) {
        job.status = task.status;
        job.progress = task.progress;
        job.error = task.error;
        job.output = task.output;
        touched = true;
      }
    }
    if (touched) {
      renderResults();
      renderProgressArea();
    }
  }));

  /* ================= 文件处理 ================= */
  function addFiles(incoming) {
    const errors = [];

    for (const file of incoming) {
      // 逐项校验，错误就地反馈而不是整体拒绝（AC-12：友好错误而非崩溃）
      const check = validateFile(file, { maxMb: CFG.limits.maxUploadMb });
      if (!check.ok) {
        errors.push(`${file.name}：${check.error}`);
        continue;
      }
      const ext = extOf(file.name);
      const matched = SOURCE_FORMATS.find((f) => f.exts.includes(ext));
      if (!matched) {
        errors.push(`${file.name}：不支持的扩展名 .${ext || '(无)'}，请上传 ${SOURCE_FORMATS.map((f) => f.exts[0].toUpperCase()).join(' / ')} 文件`);
        continue;
      }
      if (state.files.length >= CFG.limits.maxBatchFiles) {
        errors.push(`已达到批量上限 ${CFG.limits.maxBatchFiles} 个，其余文件未加入`);
        break;
      }
      // 同一文件重复添加时给出提示而非静默忽略
      if (state.files.some((f) => f.name === file.name && f.size === file.size)) {
        errors.push(`${file.name}：已在列表中，已跳过重复项`);
        continue;
      }
      state.files.push(file);
    }

    if (errors.length > 0) {
      toast('warning', `部分文件未加入（${errors.length} 项）`, errors.slice(0, 3).join('；') + (errors.length > 3 ? ' …' : ''));
    }

    if (incoming.length > 0 && state.files.length > 0) {
      state.source = detectSource(state.files);
      sourceSelect.value = state.source || '';
      onSourceChanged(false);
      renderFiles();
      // 后台补算文件指纹，用于结果核对（不阻塞交互）
      computeHashes();
    }
  }

  /** 依据扩展名推断源格式；混合类型时返回多数量者 */
  function detectSource(files) {
    if (!files.length) return null;
    const ids = files
      .map((f) => SOURCE_FORMATS.find((s) => s.exts.includes(extOf(f.name)))?.id)
      .filter(Boolean);
    if (ids.length === 0) return null;
    const counts = ids.reduce((acc, id) => ({ ...acc, [id]: (acc[id] || 0) + 1 }), {});
    return Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0];
  }

  async function computeHashes() {
    for (const file of state.files) {
      if (file.__hash) continue;
      // eslint-disable-next-line no-await-in-loop
      file.__hash = (await shortHash(file)) || null;
    }
    renderFiles();
  }

  function renderFiles() {
    clear(fileListEl);
    if (state.files.length === 0) return;

    state.files.forEach((file, index) => {
      fileListEl.append(fileItem(file, {
        status: file.__rejected ? 'error' : 'pending',
        message: file.__hash ? `SHA256:${file.__hash}` : '',
        onRemove: () => {
          state.files.splice(index, 1);
          renderFiles();
          state.source = detectSource(state.files);
          sourceSelect.value = state.source || '';
          onSourceChanged(false);
          renderResults();
        },
      }));
    });

    // 混合格式警告：批量转换要求统一目标格式，源类型不一致时结果可能不理想
    const types = new Set(state.files.map((f) => detectSource([f])));
    if (types.size > 1) {
      fileListEl.append(el('div.alert.alert--warning', {}, [
        icon('alertTriangle'),
        el('div.alert-body', {}, [
          el('div.alert-title', { text: '检测到混合文件类型' }),
          el('div', { text: '批量转换会按同一种目标格式处理全部文件，不同类型的源文件转换质量可能有差异。建议分批评次转换。' }),
        ]),
      ]));
    }
  }

  /* ================= 格式选择 ================= */
  function onSourceChanged(userPicked) {
    // 源格式变化后，目标格式可能失效，需要重新校验
    if (state.target && !lookup(state.source, state.target)?.grade) state.target = null;

    const supported = supportedTargets(state.source);
    if (!state.target && supported.length > 0) {
      // 智能默认：网页优先 PDF，Office 优先 PDF，PDF 优先 Word
      const preference = state.source === 'pdf' ? 'docx' : 'pdf';
      state.target = supported.includes(preference) ? preference : supported[0];
    }

    if (userPicked) {
      const fmt = SOURCE_FORMATS.find((f) => f.id === state.source);
      detectHint.textContent = state.source
        ? `已手动指定为 ${fmt.label}；若识别有误可在下拉框中更正`
        : '请选择源格式，或留空由系统按扩展名自动识别';
    } else {
      const fmt = SOURCE_FORMATS.find((f) => f.id === state.source);
      detectHint.textContent = state.files.length === 0
        ? '添加文件后自动识别，也可手动指定'
        : `已识别为 ${fmt?.label || '未知'}（依据扩展名，服务端会再次按文件魔数校验）`;
    }

    renderTargets();
    renderParams();
    renderQuality();
    updateSubmitState();
  }

  function renderTargets() {
    clear(targetPicker);

    if (!state.source) {
      targetPicker.append(el('div.help', { text: '请先添加文件或选择源格式' }));
      return;
    }

    for (const fmt of TARGET_FORMATS) {
      const cell = lookup(state.source, fmt.id);
      const supported = Boolean(cell?.grade);
      const isSelf = state.source === fmt.id;

      const btn = el('button.format-option', {
        type: 'button',
        'aria-pressed': String(state.target === fmt.id),
        disabled: !supported,
        'data-grade': cell?.grade || 'x',
        'data-tip': supported
          ? `${GRADE_INFO[cell.grade].label}：${GRADE_INFO[cell.grade].desc}`
          : isSelf ? '源格式与目标格式相同' : '该转换方向不支持',
        on: {
          click: () => {
            state.target = fmt.id;
            state.params = defaultParams(state.source, state.target);
            renderTargets();
            renderParams();
            renderQuality();
            updateSubmitState();
          },
        },
      }, [
        el('span.format-ext', { text: fmt.exts[0].toUpperCase() }),
        cell?.grade
          ? el('span.format-grade', { text: GRADE_INFO[cell.grade].label })
          : el('span.format-grade', { text: isSelf ? '同格式' : '不支持' }),
      ]);

      targetPicker.append(btn);
    }
  }

  /* ================= 参数表单 ================= */
  function renderParams() {
    clear(paramHost);
    const paramsHost = $('#conv-params', container);

    if (!state.source || !state.target) {
      paramsHost?.classList.add('hidden');
      return;
    }
    paramsHost?.classList.remove('hidden');

    const defs = paramsFor(state.source, state.target);
    if (defs.length === 0) {
      paramHost.append(el('div.help', { text: '该转换方向无需额外参数。' }));
      return;
    }

    if (Object.keys(state.params).length === 0) state.params = defaultParams(state.source, state.target);

    for (const def of defs) {
      const inputId = uid(`p-${def.id}`);
      let control = null;

      if (def.type === 'select') {
        control = el('select.select', { id: inputId });
        for (const opt of def.options) {
          control.append(el('option', { value: opt.value, text: opt.label, selected: String(state.params[def.id]) === String(opt.value) }));
        }
        control.addEventListener('change', () => { state.params[def.id] = control.value; });
      } else if (def.type === 'checkbox') {
        const cb = el('input', { id: inputId, type: 'checkbox', checked: state.params[def.id] !== false });
        cb.addEventListener('change', () => { state.params[def.id] = cb.checked; });
        const wrap = el('label.check', { for: inputId }, [cb, el('span', { text: def.label })]);
        const field = el('div.field', {}, [wrap]);
        if (def.help) field.append(el('div.help', { text: def.help }));
        paramHost.append(field);
        continue;
      } else if (def.type === 'number') {
        control = el('input.input', {
          id: inputId, type: 'number',
          min: def.min, max: def.max, step: def.step, value: state.params[def.id],
        });
        control.addEventListener('input', () => { state.params[def.id] = Number(control.value); });
      } else {
        control = el('input.input', { id: inputId, type: 'text', value: state.params[def.id] ?? '', placeholder: def.placeholder || '' });
        control.addEventListener('input', () => { state.params[def.id] = control.value; });
      }

      const field = el('div.field', {}, [
        el('label.label', { for: inputId }, [
          el('span', { text: def.label }),
          def.unit ? el('span.t-muted', { text: `（${def.unit}）` }) : null,
        ]),
        control,
        def.help ? el('div.help', { text: def.help }) : null,
      ]);
      paramHost.append(field);
    }
  }

  /* ================= 质量提示 ================= */
  function renderQuality() {
    clear(qualityHost);
    if (!state.source || !state.target) return;

    const hint = qualityHint(state.source, state.target);
    if (!hint) return;

    // grade C（图片式输出）需要更醒目的警告，防止预期落差（风险 R5）
    const cell = lookup(state.source, state.target);
    const alertIcon = hint.level === 'danger' ? 'alertCircle'
      : hint.level === 'warning' ? 'alertTriangle'
      : hint.level === 'success' ? 'checkCircle' : 'info';

    qualityHost.append(el(`div.alert.alert--${hint.level}`, { role: 'status' }, [
      icon(alertIcon),
      el('div.alert-body', {}, [
        el('div.alert-title', {}, [
          el('span', { text: hint.title }),
          cell?.grade ? el('span', { style: { marginLeft: '6px' } }, [gradeBadge(cell.grade)]) : null,
        ]),
        el('div', { text: hint.body }),
      ]),
    ]));
  }

  function updateSubmitState() {
    const ready = state.files.length > 0 && state.source && state.target && lookup(state.source, state.target)?.grade && !state.uploading;
    convertBtn.disabled = !ready;
    convertBtn.classList.toggle('is-loading', state.uploading);

    const label = convertBtn.querySelector('span');
    if (state.uploading) {
      if (label) label.textContent = '处理中…';
      return;
    }
    const count = state.files.length;
    if (label) {
      label.textContent = count > 1 ? `开始转换（${count} 个文件）` : '开始转换';
    }
  }

  /* ================= 提交转换 ================= */
  async function submit() {
    if (state.uploading) return;

    if (state.files.length === 0) {
      toast('warning', '请先添加文件', '拖拽文件到上传区，或点击上传区选择文件');
      dropzone.focus?.();
      return;
    }
    if (!lookup(state.source, state.target)?.grade) {
      toast('danger', '该转换方向不支持', `请更换目标格式。${state.source.toUpperCase()} → ${state.target?.toUpperCase()} 不在支持范围内`);
      return;
    }
    if (runtime.demo) {
      toast('info', '当前为演示模式', '尚未连接 Python 后端，将生成占位文件用于验证界面链路');
    }

    state.uploading = true;
    state.jobs = [];
    state.selectedJobId = null;
    state.zipReady = false;
    updateSubmitState();
    renderResults();
    renderProgressArea();

    const uploadProgress = createProgress();
    const progressWrap = el('div.col.gap-2', {}, [
      el('div.row.between', {}, [
        el('span.t-sm.t-muted', { text: '上传中…' }),
        el('span.t-sm.t-mono#upload-pct', { text: '0%' }),
      ]),
      uploadProgress,
    ]);
    clear(progressHost);
    progressHost.append(progressWrap);

    abortController = new AbortController();

    try {
      const created = await api.convert(
        state.files,
        { source: state.source, target: state.target, params: state.params },
        (loaded, total, percent) => {
          uploadProgress.setValue(percent);
          const pctNode = $('#upload-pct', progressWrap);
          if (pctNode) pctNode.textContent = `${percent}%`;
        },
      );

      // 上传完成，进入任务监控阶段
      progressWrap.remove();
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

      // 并发轮询全部任务
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

      const okCount = results.filter((r) => r.status === 'fulfilled' && r.value.status === 'success').length;
      const failCount = results.length - okCount;

      if (failCount === 0) {
        toast('success', `转换完成（${okCount} 个文件）`, '结果已就绪，可直接下载');
      } else if (okCount === 0) {
        toast('danger', '全部转换失败', '请查看下方失败原因，或更换转换方向后重试');
      } else {
        toast('warning', `部分成功（成功 ${okCount} / 失败 ${failCount}）`, '失败项已在结果列表中标注原因');
      }

      if (state.jobs.length > 1 && okCount > 0) state.zipReady = true;
      renderResults();
    } catch (err) {
      const message = err instanceof ApiError ? err.message : String(err?.message || err);
      if (err?.code !== 'ABORTED') {
        toast('danger', '转换请求失败', message);
        renderError(message);
      }
    } finally {
      state.uploading = false;
      abortController = null;
      updateSubmitState();
      renderProgressArea();
    }
  }

  /* ================= 结果区渲染 ================= */
  function renderProgressArea() {
    clear(progressHost);
    const active = state.jobs.filter((j) => j.status === 'pending' || j.status === 'running');
    if (active.length === 0) return;

    const done = state.jobs.length - active.length;
    const overall = state.jobs.length > 0 ? Math.round(((done + active.reduce((s, j) => s + j.progress / 100, 0)) / state.jobs.length) * 100) : 0;

    const bar = createProgress();
    bar.setValue(overall);

    progressHost.append(el('div.col.gap-2', {}, [
      el('div.row.between', {}, [
        el('span.t-sm.t-muted', { text: `转换中 ${done}/${state.jobs.length}` }),
        el('span.t-sm.t-mono', { text: `${overall}%` }),
      ]),
      bar,
      el('button.btn.btn--sm.btn--danger-ghost', {
        type: 'button',
        on: { click: () => cancelAll() },
      }, [icon('stop'), el('span', { text: '取消全部任务' })]),
    ]));
  }

  async function cancelAll() {
    abortController?.abort();
    await Promise.allSettled(state.jobs
      .filter((j) => j.status === 'pending' || j.status === 'running')
      .map((j) => api.tasks.cancel(j.id)));
    toast('info', '已请求取消全部任务');
  }

  function renderResults() {
    clear(resultBody);
    clear(resultHeaderActions);

    if (state.jobs.length === 0) {
      resultBody.append(emptyState({
        iconName: 'convert',
        title: '暂无转换任务',
        desc: '在左侧添加文件、选择目标格式后点击「开始转换」，结果会显示在这里。PDF 与图片可直接在线预览。',
      }));
      return;
    }

    const succeeded = state.jobs.filter((j) => j.status === 'success');

    // 顶部操作条
    if (state.zipReady) {
      resultHeaderActions.append(el('button.btn.btn--sm', {
        type: 'button',
        on: { click: () => downloadZip() },
      }, [icon('archive'), el('span', { text: '打包下载 ZIP' })]));
    }
    if (succeeded.length > 1) {
      resultHeaderActions.append(el('button.btn.btn--sm.btn--ghost', {
        type: 'button',
        on: { click: () => downloadAll() },
      }, [icon('download'), el('span', { text: '逐个下载' })]));
    }
    resultHeaderActions.append(el('button.btn.btn--sm.btn--icon.btn--ghost', {
      type: 'button',
      'aria-label': '清空结果列表',
      'data-tip': '清空结果列表',
      on: {
        click: async () => {
          if (state.jobs.length > 0 && !(await confirmDialog({
            title: '清空结果列表',
            message: '仅从当前页面移除任务记录，服务端产物仍会保留至过期时间。',
            confirmLabel: '清空',
          }))) return;
          state.jobs = [];
          state.zipReady = false;
          renderResults();
        },
      },
    }, [icon('trash')]));

    const listWrap = el('div.col.gap-3', { style: { padding: 'var(--sp-4)' } });

    // 批量汇总（AC-04：失败项标红并给出原因）
    if (state.jobs.length > 1) {
      const ok = succeeded.length;
      const failed = state.jobs.filter((j) => j.status === 'failed').length;
      listWrap.append(el('div.alert', { role: 'status' }, [
        icon(ok === state.jobs.length ? 'checkCircle' : 'info'),
        el('div.alert-body', {}, [
          el('div.alert-title', { text: `批量结果：成功 ${ok} / 共 ${state.jobs.length}` }),
          failed > 0 ? el('div', { text: `${failed} 个文件转换失败，原因见下方标红项。` }) : null,
        ]),
      ]));
    }

    for (const job of state.jobs) {
      listWrap.append(jobCard(job));
    }

    resultBody.append(listWrap);
    resultBody.append(el('div', { style: { padding: '0 var(--sp-4) var(--sp-4)' } }, [
      el('div.help', {
        text: '结果文件默认保留 24 小时，过期后自动清理。涉及重要文档请及时下载并自行校验完整性。',
      }),
    ]));
  }

  function jobCard(job) {
    const card = el('div.result-card', { 'data-job': job.id });
    const ext = job.output ? extOf(job.output.name) : '?';

    card.append(el('div.result-thumb', { text: ext.slice(0, 4) }));

    const main = el('div.result-main', {}, [
      el('div.row.gap-2', {}, [
        el('span.t-clamp-1', { text: job.name, title: job.name, style: { fontWeight: 'var(--fw-medium)' } }),
      ]),
      el('div.row.gap-2.wrap', {}, [
        statusPill(job.status),
        job.output ? tag(formatBytes(job.output.size), 'mono') : null,
        job.status === 'success' ? tag('24 小时后过期') : null,
      ]),
    ]);

    if (job.status === 'running' || job.status === 'pending') {
      const bar = createProgress({ size: 'sm' });
      if (job.status === 'pending') bar.setIndeterminate();
      else bar.setValue(job.progress);
      bar.setLabel(`${job.name} 转换进度`);
      main.append(bar);
      main.append(el('div.t-xs.t-muted', { text: job.status === 'pending' ? '排队等待执行…' : `转换中 ${job.progress}%` }));
    }

    if (job.status === 'failed' && job.error) {
      main.append(el('div.t-xs.t-danger', { text: job.error }));
    }

    card.append(main);

    // 操作区
    const actions = el('div.row.gap-1.shrink-0');

    if (job.status === 'success' && job.output) {
      actions.append(el('button.btn.btn--sm.btn--icon', {
        type: 'button',
        'aria-label': `下载 ${job.output.name}`,
        'data-tip': '下载',
        on: { click: () => downloadOne(job) },
      }, [icon('download')]));

      // 可预览类型提供在线预览
      if (canPreview(job.output.name)) {
        actions.append(el('button.btn.btn--sm.btn--icon.btn--ghost', {
          type: 'button',
          'aria-label': `预览 ${job.output.name}`,
          'data-tip': '在线预览',
          on: { click: () => showPreview(job) },
        }, [icon('eye')]));
      }
    }

    if (job.status === 'failed') {
      actions.append(el('button.btn.btn--sm.btn--icon.btn--ghost', {
        type: 'button',
        'aria-label': '重试该文件',
        'data-tip': '重试',
        on: { click: () => retryJob(job) },
      }, [icon('refresh')]));
    }

    if (job.status === 'running' || job.status === 'pending') {
      actions.append(el('button.btn.btn--sm.btn--icon.btn--ghost', {
        type: 'button',
        'aria-label': '取消该任务',
        'data-tip': '取消',
        on: { click: async () => { await api.tasks.cancel(job.id); toast('info', '已请求取消任务'); } },
      }, [icon('close')]));
    }

    actions.append(el('button.btn.btn--sm.btn--icon.btn--ghost', {
      type: 'button',
      'aria-label': '从列表移除',
      'data-tip': '移除记录',
      on: {
        click: () => {
          state.jobs = state.jobs.filter((j) => j.id !== job.id);
          renderResults();
        },
      },
    }, [icon('trash')]));

    card.append(actions);
    return card;
  }

  const canPreview = (name) => ['pdf', 'png', 'jpg', 'jpeg', 'html', 'htm'].includes(extOf(name));

  function showPreview(job) {
    const ext = extOf(job.output.name);
    const url = api.fileUrl(job.output.file_id);
    if (!url) {
      toast('warning', '无法预览', '产物链接已失效，请重新转换');
      return;
    }

    const body = el('div.col.gap-3', { style: { minHeight: '50dvh' } });

    if (ext === 'pdf') {
      body.append(el('iframe', {
        src: url,
        title: `${job.output.name} 预览`,
        style: { width: '100%', height: '62dvh', border: '1px solid var(--border-subtle)', borderRadius: 'var(--r-md)' },
      }));
    } else if (['png', 'jpg', 'jpeg'].includes(ext)) {
      body.append(el('img', {
        src: url,
        alt: `${job.output.name} 预览`,
        width: '1240',
        height: '1754',
        style: { width: '100%', height: 'auto', borderRadius: 'var(--r-md)', border: '1px solid var(--border-subtle)' },
      }));
    } else {
      // HTML：用 sandbox iframe 隔离，禁止脚本执行与同源访问（安全考虑）
      body.append(el('iframe', {
        src: url,
        title: `${job.output.name} 预览`,
        sandbox: '',
        style: { width: '100%', height: '62dvh', border: '1px solid var(--border-subtle)', borderRadius: 'var(--r-md)', background: '#fff' },
      }));
    }

    body.append(el('div.row.between.wrap.gap-2', {}, [
      el('div.t-sm.t-muted', { text: `${job.output.name} · ${formatBytes(job.output.size)}` }),
      el('div.row.gap-2', {}, [
        el('button.btn.btn--sm', {
          type: 'button',
          on: { click: () => copyText(url).then((ok) => toast(ok ? 'success' : 'warning', ok ? '链接已复制' : '复制失败')) },
        }, [icon('link'), el('span', { text: '复制链接' })]),
        el('button.btn.btn--sm.btn--primary', {
          type: 'button',
          on: { click: () => downloadOne(job) },
        }, [icon('download'), el('span', { text: '下载' })]),
      ]),
    ]));

    modal({
      title: '结果预览',
      size: ext === 'pdf' || ext === 'html' ? 'wide' : 'normal',
      body,
      actions: [{ label: '关闭', variant: 'secondary' }],
      onClose: () => { try { URL.revokeObjectURL(url); } catch { /* 非 blob URL 时忽略 */ } },
    });
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

  async function downloadAll() {
    const items = state.jobs.filter((j) => j.status === 'success' && j.output);
    if (items.length === 0) return;
    if (items.length > 5 && !(await confirmDialog({
      title: '逐个下载',
      message: `将依次触发 ${items.length} 个文件下载，浏览器可能会拦截多文件下载请求。建议改用「打包下载 ZIP」。`,
      confirmLabel: '继续下载',
    }))) return;

    for (const job of items) {
      // 串行触发，避免浏览器把并发的多文件下载当成弹窗滥用而拦截
      // eslint-disable-next-line no-await-in-loop
      await api.download(job.output.file_id, job.output.name).catch(() => {});
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, 350));
    }
  }

  async function downloadZip() {
    const ids = state.jobs.filter((j) => j.status === 'success').map((j) => j.id);
    if (ids.length === 0) return;
    try {
      const base = state.files[0]?.name ? `${state.files[0].name.replace(/\.[^.]+$/, '')}-等${ids.length}项` : 'toolkit-batch';
      await api.batchZip(ids, `${base}.zip`);
      toast('success', '打包下载已开始', `包含 ${ids.length} 个结果文件`);
    } catch (err) {
      toast('danger', '打包失败', err?.message || '请稍后重试');
    }
  }

  async function retryJob(job) {
    const file = state.files.find((f) => f.name === job.name);
    if (!file) {
      toast('warning', '无法重试', '原文件已从列表移除，请重新添加后再试');
      return;
    }
    try {
      const [task] = await api.convert([file], { source: state.source, target: state.target, params: state.params });
      upsertTask(task);
      job.id = task.task_id;
      job.status = task.status;
      job.progress = 0;
      job.error = null;
      job.output = null;
      renderResults();
      await pollTask(task.task_id, {
        onUpdate: (t) => {
          upsertTask(t);
          job.status = t.status;
          job.progress = t.progress;
          job.error = t.error;
          job.output = t.output;
          renderResults();
        },
      });
      toast('success', '重试完成');
    } catch (err) {
      toast('danger', '重试失败', err?.message || '未知错误');
    }
  }

  function renderError(message) {
    clear(resultBody);
    resultBody.append(el('div', { style: { padding: 'var(--sp-4)' } }, [
      el('div.alert.alert--danger', { role: 'alert' }, [
        icon('alertCircle'),
        el('div.alert-body', {}, [
          el('div.alert-title', { text: '转换请求失败' }),
          el('div', { text: message }),
          el('div.t-xs', { text: '排查建议：确认后端服务已启动（访问 /healthz）；检查文件是否损坏；若持续失败请查看服务端日志。' }),
        ]),
      ]),
    ]));
  }

  /* ================= 支持矩阵弹窗 ================= */
  function showMatrixDialog() {
    const ids = ['html', 'docx', 'xlsx', 'pptx', 'pdf'];
    const targets = ['docx', 'xlsx', 'pptx', 'pdf', 'html', 'png'];

    const table = el('table.table.matrix-table');
    const thead = el('thead', {}, [
      el('tr', {}, [
        el('th', { text: '源 ＼ 目标' }),
        ...targets.map((t) => el('th', { text: t.toUpperCase() })),
      ]),
    ]);

    const tbody = el('tbody');
    for (const s of ids) {
      const tr = el('tr', {}, [el('td.m-idx', { text: s.toUpperCase() })]);
      for (const t of targets) {
        const cell = MATRIX[s]?.[t];
        const td = el('td');
        if (s === t) {
          td.append(el('span.m-none', { text: '—' }));
        } else if (!cell?.grade) {
          td.append(el('span.m-cell.m-x', { text: '×', title: '不支持' }));
        } else {
          td.className = 'is-matchable';
          td.append(el('span', {
            class: `m-cell m-${cell.grade.toLowerCase()}`,
            text: cell.grade,
            title: `${GRADE_INFO[cell.grade].label}：${cell.note}｜引擎：${cell.engine}`,
          }));
          // 点击矩阵格子直接套用该组合，省去两次下拉选择
          td.addEventListener('click', () => {
            if (state.source !== s) {
              toast('info', '已切换格式组合', `${s.toUpperCase()} → ${t.toUpperCase()}`);
            }
            state.source = s;
            sourceSelect.value = s;
            state.target = t;
            state.params = defaultParams(s, t);
            onSourceChanged(true);
            dlg.close(true);
          });
        }
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.append(thead, tbody);

    const legend = el('div.row.gap-4.wrap.mt-4', {}, ['A', 'B', 'C'].map((g) => el('div.row.gap-2', {}, [
      gradeBadge(g),
      el('span.t-xs.t-muted', { text: GRADE_INFO[g].desc }),
    ])).concat([
      el('div.row.gap-2', {}, [el('span.m-cell.m-x', { text: '×' }), el('span.t-xs.t-muted', { text: '不支持该方向' })]),
    ]));

    const body = el('div.col.gap-3', {}, [
      el('div.help', { text: '点击表格中的任意等级格子，可直接套用该「源 → 目标」组合。悬停查看质量说明与所用引擎。' }),
      el('div.table-wrap', {}, [table]),
      legend,
    ]);

    const dlg = modal({
      title: '格式支持矩阵',
      size: 'wide',
      body,
      actions: [{ label: '关闭', variant: 'secondary' }],
    });
  }

  /* ================= 生命周期 ================= */
  return {
    onShow() {
      // 从任务中心返回时，同步最新任务状态
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
      updateSubmitState();
    },
    async unmount() {
      abortController?.abort();
      for (const dispose of disposers) {
        try { dispose(); } catch { /* 忽略 */ }
      }
      disposers.length = 0;
      // 释放仍在使用中的 blob URL（避免内存泄漏 —— AC-11）
      $$('iframe[src^="blob:"], img[src^="blob:"]', container).forEach((n) => {
        try { URL.revokeObjectURL(n.src); } catch { /* 忽略 */ }
      });
    },
  };
}
