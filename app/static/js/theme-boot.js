/**
 * theme-boot.js — 主题预置（首屏关键路径）
 *
 * 为什么单独成文件而不是内联 <script>：
 * 后端会下发严格的 CSP（NFR-05）。内联脚本需要 'unsafe-inline' 或为每次
 * 构建计算 sha256 哈希；把这段逻辑放到同源静态文件里，CSP 就能保持
 * `script-src 'self'`，安全性更高且无需构建期哈希计算。
 *
 * 为什么必须同步加载（不能 defer）：
 * 它负责在样式表生效前设置 <html data-theme>，否则暗色偏好用户会先看到
 * 一帧白屏（FOUC / 主题闪烁）。体积 < 1 KB，代价可接受。
 */
(function themeBoot() {
  var STORAGE_KEY = 'toolkit.theme';
  var theme = 'auto';

  try {
    theme = window.localStorage.getItem(STORAGE_KEY) || 'auto';
  } catch (err) {
    // 隐私模式 / 禁用存储：退回 auto，不影响功能
    theme = 'auto';
  }

  var prefersDark = false;
  try {
    prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  } catch (err) {
    prefersDark = false;
  }

  var resolved = theme === 'auto' ? (prefersDark ? 'dark' : 'light') : theme;
  if (resolved !== 'dark' && resolved !== 'light') resolved = 'light';

  document.documentElement.setAttribute('data-theme', resolved);
})();
