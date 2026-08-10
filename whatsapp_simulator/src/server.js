const express = require('express');
const statusRoutes = require('./routes/status');
const messageRoutes = require('./routes/messages');
const sessionRoutes = require('./routes/session');

function buildServer() {
  const app = express();
  app.use(express.json({ limit: '25mb' }));

  app.use(statusRoutes);
  app.use(messageRoutes);
  app.use(sessionRoutes);

  app.use((req, res) => {
    res.status(404).json({ error: 'not found' });
  });

  app.use((err, req, res, next) => {
    console.error('[server] unhandled error:', err);
    res.status(500).json({ error: err.message || 'internal error' });
  });

  return app;
}

module.exports = { buildServer };
