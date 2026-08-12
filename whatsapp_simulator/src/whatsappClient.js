const qrcode = require('qrcode');
const qrcodeTerminal = require('qrcode-terminal');
const { Client, LocalAuth } = require('whatsapp-web.js');
const config = require('./config');
const webhook = require('./webhook');
const { hasExistingSession, isWhitelisted, resolveLidToPhone } = require('./utils');

const STATE = {
  INITIALIZING: 'INITIALIZING',
  QR_PENDING: 'QR_PENDING',
  AUTHENTICATED: 'AUTHENTICATED',
  READY: 'READY',
  DISCONNECTED: 'DISCONNECTED',
  AUTH_FAILURE: 'AUTH_FAILURE',
};

let client = null;
let state = STATE.INITIALIZING;
let lastQrDataUrl = null;
let lastInfo = null;
let consecutiveInitFailures = 0;
let hadExistingSession = false;
let sessionStartTimestamp = 0;

function buildClient() {
  return new Client({
    authStrategy: new LocalAuth({ dataPath: config.sessionPath }),
    puppeteer: {
      executablePath: config.puppeteerExecutablePath,
      args: ['--no-sandbox', '--disable-setuid-sandbox'],
    },
  });
}

function registerEvents(c) {
  c.on('qr', async (qr) => {
    state = STATE.QR_PENDING;
    consecutiveInitFailures = 0;
    if (hadExistingSession) {
      console.warn('[whatsappClient] 缓存的登录态已失效，需要重新扫码登录');
      hadExistingSession = false;
    }
    lastQrDataUrl = await qrcode.toDataURL(qr);
    qrcodeTerminal.generate(qr, { small: true });
    webhook.dispatch('qr', { qr });
  });

  c.on('authenticated', () => {
    state = STATE.AUTHENTICATED;
    consecutiveInitFailures = 0;
    lastQrDataUrl = null;
    webhook.dispatch('authenticated', {});
  });

  c.on('auth_failure', (message) => {
    state = STATE.AUTH_FAILURE;
    webhook.dispatch('auth_failure', { message });
  });

  c.on('ready', () => {
    state = STATE.READY;
    lastInfo = c.info || null;
    if (hadExistingSession) {
      console.log(
        `[whatsappClient] 使用缓存的登录态，当前账号: ${lastInfo?.pushname || ''} (${lastInfo?.wid?.user || ''})`
      );
      hadExistingSession = false;
    }
    webhook.dispatch('ready', { info: lastInfo });
  });

  c.on('disconnected', (reason) => {
    state = STATE.DISCONNECTED;
    webhook.dispatch('disconnected', { reason });
  });

  c.on('message', async (message) => {
    if (message.timestamp && message.timestamp < sessionStartTimestamp) {
      console.debug(
        `[whatsappClient] 跳过重连补发的历史消息: ${message.id?._serialized}`
      );
      return;
    }
    const from = await resolveLidToPhone(c, message.from);
    const senderId = message.author || message.from;
    const senderPhone = senderId === message.from ? from : await resolveLidToPhone(c, senderId);

    if (!isWhitelisted(senderPhone)) {
      console.debug(`[whatsappClient] 发送者不在白名单，跳过推送: ${senderPhone}`);
      return;
    }

    const payload = {
      id: message.id?._serialized,
      from,
      to: message.to,
      body: message.body,
      type: message.type,
      hasMedia: message.hasMedia,
      timestamp: message.timestamp,
    };

    // 目前只处理以「文件」形式发送的附件（type === 'document'）；图片/语音/贴纸等
    // 其它带媒体的消息类型不下载、不转发，避免无意义的下载流量。
    if (message.type === 'document' && message.hasMedia) {
      try {
        const media = await message.downloadMedia();
        const sizeBytes = Buffer.byteLength(media.data, 'base64');
        if (sizeBytes > config.maxMediaSizeMB * 1024 * 1024) {
          console.warn(
            `[whatsappClient] 文件超出大小限制 (${sizeBytes} bytes > ${config.maxMediaSizeMB}MB)，跳过转发: ${media.filename}`
          );
          payload.mediaError = 'too_large';
        } else {
          payload.media = {
            mimetype: media.mimetype,
            filename: media.filename,
            data: media.data,
          };
        }
      } catch (err) {
        console.error('[whatsappClient] 下载文件失败:', err);
        payload.mediaError = 'download_failed';
      }
    }

    webhook.dispatch('message', payload);
  });

  c.on('message_ack', (message, ack) => {
    webhook.dispatch('message_ack', {
      id: message.id?._serialized,
      to: message.to,
      ack,
    });
  });
}

const INIT_RETRY_DELAY_MS = 5000;

function init() {
  hadExistingSession = hasExistingSession();
  sessionStartTimestamp = Math.floor(Date.now() / 1000);
  client = buildClient();
  registerEvents(client);
  state = STATE.INITIALIZING;
  client.initialize().catch(async (err) => {
    consecutiveInitFailures += 1;
    console.error(
      '[whatsappClient] initialize failed (attempt',
      consecutiveInitFailures,
      '), retrying in',
      INIT_RETRY_DELAY_MS,
      'ms:',
      err.message
    );
    state = STATE.DISCONNECTED;
    await client.destroy().catch(() => {});
    setTimeout(init, INIT_RETRY_DELAY_MS);
  });
  return client;
}

function getClient() {
  return client;
}

function getState() {
  return { state, info: lastInfo };
}

function getQr() {
  return lastQrDataUrl;
}

async function logout() {
  if (!client) return;
  await client.logout();
  lastInfo = null;
  state = STATE.DISCONNECTED;
}

async function restart() {
  if (client) {
    await client.destroy().catch(() => {});
  }
  init();
}

async function shutdown() {
  if (client) {
    await client.destroy().catch(() => {});
  }
}

module.exports = { STATE, init, getClient, getState, getQr, logout, restart, shutdown };
