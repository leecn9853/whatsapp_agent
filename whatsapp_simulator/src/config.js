require('dotenv').config({ quiet: true });

module.exports = {
  port: Number(process.env.PORT) || 3000,
  sessionPath: process.env.SESSION_PATH || './.wwebjs_auth',
  webhookUrl: process.env.WEBHOOK_URL || '',
  webhookExcludeEvents: (process.env.WEBHOOK_EXCLUDE_EVENTS ?? 'qr')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean),
  messageWhitelist: (process.env.MESSAGE_WHITELIST ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean),
  puppeteerExecutablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
  maxMediaSizeMB: Number(process.env.MAX_MEDIA_SIZE_MB) || 20,
};
