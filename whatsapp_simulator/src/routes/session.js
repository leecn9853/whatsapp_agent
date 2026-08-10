const express = require('express');
const whatsappClient = require('../whatsappClient');

const router = express.Router();

router.post('/session/logout', async (req, res, next) => {
  try {
    await whatsappClient.logout();
    res.json({ ok: true });
  } catch (err) {
    next(err);
  }
});

router.post('/session/restart', async (req, res, next) => {
  try {
    await whatsappClient.restart();
    res.json({ ok: true });
  } catch (err) {
    next(err);
  }
});

module.exports = router;
