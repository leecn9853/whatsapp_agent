const express = require('express');
const whatsappClient = require('../whatsappClient');

const router = express.Router();

router.post('/session/logout', async (req, res, next) => {
  try {
    // 登出 + 清 .wwebjs_auth + 重新 init（换号扫码）
    await whatsappClient.logout();
    res.json({ ok: true, ...whatsappClient.getState() });
  } catch (err) {
    next(err);
  }
});

router.post('/session/restart', async (req, res, next) => {
  try {
    // 保留本地会话，同账号重连
    await whatsappClient.restart();
    res.json({ ok: true, ...whatsappClient.getState() });
  } catch (err) {
    next(err);
  }
});

module.exports = router;
