/**
 * boot-guard.js — 启动守卫
 *
 * 【为什么必须是 classic script，而不是 ES Module】
 *   app.js 是 type="module"。浏览器对模块脚本执行严格的同源检查：
 *   当页面通过 file:// 打开时，模块的来源被视为 "null"，
 *   加载会被直接拦截（CORS policy 报错），且**不会**触发任何可见错误。
 *   结果是：外壳样式正常渲染，但导航和主内容区永远空着 —— 也就是"白屏"。
 *
 *   classic script 不受该限制，在 file:// 下依然能执行。
 *   因此这段"看门"逻辑必须独立于模块体系存在，才能在最坏情况下跑起来。
 *
 * 【职责边界】
 *   只做一件事：app.js 在预期时间内没有完成启动 → 显示可操作的排查指引。
 *   不包含任何业务逻辑，不依赖任何其他文件，不使用 import。
 *
 * 【正常路径开销】
 *   应用启动成功时会设置 window.__TOOLKIT_READY__，
 *   本脚本检测到后直接静默退出，不产生任何副作用。
 */
(function () {
  'use strict';

  /** 观察窗口。静态应用无网络依赖，4 秒远超正常首屏耗时；宁可稍等也不要误报。 */
  var TIMEOUT_MS = 4000;

  /** 极简 DOM 构建器：本文件不依赖 utils.js（模块在 file:// 下不可用） */
  function h(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      for (var key in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, key)) continue;
        var value = attrs[key];
        if (value === null || value === undefined) continue;
        if (key === 'text') node.textContent = value;
        else if (key === 'class') node.className = value;
        else node.setAttribute(key, value);
      }
    }
    if (children) {
      for (var i = 0; i < children.length; i++) {
        var child = children[i];
        if (child) node.appendChild(child);
      }
    }
    return node;
  }

  /** 一段可复制的命令（用 code 包裹，便于用户直接选中复制） */
  function cmd(text) {
    return h('code', { class: 'boot-cmd', text: text });
  }

  function step(children) {
    return h('li', null, children);
  }

  /**
   * 构建排查指引面板。
   * 按协议区分文案：file:// 是一类确定的问题，其余情况统一引导到控制台。
   */
  function buildPanel() {
    var isFile = window.location.protocol === 'file:';

    var items = isFile
      ? [
        step([
          h('span', { text: '双击项目里的 ' }),
          cmd('toolkit/start.bat'),
          h('span', { text: '（Windows）或运行 ' }),
          cmd('bash toolkit/start.sh'),
          h('span', { text: '，脚本会启动本地服务并自动打开浏览器。' }),
        ]),
        step([
          h('span', { text: '或在 ' }),
          cmd('toolkit/app/static'),
          h('span', { text: ' 目录下执行 ' }),
          cmd('python -m http.server 5000'),
          h('span', { text: '，然后访问 ' }),
          cmd('http://localhost:5000'),
          h('span', { text: '。' }),
        ]),
        step([
          h('span', { text: '不要直接双击 ' }),
          cmd('index.html'),
          h('span', { text: '：浏览器安全策略禁止 file:// 页面加载 ES Module，这是浏览器的限制，无法在前端绕过。' }),
        ]),
      ]
      : [
        step([
          h('span', { text: '按 ' }),
          cmd('F12'),
          h('span', { text: ' 打开控制台，查看 Console 与 Network 两个面板的报错。' }),
        ]),
        step([
          h('span', { text: '确认 ' }),
          cmd('js/'),
          h('span', { text: ' 与 ' }),
          cmd('css/'),
          h('span', { text: ' 目录与 ' }),
          cmd('index.html'),
          h('span', { text: ' 位于同一层级，且服务器返回的 ' }),
          cmd('.js'),
          h('span', { text: ' MIME 类型为 ' }),
          cmd('text/javascript'),
          h('span', { text: '。' }),
        ]),
        step([
          h('span', { text: '若通过反向代理访问，检查是否部署在子路径下，以及代理是否透传了静态文件。' }),
        ]),
      ];

    return h('div', { class: 'boot-panel' }, [
      h('div', { class: 'boot-panel-head' }, [
        h('span', { class: 'boot-panel-mark', 'aria-hidden': 'true', text: 'TK' }),
        h('div', null, [
          h('h1', { class: 'boot-panel-title', text: '页面未能启动' }),
          h('p', {
            class: 'boot-panel-sub',
            text: isFile
              ? '检测到当前是通过本地文件直接打开的，应用脚本被浏览器的安全策略拦截了。'
              : '应用脚本未在预期时间内完成加载。',
          }),
        ]),
      ]),

      h('h2', { class: 'boot-panel-h2', text: '解决办法' }),
      h('ol', { class: 'boot-list' }, items),

      h('details', { class: 'boot-details' }, [
        h('summary', { text: '诊断信息' }),
        h('dl', { class: 'boot-kv' }, [
          h('dt', { text: '访问协议' }),
          h('dd', { text: window.location.protocol }),
          h('dt', { text: '入口脚本' }),
          h('dd', { text: 'js/app.js（type="module"）' }),
          h('dt', { text: '启动标志' }),
          h('dd', { text: window.__TOOLKIT_READY__ ? '已就绪' : '未设置' }),
          h('dt', { text: '模块支持' }),
          h('dd', {
            text: 'HTMLScriptElement' in window && 'noModule' in document.createElement('script')
              ? '支持 type="module"'
              : '不支持 type="module"（浏览器过旧）',
          }),
        ]),
      ]),
    ]);
  }

  /** 应用未启动则揭示面板；已启动或已显示过则原地返回 */
  function reveal() {
    if (window.__TOOLKIT_READY__) return;

    var host = document.getElementById('boot-fallback');
    if (!host || host.dataset.shown === '1') return;
    host.dataset.shown = '1';

    // 隐藏空壳外壳：留着只会让人以为是样式坏了，反而掩盖真正的原因
    var shell = document.querySelector('.app');
    if (shell) shell.hidden = true;

    host.hidden = false;
    host.appendChild(buildPanel());

    if (host.dataset.shown === '1') {
      console.error('[boot-guard] 应用未在 ' + TIMEOUT_MS + 'ms 内完成启动，已显示排查指引。'
        + (window.location.protocol === 'file:' ? ' 原因：file:// 协议下 ES Module 被浏览器拦截。' : ''));
    }
  }

  function schedule() {
    // 模块脚本是 defer 语义，会在 DOMContentLoaded 之后才执行；
    // 因此必须在 DOMContentLoaded 之后再起计时，否则会误判为"未启动"。
    window.setTimeout(reveal, TIMEOUT_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', schedule, { once: true });
  } else {
    schedule();
  }
})();
