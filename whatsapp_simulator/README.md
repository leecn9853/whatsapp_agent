# whatsapp_simulator

基于 [whatsapp-web.js](https://github.com/wwebjs/whatsapp-web.js) 的 Express 服务，把真实的 WhatsApp Web 会话（Puppeteer 驱动，需扫码登录）封装成 HTTP 接口 + Webhook 推送。

## 启动

```bash
cd whatsapp_simulator
npm install
cp .env.example .env
npm start
```

首次启动会打印二维码（也可 `GET /qr` 获取），扫码登录后会话持久化在 `.wwebjs_auth/`，重启无需再扫。

> Puppeteer 默认下载内置 Chromium（约 300MB）。想复用本机 Chrome，可在 `.env` 设置 `PUPPETEER_EXECUTABLE_PATH`。

## 环境变量（`.env`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `3000` | HTTP 服务端口 |
| `SESSION_PATH` | `./.wwebjs_auth` | 登录会话持久化目录 |
| `WEBHOOK_URL` | （空） | 设置后事件会 POST 到此地址 |
| `WEBHOOK_EXCLUDE_EVENTS` | `qr` | 不推送的事件，逗号分隔 |
| `MESSAGE_WHITELIST` | （空） | 只有这些号码/JID 发来的消息才推送 `message` 事件，逗号分隔，留空不限制 |
| `PUPPETEER_EXECUTABLE_PATH` | （空） | 复用本机 Chrome/Chromium 路径 |

## REST 接口

| 方法与路径 | 请求体 | 说明 |
|---|---|---|
| `GET /health` | – | 存活检查 |
| `GET /status` | – | `{ state, info }` |
| `GET /qr` | – | 二维码 PNG（无待扫码时 404） |
| `POST /messages` | `{ to, message }` | 发文本消息，`to` 可为裸手机号或完整 chatId |
| `POST /messages/media` | `{ to, mediaUrl?, mediaBase64?, mimetype?, filename?, caption? }` | 发媒体消息 |
| `GET /chats` | – | 会话列表 |
| `GET /chats/:chatId/messages?limit=50` | – | 某会话的最近消息 |
| `POST /session/logout` | – | 登出 |
| `POST /session/restart` | – | 重新初始化客户端 |

未进入 `READY` 状态时，`/messages` 与 `/messages/media` 返回 `503`。

```bash
curl -X POST http://localhost:3000/messages \
  -H 'Content-Type: application/json' \
  -d '{"to":"14155551234","message":"来自模拟器的问候"}'
```

## Webhook 事件

设置 `WEBHOOK_URL` 后，以下事件会 POST 为 `{ event, data, timestamp }`：

| 事件 | `data` |
|---|---|
| `qr` | `{ qr }` |
| `authenticated` | `{}` |
| `auth_failure` | `{ message }` |
| `ready` | `{ info }` |
| `disconnected` | `{ reason }` |
| `message` | `{ id, from, to, body, type, hasMedia, timestamp }` |
| `message_ack` | `{ id, to, ack }` |

推送失败自动重试 3 次，仍失败只记日志，不影响客户端本身。

## 运维：掉线排查

simulator 基于 WhatsApp Web 非官方协议，可能因 WhatsApp 更新或会话过期而掉线。用户发消息无响应时：

1. 检查连接状态：
   ```bash
   curl http://localhost:3000/status
   ```
   `state` 应为 `READY`；否则需要重新扫码或重启会话。

2. 获取二维码重新登录：
   ```bash
   curl http://localhost:3000/qr -o qr.png && open qr.png   # macOS
   ```
   或重启服务后看终端输出的二维码。

3. 重启会话（不删 `.wwebjs_auth/`）：
   ```bash
   curl -X POST http://localhost:3000/session/restart
   ```

4. 完全登出后重新扫码：
   ```bash
   curl -X POST http://localhost:3000/session/logout
   ```

生产环境可用 cron 定时 `curl /status`，`state` 非 `READY` 时通知运维（本仓库未内置告警，由部署侧自行配置）。
