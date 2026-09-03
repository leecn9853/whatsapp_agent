const express = require('express');
const whatsappClient = require('../whatsappClient');

const router = express.Router();

router.get('/health', (req, res) => {
  res.json({ ok: true });
});

router.get('/status', (req, res) => {
  res.json(whatsappClient.getState());
});

/** data URL → PNG Buffer；非法则 null */
function dataUrlToPng(dataUrl) {
  const m = /^data:image\/png;base64,(.+)$/s.exec(dataUrl || '');
  if (!m) return null;
  return Buffer.from(m[1], 'base64');
}

router.get('/qr', (req, res) => {
  const qr = whatsappClient.getQr();
  if (!qr) {
    return res.status(404).json({
      error: 'no QR pending',
      hint: '未在待扫码状态；打开 /login，或 POST /session/logout 换号',
    });
  }

  // 仅 ?format=json 返回 data URL；浏览器默认拿 PNG
  if (req.query.format === 'json') {
    return res.json({ qr });
  }

  const png = dataUrlToPng(qr);
  if (!png) {
    return res.status(500).json({ error: 'invalid QR payload' });
  }
  res.set('Cache-Control', 'no-store');
  res.type('png').send(png);
});

router.get('/login', (req, res) => {
  const { state, info } = whatsappClient.getState();
  const hasQr = Boolean(whatsappClient.getQr());
  const name = info?.pushname || '';
  const phone = info?.wid?.user || '';
  const accountLine = name || phone
    ? `${escapeHtml(name)}${name && phone ? ' · ' : ''}${escapeHtml(phone)}`
    : '';

  // READY 时停掉自动刷新，避免打断操作；待扫码/初始化时自动刷新拿新码
  const autoRefresh = state !== 'READY' && state !== 'AUTH_FAILURE';

  let body;
  if (state === 'READY') {
    body = `
      <p class="ok">已登录${accountLine ? `：<strong>${accountLine}</strong>` : ''}</p>
      <div class="actions">
        <button type="button" id="btn-logout">退出并换号扫码</button>
        <button type="button" id="btn-restart" class="secondary">仅重连（同账号）</button>
      </div>
      <p class="hint">「退出并换号」会清除本地登录缓存并生成新二维码；「仅重连」保留当前账号会话。</p>`;
  } else if (hasQr) {
    body = `
      <img src="/qr?t=${Date.now()}" alt="QR" />
      <p class="hint">请用 WhatsApp → 已关联的设备 → 关联设备 扫码</p>
      <div class="actions">
        <button type="button" id="btn-logout" class="secondary">清除缓存重新出码</button>
      </div>`;
  } else {
    body = `
      <p>正在准备登录（${escapeHtml(state)}）…</p>
      <p class="hint">若长时间无二维码，可点下方按钮强制清缓存重试。</p>
      <div class="actions">
        <button type="button" id="btn-logout">清除缓存并重新出码</button>
        <button type="button" id="btn-restart" class="secondary">仅重启客户端</button>
      </div>`;
  }

  res.set('Cache-Control', 'no-store');
  res.type('html').send(`<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>WhatsApp 扫码登录</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 420px; margin: 2rem auto; padding: 0 1rem; text-align: center; color: #111; }
    img { width: 280px; height: 280px; background: #f3f3f3; }
    .state { margin: 0.75rem 0; color: #555; }
    .ok { margin: 1rem 0; }
    .hint { font-size: 0.9rem; color: #777; line-height: 1.4; }
    .actions { display: flex; flex-direction: column; gap: 0.6rem; margin: 1.25rem 0; }
    button { font: inherit; padding: 0.65rem 1rem; cursor: pointer; border: 1px solid #111; background: #111; color: #fff; border-radius: 6px; }
    button.secondary { background: #fff; color: #111; }
    button:disabled { opacity: 0.5; cursor: wait; }
    #msg { min-height: 1.2em; color: #b45309; font-size: 0.9rem; }
  </style>
</head>
<body>
  <h1>WhatsApp 扫码</h1>
  <p class="state">状态：<strong>${escapeHtml(state)}</strong></p>
  ${body}
  <p id="msg"></p>
  <script>
    const prevState = ${JSON.stringify(state)};
    async function post(path) {
      const msg = document.getElementById('msg');
      const buttons = document.querySelectorAll('button');
      buttons.forEach((b) => { b.disabled = true; });
      msg.textContent = '处理中…';
      try {
        const res = await fetch(path, { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || res.statusText);
        msg.textContent = '已提交，等待新状态…';
        for (let i = 0; i < 25; i++) {
          await new Promise((r) => setTimeout(r, 1000));
          const s = await fetch('/status').then((r) => r.json());
          if (s.state !== prevState || s.state === 'QR_PENDING' || s.state === 'READY') {
            location.reload();
            return;
          }
          msg.textContent = '等待中（' + s.state + '）…';
        }
        location.reload();
      } catch (e) {
        msg.textContent = '失败：' + (e.message || e);
        buttons.forEach((b) => { b.disabled = false; });
      }
    }
    const logoutBtn = document.getElementById('btn-logout');
    const restartBtn = document.getElementById('btn-restart');
    if (logoutBtn) logoutBtn.onclick = () => post('/session/logout');
    if (restartBtn) restartBtn.onclick = () => post('/session/restart');
    ${autoRefresh ? 'setTimeout(() => location.reload(), 15000);' : ''}
  </script>
</body>
</html>`);
});

function escapeHtml(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

module.exports = router;
