// ==UserScript==
// @name         139云盘 AI解题 - 配置导出助手
// @namespace    ai-solve-proxy
// @version      1.0
// @description  在已登录的139云盘页面，一键导出 AI解题代理的 config.json（token + userDomainId）
// @match        https://yun.139.com/archive-book-h5/*
// @match        https://yun.139.com/archive-book/*
// @match        https://yun.139.com/*
// @grant        GM_setClipboard
// @run-at       document-idle
// @noframes
// ==/UserScript==

(function () {
  'use strict';
  const KEY = 'archive-book_login_tokenInfo';

  function readLogin() {
    try {
      const raw = localStorage.getItem(KEY);
      if (!raw) return null;
      const d = JSON.parse(raw).data || {};
      if (!d.token) return null;
      return { token: d.token, userDomainId: d.userDomainId || '', account: d.account || '' };
    } catch (e) { return null; }
  }

  function mask(s) {
    if (!s) return '';
    return s.length > 24 ? s.slice(0, 16) + '…' + s.slice(-8) : s;
  }

  function makeConfig(cfg) {
    const c = { token: cfg.token, userDomainId: cfg.userDomainId };
    if (cfg.account) c.account = cfg.account;
    return JSON.stringify(c, null, 2);
  }

  function downloadConfig(cfg) {
    const blob = new Blob([makeConfig(cfg)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'config.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    setStatus('✅ config.json 已下载，放到项目目录即可');
  }

  async function copyText(text) {
    try {
      if (typeof GM_setClipboard === 'function') GM_setClipboard(text);
      else await navigator.clipboard.writeText(text);
      return true;
    } catch (e) { return false; }
  }

  async function copyConfig(cfg) {
    setStatus(await copyText(makeConfig(cfg)) ? '✅ 配置已复制' : '❌ 复制失败');
  }

  function autoCopyIfEnabled(cfg) {
    try {
      if (localStorage.getItem('axp_auto_copy') === '1' && cfg) {
        copyText(makeConfig(cfg)).then(ok => setStatus(ok ? '🔄 已自动复制配置到剪贴板' : ''));
      }
    } catch (e) {}
  }

  function setStatus(msg) {
    const el = document.getElementById('axp-status');
    if (el) el.textContent = msg;
  }

  function refresh() {
    const cfg = readLogin();
    const info = document.getElementById('axp-info');
    const btnBox = document.getElementById('axp-btns');
    const auto = document.getElementById('axp-auto');
    if (!cfg) {
      info.innerHTML = '<div style="color:#e5484d;font-weight:600;">✗ 未检测到登录态</div>' +
        '<div style="color:#888;font-size:12px;margin-top:4px;">请先在 yun.139.com/archive-book-h5/ 登录移动云盘账号</div>';
      btnBox.style.display = 'none';
      setStatus('');
      return;
    }
    info.innerHTML =
      '<div style="color:#1fb57c;font-weight:600;">✓ 已登录</div>' +
      '<div class="axp-row">账号：<b>' + (cfg.account || '-') + '</b></div>' +
      '<div class="axp-row">userDomainId：<b>' + cfg.userDomainId + '</b></div>' +
      '<div class="axp-row">token：<span class="axp-mono">' + mask(cfg.token) + '</span></div>';
    btnBox.style.display = 'flex';
    auto.checked = localStorage.getItem('axp_auto_copy') === '1';
    setStatus('就绪');
    autoCopyIfEnabled(cfg);
  }

  function buildUI() {
    const style = document.createElement('style');
    style.textContent = `
      #axp-fab { position:fixed; right:18px; bottom:90px; z-index:999999; width:52px; height:52px; border-radius:50%;
        background:linear-gradient(135deg,#3b6ef6,#6a5cfc); color:#fff; font-size:24px; display:flex; align-items:center;
        justify-content:center; cursor:pointer; box-shadow:0 4px 16px rgba(59,110,246,.45); user-select:none; transition:transform .15s; }
      #axp-fab:hover { transform:scale(1.08); }
      #axp-panel { position:fixed; right:18px; bottom:150px; z-index:999999; width:330px; background:#fff; color:#222;
        border-radius:12px; box-shadow:0 8px 32px rgba(0,0,0,.18); padding:14px 16px; display:none; font-family:"PingFang SC","Microsoft YaHei",sans-serif; font-size:13px; }
      #axp-panel .axp-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }
      #axp-panel .axp-title { font-weight:700; font-size:14px; }
      #axp-close { cursor:pointer; color:#999; font-size:16px; padding:0 4px; }
      #axp-panel .axp-row { margin:4px 0; word-break:break-all; }
      #axp-panel .axp-mono { font-family:monospace; font-size:11px; color:#666; background:#f5f6fa; padding:1px 5px; border-radius:4px; }
      #axp-btns { display:flex; gap:8px; margin-top:12px; }
      #axp-btns button { flex:1; padding:8px 0; border:none; border-radius:8px; cursor:pointer; font-size:13px; color:#fff; }
      #axp-dl { background:#3b6ef6; } #axp-cp { background:#1fb57c; } #axp-tk { background:#555; }
      #axp-auto-row { margin-top:10px; font-size:12px; color:#888; display:flex; align-items:center; gap:6px; }
      #axp-status { margin-top:6px; color:#888; font-size:12px; min-height:16px; }
    `;
    document.head.appendChild(style);

    const fab = document.createElement('div');
    fab.id = 'axp-fab'; fab.textContent = '⚙'; fab.title = 'AI解题代理 - 导出配置';

    const panel = document.createElement('div');
    panel.id = 'axp-panel';
    panel.innerHTML = `
      <div class="axp-head"><span class="axp-title">⚙️ 导出代理配置</span><span id="axp-close">✕</span></div>
      <div id="axp-info"></div>
      <div id="axp-btns" style="display:none;">
        <button id="axp-dl">📥 下载 config.json</button>
        <button id="axp-cp">📋 复制配置</button>
        <button id="axp-tk">🔑 复制token</button>
      </div>
      <label id="axp-auto-row"><input type="checkbox" id="axp-auto"> 每次打开页面自动复制配置（可选项，默认关）</label>
      <div id="axp-status"></div>
    `;
    document.body.appendChild(fab);
    document.body.appendChild(panel);

    fab.onclick = () => { panel.style.display = panel.style.display === 'block' ? 'none' : 'block'; refresh(); };
    document.getElementById('axp-close').onclick = () => { panel.style.display = 'none'; };
    document.getElementById('axp-dl').onclick = () => { const c = readLogin(); if (c) downloadConfig(c); };
    document.getElementById('axp-cp').onclick = () => { const c = readLogin(); if (c) copyConfig(c); };
    document.getElementById('axp-tk').onclick = async () => {
      const c = readLogin();
      if (c) setStatus(await copyText(c.token) ? '✅ 已复制 token' : '❌ 复制失败');
    };
    document.getElementById('axp-auto').onchange = (e) => {
      localStorage.setItem('axp_auto_copy', e.target.checked ? '1' : '0');
    };
  }

  buildUI();
})();
