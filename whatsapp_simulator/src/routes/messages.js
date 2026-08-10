const express = require('express');
const { MessageMedia } = require('whatsapp-web.js');
const whatsappClient = require('../whatsappClient');

const router = express.Router();

function normalizeChatId(to) {
  return to.includes('@') ? to : `${to.replace(/[^\d]/g, '')}@c.us`;
}

// @lid 是 WhatsApp 隐藏号码隐私功能下的"关联 ID"，不是可直接寻址的真实号码。
// whatsapp-web.js 内部按 @lid 解析 chat 时经常找不到对应的 Chat/Wid，导致 sendMessage
// 静默返回 null（表现为我们这边的 502 "recipient may not exist"）。
// 这里在真正发送前，用官方提供的 getContactLidAndPhone 把 @lid 换成对应的真实号码
// （@c.us），换不到时才回退用原始 id 尝试（保底行为不变）。
async function resolveSendableId(client, chatId) {
  if (!chatId.endsWith('@lid')) return chatId;
  try {
    const [{ pn } = {}] = await client.getContactLidAndPhone([chatId]);
    return pn || chatId;
  } catch (err) {
    return chatId;
  }
}

function requireReadyClient(req, res) {
  const { state } = whatsappClient.getState();
  if (state !== whatsappClient.STATE.READY) {
    res.status(503).json({ error: `client not ready (state: ${state})` });
    return null;
  }
  return whatsappClient.getClient();
}

router.post('/messages', async (req, res, next) => {
  const { to, message } = req.body || {};
  try {
    if (!to || !message) {
      return res.status(400).json({ error: '"to" and "message" are required' });
    }
    const client = requireReadyClient(req, res);
    if (!client) return;

    const targetId = await resolveSendableId(client, normalizeChatId(to));
    const sent = await client.sendMessage(targetId, message);
    if (!sent) {
      console.error(`[messages] sendMessage 返回空，to=${to} targetId=${targetId}`);
      return res.status(502).json({ error: 'message failed to send (recipient may not exist on WhatsApp)' });
    }
    res.json({ id: sent.id?._serialized });
  } catch (err) {
    console.error(`[messages] sendMessage 抛出异常，to=${to}:`, err);
    next(err);
  }
});

router.post('/messages/media', async (req, res, next) => {
  const { to, mediaUrl, mediaBase64, mimetype, filename, caption } = req.body || {};
  try {
    if (!to || (!mediaUrl && !mediaBase64)) {
      return res.status(400).json({ error: '"to" and either "mediaUrl" or "mediaBase64" are required' });
    }
    const client = requireReadyClient(req, res);
    if (!client) return;

    const media = mediaUrl
      ? await MessageMedia.fromUrl(mediaUrl, { unsafeMime: true })
      : new MessageMedia(mimetype, mediaBase64, filename);

    const targetId = await resolveSendableId(client, normalizeChatId(to));
    const sent = await client.sendMessage(targetId, media, { caption });
    if (!sent) {
      console.error(`[messages/media] sendMessage 返回空，to=${to} targetId=${targetId} filename=${filename}`);
      return res.status(502).json({ error: 'message failed to send (recipient may not exist on WhatsApp)' });
    }
    res.json({ id: sent.id?._serialized });
  } catch (err) {
    console.error(`[messages/media] sendMessage 抛出异常，to=${to} filename=${filename}:`, err);
    next(err);
  }
});

router.get('/chats', async (req, res, next) => {
  try {
    const client = requireReadyClient(req, res);
    if (!client) return;

    const chats = await client.getChats();
    res.json(
      chats.map((chat) => ({
        id: chat.id._serialized,
        name: chat.name,
        isGroup: chat.isGroup,
        unreadCount: chat.unreadCount,
        timestamp: chat.timestamp,
      }))
    );
  } catch (err) {
    next(err);
  }
});

router.get('/chats/:chatId/messages', async (req, res, next) => {
  try {
    const client = requireReadyClient(req, res);
    if (!client) return;

    const limit = Number(req.query.limit) || 50;
    const chat = await client.getChatById(req.params.chatId);
    const messages = await chat.fetchMessages({ limit });
    res.json(
      messages.map((m) => ({
        id: m.id?._serialized,
        from: m.from,
        to: m.to,
        body: m.body,
        type: m.type,
        hasMedia: m.hasMedia,
        timestamp: m.timestamp,
      }))
    );
  } catch (err) {
    next(err);
  }
});

module.exports = router;
