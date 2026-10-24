/**
 * views/tasks.js — 系统级：统一任务中心
 *
 * 覆盖需求：FR-SYS-01 / FR-SYS-04 / FR-SYS-05 / FR-SYS-06 / FR-SYS-08
 *   - 所有模块的异步任务集中展示进度、状态、耗时与失败原因
 *   - 支持按状态筛选、关键字搜索、批量打包下载、批量清理
 *   - 引擎自检：LibreOffice / Chromium / FFmpeg 版本与路径，缺失时给出安装指引
 *   - 磁盘余量与产物有效期提示（剩余 < 1 GB 时告警）
 *
 * 数据来源说明：本地任务由 store 维护（各视图提交后写入），
 * 进入本视图时再向后端拉取一次全量列表做对齐，保证刷新页面后不丢记录。
 */

import { el, clear, $, $$, formatBytes, formatTime, formatRelative, formatElapsed,
  extOf, debounce } from '../utils.js';
import { icon } from '../icons.js';
import { api, ApiError } from '../api.js';
import {
  toast, statusPill, emptyState, confirmDialog, modal, tag, createProgress, ENGINE_LABELS,
} from '../ui.js';
import { getTasks, replaceTasks, removeTask, subscribe, state as storeState } from '../store.js';

/** 模块 → 中文标签 */
const MODULE_LABELS = {
  converter: '文档转换',
  audio: '音频',
  video: '视频下载',
  markdown: 'Markdown',
  ssh: 'SSH',
};

/* ------------------------------------------------------------------ *
 * 视图工厂
 * ------------------------------------------------------------------ */
export default async function createTasksView(container) {
  /* ---------------- 状态 ---------------- */
  const state = {
    filter: 'all',
    keyword: '',
    selected: new Set(),
    loading: false,
    autoRefresh: true,
    lastSync: null,
    engines: null,
    diskFree: null,
  };

  let refreshTimer = null;
  const disposers = [];

  // 用 classList 增删而非赋值 className：路由依赖容器上的 `is-active` 控制可见性
  container.classList.remove('view--split');
  container.classList.add('view', 'view--single');
  container.innerHTML = '';

  /* ================= 结构 ================= */

  // ---------- 状态筛选 ----------
  const filterSegmented = el('div.segmented', { role: 'group', 'aria-label': '按状态筛选任务' });
  const FILTERS = [
    { id: 'all', label: '全部' },
    { id: 'running', label: '进行中' },
    { id: 'success', label: '已完成' },
    { id: 'failed', label: '已失败' },
  ];
  for (const f of FILTERS) {
    filterSegmented.append(el('button.segmented-item', {
      type: 'button',
      text: f.label,
      'aria-pressed': String(state.filter === f.id),
      on: {
        click: () => {
          state.filter = f.id;
          for (const btn of $$('.segmented-item', filterSegmented)) {
            btn.setAttribute('aria-pressed', String(btn.textContent === f.label));
          }
          renderTable();
        },
      },
    }));
  }

  const searchInput = el('input.input', {
    type: 'search',
    placeholder: '搜索文件名或任务 ID…',
    'aria-label': '搜索任务',
    style: { maxWidth: '240px' },
  });
  searchInput.addEventListener('input', debounce(() => {
    state.keyword = searchInput.value.trim().toLowerCase();
    renderTable();
  }, 200));

  const summaryEl = el('div.row.gap-3.t-xs.t-muted');

  const toolbar = el('div.tasks-toolbar', { role: 'toolbar', 'aria-label': '任务中心工具栏' }, [
    filterSegmented,
    el('label', { style: { position: 'relative' } }, [
      searchInput,
      el('span.sr-only', { text: '搜索任务' }),
    ]),
    el('div.grow'),
    summaryEl,
    el('button.btn.btn--sm', {
      type: 'button',
      'data-tip': '从服务端重新拉取任务列表',
      on: { click: () => syncFromServer(true) },
    }, [icon('refresh'), el('span', { text: '刷新' })]),
    el('button.btn.btn--sm', {
      type: 'button',
      'data-tip': '把已完成任务的结果打包为 ZIP',
      on: { click: () => zipSelected() },
    }, [icon('archive'), el('span', { text: '打包下载' })]),
    el('button.btn.btn--sm.btn--danger-ghost', {
      type: 'button',
      'data-tip': '清理已完成的任务记录与产物',
      on: { click: () => purgeFinished() },
    }, [icon('trash'), el('span', { text: '清理' })]),
  ]);

  // ---------- 表格 ----------
  const tableBody = el('tbody');
  const table = el('table.table', { 'aria-label': '任务列表' }, [
    el('thead', {}, [
      el('tr', {}, [
        el('th', { style: { width: '36px' } }, [
          el('input', {
            type: 'checkbox',
            'aria-label': '全选',
            on: { change: (e) => toggleAll(e.currentTarget.checked) },
          }),
        ]),
        el('th', { text: '任务' }),
        el('th', { text: '模块 / 操作' }),
        el('th', { text: '状态' }),
        el('th', { text: '进度' }),
        el('th', { text: '耗时' }),
        el('th', { text: '创建时间' }),
        el('th', { text: '有效期' }),
        el('th', { style: { textAlign: 'right' }, text: '操作' }),
      ]),
    ]),
    tableBody,
  ]);

  const tableWrap = el('div.tasks-body', {}, [
    el('div.table-wrap', {}, [table]),
    el('div#tasks-empty'),
  ]);

  // ---------- 批量操作条 ----------
  const bulkCount = el('span.t-sm');
  const bulkbar = el('div.bulkbar', { hidden: true }, [
    bulkCount,
    el('div.grow'),
    el('button.btn.btn--sm', {
      type: 'button',
      on: { click: () => zipSelected() },
    }, [icon('archive'), el('span', { text: '打包下载所选' })]),
    el('button.btn.btn--sm.btn--danger-ghost', {
      type: 'button',
      on: { click: () => deleteSelected() },
    }, [icon('trash'), el('span', { text: '删除所选' })]),
    el('button.btn.btn--sm.btn--ghost', {
      type: 'button',
      on: { click: () => { state.selected.clear(); renderTable(); } },
    }, [el('span', { text: '取消选择' })]),
  ]);

  // ---------- 引擎自检 ----------
  const enginesBody = el('div');
  const enginesPanel = el('section', {
    'aria-label': '引擎自检',
    style: { margin: 'var(--sp-4)', border: '1px solid var(--border-subtle)', borderRadius: 'var(--r-lg)', background: 'var(--bg-surface)' },
  }, [
    el('div.panel-header', { style: { borderRadius: 'var(--r-lg) var(--r-lg) 0 0' } }, [
      el('h2', {}, [icon('cpu'), el('span', { text: '引擎自检' })]),
      el('div.panel-actions', {}, [
        el('button.btn.btn--sm.btn--ghost', {
          type: 'button',
          on: { click: () => loadEngines(true) },
        }, [icon('refresh'), el('span', { text: '重新检测' })]),
      ]),
    ]),
    el('div', { style: { padding: 'var(--sp-2) var(--sp-4) var(--sp-4)' } }, [enginesBody]),
  ]);

  // ---------- 账号管理（仅管理员可见） ----------
  const usersBody = el('div');
  const usersPanel = el('section', {
    'aria-label': '账号管理',
    style: { margin: 'var(--sp-4)', border: '1px solid var(--border-subtle)', borderRadius: 'var(--r-lg)', background: 'var(--bg-surface)' },
  }, [
    el('div.panel-header', { style: { borderRadius: 'var(--r-lg) var(--r-lg) 0 0' } }, [
      el('h2', {}, [icon('user'), el('span', { text: '账号管理' })]),
      el('div.panel-actions', {}, [
        el('button.btn.btn--sm.btn--ghost', {
          type: 'button',
          on: { click: () => loadUsers(true) },
        }, [icon('refresh'), el('span', { text: '刷新' })]),
      ]),
    ]),
    el('div', { style: { padding: 'var(--sp-2) var(--sp-4) var(--sp-4)' } }, [usersBody]),
  ]);

  const isAdmin = () => storeState.auth?.user?.role === 'admin';

  async function loadUsers(showToast) {
    if (!isAdmin()) return;
    try {
      const data = await api.admin.listUsers({ pageSize: 200 });
      renderUsers(data?.items || []);
      if (showToast) toast('success', '账号列表已刷新', `共 ${data?.total ?? 0} 个注册用户`);
    } catch (err) {
      clear(usersBody);
      usersBody.append(el('div.alert.alert--danger', {}, [
        icon('alertCircle'),
        el('div.alert-body', {}, [el('div', { text: err instanceof ApiError ? err.message : String(err?.message || err) })]),
      ]));
    }
  }

  function renderUsers(items) {
    clear(usersBody);
    if (!items.length) {
      usersBody.append(emptyState({
        iconName: 'user',
        title: '暂无注册用户',
        desc: '在 config.toml 中把 auth.allow_registration 设为 true 后，用户即可通过邮箱验证码自行注册。',
      }));
      return;
    }

    const list = el('div.col.gap-2');
    for (const u of items) {
      list.append(el('div.row.gap-3', {
        style: { padding: 'var(--sp-3) 0', borderBottom: '1px solid var(--border-subtle)', flexWrap: 'wrap' },
      }, [
        el('div.col', { style: { flex: '1 1 220px', minWidth: '0' } }, [
          el('div', { text: u.display_name || u.email, style: { fontWeight: 'var(--fw-semibold)' } }),
          el('div.help', { text: `${u.email}　最近登录：${u.last_login_at ? formatRelative(u.last_login_at) : '从未'}` }),
        ]),
        tag(u.role === 'admin' ? '管理员' : '普通用户'),
        tag(u.status === 'active' ? '正常' : '已禁用'),
        el('div.row.gap-1', {}, [
          el('button.btn.btn--sm.btn--secondary', {
            type: 'button',
            text: u.role === 'admin' ? '降为普通' : '设为管理员',
            on: { click: () => patchUser(u, { role: u.role === 'admin' ? 'user' : 'admin' }) },
          }),
          el('button.btn.btn--sm.btn--secondary', {
            type: 'button',
            text: u.status === 'active' ? '禁用' : '启用',
            on: { click: () => patchUser(u, { status: u.status === 'active' ? 'disabled' : 'active' }) },
          }),
          el('button.btn.btn--sm.btn--ghost', {
            type: 'button',
            text: '强制下线',
            on: { click: () => revokeUser(u) },
          }),
          el('button.btn.btn--sm.btn--danger-ghost', {
            type: 'button',
            text: '删除',
            on: { click: () => removeUser(u) },
          }),
        ]),
      ]));
    }
    usersBody.append(list);
  }

  async function patchUser(user, patch) {
    try {
      await api.admin.updateUser(user.id, patch);
      toast('success', '已更新', `${user.email} 已更新`);
      await loadUsers(false);
    } catch (err) {
      toast('error', '更新失败', err instanceof ApiError ? err.message : String(err?.message || err));
    }
  }

  async function revokeUser(user) {
    const ok = await confirmDialog({
      title: '强制下线',
      message: `将吊销 ${user.email} 的全部登录状态，该用户需要重新登录。`,
      confirmLabel: '确认吊销',
      danger: true,
    });
    if (!ok) return;
    try {
      await api.admin.revokeTokens(user.id);
      toast('success', '已吊销', `${user.email} 的所有会话已失效`);
    } catch (err) {
      toast('error', '操作失败', err instanceof ApiError ? err.message : String(err?.message || err));
    }
  }

  async function removeUser(user) {
    const ok = await confirmDialog({
      title: '删除账号',
      message: `将永久删除账号 ${user.email}，该操作不可撤销。`,
      confirmLabel: '删除',
      danger: true,
    });
    if (!ok) return;
    try {
      await api.admin.removeUser(user.id);
      toast('success', '已删除', user.email);
      await loadUsers(false);
    } catch (err) {
      toast('error', '删除失败', err instanceof ApiError ? err.message : String(err?.message || err));
    }
  }

  const layoutParts = [toolbar, tableWrap, bulkbar, enginesPanel];
  if (isAdmin()) layoutParts.push(usersPanel);
  container.append(...layoutParts);

  /* ================= 初始化 ================= */
  renderTable();
  loadEngines(false);
  if (isAdmin()) loadUsers(false);

  // 订阅本地任务变化（其他视图提交任务后本视图即时可见）
  disposers.push(subscribe(['tasks'], () => renderTable()));
  // 订阅后端状态（磁盘余量、引擎状态）
  disposers.push(subscribe(['backend', 'engines'], () => {
    state.diskFree = storeState.backend.diskFreeBytes;
    renderSummary();
    renderEngines();
  }));

  // 自动刷新：仅在存在进行中任务时轮询，避免空转
  refreshTimer = setInterval(() => {
    if (!state.autoRefresh) return;
    const hasActive = getTasks().some((t) => t.status === 'pending' || t.status === 'running');
    if (hasActive) syncFromServer(false);
  }, 3000);
  disposers.push(() => clearInterval(refreshTimer));

  /* ================= 数据同步 ================= */
  async function syncFromServer(showToast) {
    if (state.loading) return;
    state.loading = true;
    try {
      const data = await api.tasks.list({ pageSize: 200 });
      const items = data?.items || [];
      if (items.length > 0 || getTasks().length === 0) replaceTasks(items);
      state.lastSync = new Date();
      renderTable();
      if (showToast) toast('success', '已刷新', `共 ${items.length} 条任务记录`);
    } catch (err) {
      if (showToast) {
        toast('danger', '刷新失败', err instanceof ApiError ? err.message : String(err?.message || err));
      }
    } finally {
      state.loading = false;
    }
  }

  async function loadEngines(showToast) {
    clear(enginesBody);
    enginesBody.append(el('div.row.gap-2.t-sm.t-muted', {}, [icon('activity'), el('span', { text: '正在检测…' })]));

    try {
      const data = await api.engines();
      state.engines = data?.engines || storeState.engines;
      state.diskFree = data?.disk_free_bytes ?? storeState.backend.diskFreeBytes;
      renderEngines();
      if (showToast) toast('success', '检测完成', '引擎状态已更新');
    } catch (err) {
      clear(enginesBody);
      enginesBody.append(el('div.alert.alert--warning', {}, [
        icon('alertTriangle'),
        el('div.alert-body', {}, [
          el('div.alert-title', { text: '无法获取引擎状态' }),
          el('div', { text: err?.message || '后端服务不可达。演示模式下引擎状态为模拟值。' }),
        ]),
      ]));
    }
  }

  function renderEngines() {
    clear(enginesBody);
    const engines = state.engines || storeState.engines || {};

    const stateText = { ok: '可用', warn: '降级运行', fail: '不可用', unknown: '未知' };
    const stateTag = { ok: 'success', warn: 'warning', fail: 'danger', unknown: '' };

    for (const key of ['libreoffice', 'chromium', 'ffmpeg']) {
      const info = engines[key] || { state: 'unknown' };
      const label = ENGINE_LABELS[key]?.name || key;

      const row = el('div.engine-row', {}, [
        el('span.engine-name', { text: label }),
        el('span', {}, [tag(stateText[info.state] || '未知', stateTag[info.state] || '')]),
        info.version ? el('span.t-xs.t-mono.t-muted', { text: `v${info.version}` }) : null,
        el('span.grow', {}, [
          info.path ? el('code', { text: info.path }) : null,
        ]),
        el('span.t-xs.t-muted', { text: info.state === 'ok' ? '' : (info.hint || '') }),
      ]);
      enginesBody.append(row);

      // 缺失引擎给出可执行的安装指引（FR-SYS-06）
      if (info.state === 'fail') {
        enginesBody.append(el('div.alert.alert--danger', { style: { marginBottom: 'var(--sp-3)' } }, [
          icon('alertCircle'),
          el('div.alert-body', {}, [
            el('div.alert-title', { text: `${label} 不可用，相关转换功能将失败` }),
            el('div', { text: installHint(key) }),
          ]),
        ]));
      }
    }

    // 磁盘余量
    if (typeof state.diskFree === 'number') {
      const low = state.diskFree < 1024 ** 3;
      enginesBody.append(el(`div.alert${low ? '.alert--warning' : '.alert--info'}`, { style: { marginTop: 'var(--sp-3)' } }, [
        icon(low ? 'alertTriangle' : 'database'),
        el('div.alert-body', {}, [
          el('div.alert-title', { text: `磁盘剩余 ${formatBytes(state.diskFree)}` }),
          el('div', {
            text: low
              ? '可用空间已低于 1 GB，转换任务可能因写盘失败而中断。请清理 storage/outputs 目录中的过期产物。'
              : '输出文件默认保留 24 小时，过期后自动清理。',
          }),
        ]),
      ]));
    }
  }

  function installHint(key) {
    const hints = {
      libreoffice: 'Windows：解压 LibreOffice Portable 到 runtime/libreoffice/ 并在 config.toml 指定 soffice 路径。Linux：apt install libreoffice 或下载 AppImage。',
      chromium: '执行 playwright install chromium，并把浏览器目录放入 runtime/chromium/。Linux 若缺依赖库，会输出缺失的 .so 清单。',
      ffmpeg: '下载静态构建（Windows 用 gyan.dev 的 full-static 版），解压到 runtime/ 并确保可执行权限。',
    };
    return hints[key] || '请检查 config.toml 中 [engines] 配置的路径是否正确。';
  }

  /* ================= 表格渲染 ================= */
  function filteredTasks() {
    let list = getTasks();

    if (state.filter === 'running') list = list.filter((t) => t.status === 'pending' || t.status === 'running');
    else if (state.filter !== 'all') list = list.filter((t) => t.status === state.filter);

    if (state.keyword) {
      list = list.filter((t) => {
        const name = (t.inputs?.[0]?.name || '').toLowerCase();
        return name.includes(state.keyword) || t.task_id.toLowerCase().includes(state.keyword);
      });
    }
    return list;
  }

  function renderTable() {
    const tasks = filteredTasks();
    const allTasks = getTasks();
    const emptyHost = $('#tasks-empty', container);

    clear(tableBody);
    if (emptyHost) clear(emptyHost);

    if (allTasks.length === 0) {
      if (emptyHost) {
        emptyHost.append(emptyState({
          iconName: 'tasks',
          title: '暂无任务记录',
          desc: '在转换中心、音频工具或 Markdown 预览中提交任务后，这里会集中显示全部任务的进度与结果。',
        }));
      }
      table.hidden = true;
      renderSummary();
      renderBulkbar();
      return;
    }

    table.hidden = false;

    if (tasks.length === 0) {
      tableBody.append(el('tr', {}, [
        el('td', { colspan: '9', style: { textAlign: 'center', padding: 'var(--sp-7)', color: 'var(--text-muted)' } }, [
          el('div', { text: state.keyword ? `没有匹配「${state.keyword}」的任务` : '当前筛选条件下没有任务' }),
        ]),
      ]));
      renderSummary();
      return;
    }

    for (const task of tasks) {
      const isSelected = state.selected.has(task.task_id);
      const row = el('tr', isSelected ? { class: 'is-selected', 'data-task': task.task_id } : { 'data-task': task.task_id });

      // 勾选
      row.append(el('td', {}, [
        el('input', {
          type: 'checkbox',
          checked: isSelected,
          'aria-label': `选择任务 ${task.inputs?.[0]?.name || task.task_id}`,
          on: {
            change: (e) => {
              if (e.currentTarget.checked) state.selected.add(task.task_id);
              else state.selected.delete(task.task_id);
              renderTable();
            },
          },
        }),
      ]));

      // 任务名
      const inputName = task.inputs?.[0]?.name || '未知文件';
      const outputName = task.output?.name;
      row.append(el('td', {}, [
        el('div.col.gap-1', {}, [
          el('span.t-clamp-1', { text: inputName, title: inputName, style: { color: 'var(--text-primary)', fontWeight: 'var(--fw-medium)' } }),
          outputName && outputName !== inputName
            ? el('span.t-xs.t-muted', {}, [icon('arrowRight', 'icon'), el('span', { text: ` ${outputName}` })])
            : null,
          el('span.t-xs.t-mono.t-muted', { text: task.task_id.slice(0, 12) }),
        ]),
      ]));

      // 模块 / 操作
      row.append(el('td', {}, [
        el('div.col.gap-1', {}, [
          el('span', {}, [tag(MODULE_LABELS[task.module] || task.module || '—', 'mono')]),
          el('span.t-xs.t-muted.t-mono', { text: task.operation || '—' }),
        ]),
      ]));

      // 状态
      row.append(el('td', {}, [statusPill(task.status)]));

      // 进度
      const pct = Math.max(0, Math.min(100, Number(task.progress) || 0));
      row.append(el('td', {}, [
        el('div.col.gap-1', { style: { minWidth: '92px' } }, [
          el('span.t-xs.t-mono', { text: task.status === 'success' ? '100%' : `${pct}%` }),
          (() => {
            const bar = createProgress({ size: 'sm' });
            if (task.status === 'pending') bar.setIndeterminate();
            else bar.setValue(task.status === 'success' ? 100 : pct, task.status === 'failed' ? 'failed' : undefined);
            bar.setLabel(`${inputName} 进度`);
            return bar;
          })(),
        ]),
      ]));

      // 耗时
      row.append(el('td', {}, [
        el('span.t-xs.t-mono.t-muted', {
          text: formatElapsed(task.created_at, task.finished_at),
        }),
      ]));

      // 创建时间
      row.append(el('td', {}, [
        el('div.col.gap-1', {}, [
          el('span.t-xs', { text: formatTime(task.created_at) }),
          el('span.t-xs.t-muted', { text: formatRelative(task.created_at) }),
        ]),
      ]));

      // 有效期
      row.append(el('td', {}, [
        task.expire_at
          ? el('span.t-xs.t-muted', { text: formatRelative(task.expire_at) })
          : el('span.t-xs.t-muted', { text: task.status === 'failed' ? '—' : '待生成' }),
      ]));

      // 操作
      const actions = el('td.cell-actions', {}, [
        el('div.row.gap-1', { style: { justifyContent: 'flex-end' } }, [
          el('button.btn.btn--sm.btn--icon.btn--ghost', {
            type: 'button',
            'aria-label': '查看任务详情',
            'data-tip': '详情',
            on: { click: () => showDetail(task) },
          }, [icon('eye')]),

          task.output
            ? el('button.btn.btn--sm.btn--icon', {
                type: 'button',
                'aria-label': `下载 ${task.output.name}`,
                'data-tip': '下载',
                on: { click: () => downloadTask(task) },
              }, [icon('download')])
            : null,

          (task.status === 'pending' || task.status === 'running')
            ? el('button.btn.btn--sm.btn--icon.btn--ghost', {
                type: 'button',
                'aria-label': '取消任务',
                'data-tip': '取消',
                on: {
                  click: async () => {
                    try {
                      const updated = await api.tasks.cancel(task.task_id);
                      if (updated && !updated.demo) replaceTasks(getTasks().map((t) => (t.task_id === task.task_id ? updated : t)));
                      toast('info', '已请求取消任务');
                    } catch (err) {
                      toast('danger', '取消失败', err?.message || '未知错误');
                    }
                  },
                },
              }, [icon('stop')])
            : null,

          el('button.btn.btn--sm.btn--icon.btn--danger-ghost', {
            type: 'button',
            'aria-label': '删除任务记录与产物',
            'data-tip': '删除',
            on: { click: () => deleteTask(task) },
          }, [icon('trash')]),
        ]),
      ]);
      row.append(actions);

      tableBody.append(row);
    }

    renderSummary();
    renderBulkbar();
  }

  function renderSummary() {
    const all = getTasks();
    const running = all.filter((t) => t.status === 'running' || t.status === 'pending').length;
    const success = all.filter((t) => t.status === 'success').length;
    const failed = all.filter((t) => t.status === 'failed').length;

    clear(summaryEl);
    summaryEl.append(
      el('span', { text: `共 ${all.length}` }),
      running > 0 ? el('span.t-accent', { text: `进行中 ${running}` }) : null,
      success > 0 ? el('span.t-success', { text: `成功 ${success}` }) : null,
      failed > 0 ? el('span.t-danger', { text: `失败 ${failed}` }) : null,
      state.lastSync ? el('span', { text: `同步于 ${formatTime(state.lastSync, false)}` }) : null,
    );
  }

  function renderBulkbar() {
    const count = state.selected.size;
    bulkbar.hidden = count === 0;
    bulkCount.textContent = `已选择 ${count} 项`;
  }

  function toggleAll(checked) {
    const visible = filteredTasks();
    if (checked) visible.forEach((t) => state.selected.add(t.task_id));
    else visible.forEach((t) => state.selected.delete(t.task_id));
    renderTable();
  }

  /* ================= 操作 ================= */
  async function downloadTask(task) {
    if (!task.output) return;
    try {
      await api.download(task.output.file_id, task.output.name);
      toast('success', '已开始下载', task.output.name);
    } catch (err) {
      toast('danger', '下载失败', err?.message || '文件可能已过期');
    }
  }

  async function zipSelected() {
    const ids = [...state.selected].filter((id) => getTasks().find((t) => t.task_id === id && t.status === 'success'));
    const fallback = getTasks().filter((t) => t.status === 'success').map((t) => t.task_id);
    const target = ids.length > 0 ? ids : fallback;

    if (target.length === 0) {
      toast('warning', '没有可打包的已完成任务', '请先选择已完成的任务，或等待任务完成');
      return;
    }
    try {
      await api.batchZip(target, `toolkit-tasks-${target.length}.zip`);
      toast('success', '打包下载已开始', `包含 ${target.length} 个结果文件`);
    } catch (err) {
      toast('danger', '打包失败', err?.message || '请稍后重试');
    }
  }

  async function deleteTask(task) {
    const ok = await confirmDialog({
      title: '删除任务',
      message: `确定要删除「${task.inputs?.[0]?.name || task.task_id}」吗？`,
      details: task.output
        ? `该任务的结果文件（${task.output.name}）将一并删除，且无法恢复。`
        : '任务记录将被移除。',
      confirmLabel: '删除',
      danger: true,
    });
    if (!ok) return;

    try {
      await api.tasks.remove(task.task_id);
      removeTask(task.task_id);
      state.selected.delete(task.task_id);
      renderTable();
      toast('success', '已删除');
    } catch (err) {
      toast('danger', '删除失败', err?.message || '未知错误');
    }
  }

  async function deleteSelected() {
    const ids = [...state.selected];
    if (ids.length === 0) return;

    const ok = await confirmDialog({
      title: `删除所选的 ${ids.length} 项任务`,
      message: '任务记录与其结果文件都会被删除。',
      details: '此操作不可撤销。如果结果文件还需要使用，请先下载。',
      confirmLabel: '全部删除',
      danger: true,
    });
    if (!ok) return;

    let okCount = 0;
    let failCount = 0;
    for (const id of ids) {
      try {
        // eslint-disable-next-line no-await-in-loop
        await api.tasks.remove(id);
        removeTask(id);
        okCount += 1;
      } catch {
        failCount += 1;
      }
    }
    state.selected.clear();
    renderTable();
    toast(failCount === 0 ? 'success' : 'warning',
      `已删除 ${okCount} 项${failCount ? `，${failCount} 项失败` : ''}`);
  }

  async function purgeFinished() {
    const finished = getTasks().filter((t) => t.status === 'success' || t.status === 'failed' || t.status === 'cancelled');
    if (finished.length === 0) {
      toast('info', '没有可清理的任务', '仅已完成、失败或已取消的任务可被清理');
      return;
    }

    const ok = await confirmDialog({
      title: `清理 ${finished.length} 项已结束的任务`,
      message: '已完成、失败与已取消的任务记录及其结果文件都会被删除。',
      details: '正在进行的任务不受影响，不会被中断。此操作不可撤销。',
      confirmLabel: '确认清理',
      danger: true,
    });
    if (!ok) return;

    let okCount = 0;
    for (const task of finished) {
      try {
        // eslint-disable-next-line no-await-in-loop
        await api.tasks.remove(task.task_id);
        removeTask(task.task_id);
        okCount += 1;
      } catch { /* 单条失败不阻断整批 */ }
    }
    renderTable();
    toast('success', `已清理 ${okCount} 项任务`);
  }

  /* ================= 任务详情 ================= */
  function showDetail(task) {
    const body = el('div.task-detail');

    const dl = el('dl.kv');
    const rows = [
      ['任务 ID', task.task_id],
      ['模块', MODULE_LABELS[task.module] || task.module || '—'],
      ['操作', task.operation || '—'],
      ['状态', null],  // 用胶囊渲染
      ['进度', task.status === 'success' ? '100%' : `${task.progress || 0}%`],
      ['创建时间', formatTime(task.created_at)],
      ['开始时间', task.started_at ? formatTime(task.started_at) : '—'],
      ['结束时间', task.finished_at ? formatTime(task.finished_at) : '—'],
      ['耗时', formatElapsed(task.created_at, task.finished_at)],
      ['过期时间', task.expire_at ? `${formatTime(task.expire_at)}（${formatRelative(task.expire_at)}）` : '—'],
    ];

    for (const [k, v] of rows) {
      dl.append(el('dt', { text: k }));
      if (k === '状态') {
        dl.append(el('dd', {}, [statusPill(task.status)]));
      } else {
        dl.append(el('dd', { text: String(v), class: k === '任务 ID' || k === '操作' ? 't-mono t-xs' : '' }));
      }
    }
    body.append(dl);

    // 输入文件
    if (task.inputs?.length) {
      body.append(el('div.form-section-title', { text: '输入文件' }));
      for (const input of task.inputs) {
        body.append(el('div.file-item', {}, [
          el('div.file-icon', { text: extOf(input.name).slice(0, 4) || '?' }),
          el('div.file-meta', {}, [
            el('div.file-name.t-clamp-1', { text: input.name }),
            el('div.file-sub', {}, [
              el('span', { text: formatBytes(input.size) }),
              input.sha256 ? el('span.t-mono', { text: `SHA256:${input.sha256}` }) : null,
            ]),
            // 视频下载任务没有本地文件：source 是作品长链，展示出来便于追溯来源
            input.source
              ? el('div.file-sub', {}, [
                  el('span.t-xs.t-muted', { text: '来源' }),
                  el('span.t-xs.t-mono.t-break', { text: input.source, title: input.source }),
                ])
              : null,
          ]),
        ]));
      }
    }

    // 输出文件
    if (task.output) {
      body.append(el('div.form-section-title', { text: '输出文件' }));
      body.append(el('div.file-item.is-ok', {}, [
        el('div.file-icon', { text: extOf(task.output.name).slice(0, 4) }),
        el('div.file-meta', {}, [
          el('div.file-name.t-clamp-1', { text: task.output.name }),
          el('div.file-sub', {}, [el('span', { text: formatBytes(task.output.size) })]),
        ]),
        el('button.btn.btn--sm', {
          type: 'button',
          on: { click: () => downloadTask(task) },
        }, [icon('download'), el('span', { text: '下载' })]),
      ]));
    }

    // 失败原因
    if (task.status === 'failed' && task.error) {
      body.append(el('div.alert.alert--danger', { role: 'alert' }, [
        icon('alertCircle'),
        el('div.alert-body', {}, [
          el('div.alert-title', { text: '失败原因' }),
          el('div', { text: task.error }),
        ]),
      ]));
    }

    modal({
      title: '任务详情',
      size: 'wide',
      body,
      actions: [{ label: '关闭', variant: 'secondary' }],
    });
  }

  /* ================= 生命周期 ================= */
  return {
    onShow() {
      renderTable();
      // 进入视图时做一次全量对齐，保证刷新页面后能看到历史任务
      syncFromServer(false);
      renderSummary();
    },
    async unmount() {
      state.autoRefresh = false;
      for (const dispose of disposers) {
        try { dispose(); } catch { /* 忽略 */ }
      }
      disposers.length = 0;
    },
  };
}
