const config = require('./config');

const MAX_ATTEMPTS = 3;
const RETRY_DELAY_MS = 500;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function dispatch(event, data) {
  if (!config.webhookUrl) {
    console.debug(`[webhook] WEBHOOK_URL not set, skipping dispatch for "${event}"`);
    return;
  }

  if (config.webhookExcludeEvents.includes(event)) {
    console.debug(`[webhook] "${event}" is excluded via WEBHOOK_EXCLUDE_EVENTS, skipping dispatch`);
    return;
  }

  const payload = JSON.stringify({ event, data, timestamp: Date.now() });

  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    try {
      const res = await fetch(config.webhookUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: payload,
      });
      if (!res.ok) {
        throw new Error(`webhook responded with status ${res.status}`);
      }
      return;
    } catch (err) {
      console.error(`[webhook] attempt ${attempt}/${MAX_ATTEMPTS} for "${event}" failed:`, err.message);
      if (attempt < MAX_ATTEMPTS) {
        await sleep(RETRY_DELAY_MS * attempt);
      }
    }
  }

  console.error(`[webhook] giving up dispatching "${event}" after ${MAX_ATTEMPTS} attempts`);
}

module.exports = { dispatch };
