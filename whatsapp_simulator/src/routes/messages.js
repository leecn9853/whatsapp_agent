const express = require('express');
const { MessageMedia } = require('whatsapp-web.js');
const whatsappClient = require('../whatsappClient');
const { normalizeChatId, resolveLidToPhone } = require('../utils');

const router = express.Router();

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

    const targetId = await resolveLidToPhone(client, normalizeChatId(to));
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

    const targetId = await resolveLidToPhone(client, normalizeChatId(to));
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
    const lidIds = chats.map((chat) => chat.id._serialized).filter((id) => id.endsWith('@lid'));

    const phoneByLid = new Map();
    if (lidIds.length) {
      try {
        const resolved = await client.getContactLidAndPhone(lidIds);
        resolved.forEach(({ lid, pn }) => {
          if (lid && pn) phoneByLid.set(lid, pn);
        });
      } catch (err) {
        console.error('[chats] getContactLidAndPhone 解析 @lid 真实号码失败:', err);
      }
    }

    res.json(
      chats.map((chat) => {
        const id = chat.id._serialized;
        return {
          id,
          phone: phoneByLid.get(id) || (id.endsWith('@c.us') ? id : null),
          name: chat.name,
          isGroup: chat.isGroup,
          unreadCount: chat.unreadCount,
          timestamp: chat.timestamp,
        };
      })
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
