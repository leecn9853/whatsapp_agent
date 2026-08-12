const fs = require('fs');
const path = require('path');
const config = require('./config');

function hasExistingSession() {
  return fs.existsSync(path.join(path.resolve(config.sessionPath), 'session'));
}

function normalizeNumber(id) {
  return (id || '').replace(/\D/g, '');
}

function isWhitelisted(senderPhone) {
  if (config.messageWhitelist.length === 0) return true;
  const sender = normalizeNumber(senderPhone);
  return config.messageWhitelist.some((entry) => normalizeNumber(entry) === sender);
}

// @lid 是隐藏号码隐私模式下的匿名会话 id，同一联系人重新登录或换设备后可能变化，
// 不适合作为 agent 侧会话线程/checkpoint 的 key，也不能直接用于 sendMessage 寻址。
// 这里换成稳定的真实号码（@c.us）再使用。
async function resolveLidToPhone(client, id) {
  if (!id || !id.endsWith('@lid')) return id;
  try {
    const [{ pn } = {}] = await client.getContactLidAndPhone([id]);
    return pn || id;
  } catch (err) {
    console.error('[utils] 解析 @lid 真实号码失败:', err);
    return id;
  }
}

function normalizeChatId(to) {
  return to.includes('@') ? to : `${to.replace(/[^\d]/g, '')}@c.us`;
}

module.exports = {
  hasExistingSession,
  normalizeNumber,
  isWhitelisted,
  resolveLidToPhone,
  normalizeChatId,
};
