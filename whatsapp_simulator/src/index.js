const config = require('./config');
const { buildServer } = require('./server');
const whatsappClient = require('./whatsappClient');

const app = buildServer();

app.listen(config.port, () => {
  console.log(`[whatsapp_simulator] HTTP server listening on port ${config.port}`);
});

whatsappClient.init();

async function shutdown(signal) {
  console.log(`[whatsapp_simulator] received ${signal}, shutting down gracefully...`);
  await whatsappClient.shutdown();
  process.exit(0);
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
