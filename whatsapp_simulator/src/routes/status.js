const express = require('express');
const whatsappClient = require('../whatsappClient');

const router = express.Router();

router.get('/health', (req, res) => {
  res.json({ ok: true });
});

router.get('/status', (req, res) => {
  res.json(whatsappClient.getState());
});

router.get('/qr', (req, res) => {
  const qr = whatsappClient.getQr();
  if (!qr) {
    return res.status(404).json({ error: 'no QR pending' });
  }
  res.json({ qr });
});

module.exports = router;
