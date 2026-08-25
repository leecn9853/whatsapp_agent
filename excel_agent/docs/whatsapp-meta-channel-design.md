# 新渠道：channels/whatsapp_meta（对接 Meta 官方 Cloud API）

## 背景

现有 `channels/whatsapp/` 对接的是内部 `whatsapp_simulator`（`WHATSAPP_SIMULATOR_URL`），走自定义的
`{"event": "message", "data": {...}}` 格式，媒体是网关帮忙下载好、以 base64 内嵌在 webhook 里推过来的。
这不是 Meta 官方 WhatsApp Cloud API 的协议。

本次要接的是真正的 Meta Cloud API（`graph.facebook.com/.../messages` + 官方 webhook 格式）。两边协议、鉴权、
媒体处理方式都不一样，**不做兼容层、不改 `channels/whatsapp/` 里任何代码**，新增一个独立目录
`channels/whatsapp_meta/`，只在 `shared/` 这一层复用：

- 复用：`shared/engine.py`（`run_agent_turn`，重试/续跑/工具调用事件）、`shared/runtime.py`（`agent`/`lock_for`）、
  `shared/runs_store.py`、`shared/files.py`
- 新增：`shared/thread_ids.py` 里加 `whatsapp_meta_thread_id(phone)`，前缀用 `wa_meta:`，跟现有 `whatsapp:`
  区分（同一个手机号在两个渠道各算一条独立线程，不共享对话历史/长期记忆）
- 不复用：`channels/whatsapp/client.py`、`routes.py`、`processor.py` 的具体实现——协议差太多，硬凑复用
  会变成一堆 `if is_meta: ... else: ...` 的补丁代码，直接各写各的更干净

## 对另一个 AI 给的方案的评价

它给的是教科书式的 Meta Cloud API 入门示例，思路（webhook 验签 handshake、收到消息立刻 200、异步跑
agent、`message.id` 去重）方向都对，但停在"能跑通 demo"的程度，生产环境要用会漏几个关键的坑：

| 遗漏点 | 问题 | 现有代码里已经有的参照 |
|---|---|---|
| POST 请求本身不做签名校验 | 示例只在 GET handshake 校验 `hub.verify_token`，POST 收消息时完全没校验来源，只要知道你的 webhook URL 就能伪造消息、白嫖你的 agent/LLM 调用 | 无，需新增（见下） |
| `message.id` 去重"生产环境再做" | Meta 的 webhook 投递本身就会重复（网络抖动、你这边响应稍慢），demo 阶段不做去重，测试时就会看到 agent 重复回复，容易误判为 bug | 无，需新增 |
| 媒体是 base64 内嵌 | Cloud API 的 webhook **只给 media id**，不给内容；拿到 id 后要额外发一次 `GET /{media-id}` 换一个几分钟就过期的临时 URL，再拿这个 URL + `Authorization: Bearer <token>` 单独下载。示例代码里"确保收到的是文本消息"那段完全没考虑图片/文件消息该怎么收 | `channels/whatsapp/routes.py` 里 media 已经是 base64，这次的下载两步流程得重新写 |
| 出站发图片/文件也是"直接传内容" | 示例只写了发文本；Cloud API 发媒体同样是两步：先 `POST /{phone_number_id}/media` 上传换 media id（或者用一个公网可访问的 https 链接），再在消息体里引用 id/链接，不能像 simulator 那样直接传 base64 | `channels/whatsapp/client.py` 的 `send_file` 是直传 base64，这次要重新写 |
| **24 小时会话窗口 / 消息模板** —— 最容易被忽略但最致命 | 只有用户在过去 24 小时内发过消息，你才能自由发文本回复；超过 24 小时，只能发"预先在 Meta 后台审批过的消息模板"（HSM），不能发任意文本。对一个 agent 来说：如果处理耗时很长、或者想主动推送提醒/异步通知，一旦跨过 24 小时窗口，普通文本消息会被 API 直接拒绝 | 完全没有对应逻辑，需要在 `processor.py` 里判断/在文档里明确这个约束 |
| access token 类型 | Meta 开发者后台默认给的是 24 小时过期的临时 token，示例直接硬编码成"填你的 token"，没提这个坑；服务端要长期跑，必须换成 System User 签发的永久 token | 无 |
| webhook payload 可能是批量的 | 示例只取 `entry?.[0]`/`changes?.[0]`/`messages?.[0]`，理论上这几层都是数组，且同一个 POST 里还可能混入 `statuses`（发出去的消息的已送达/已读状态回调），不是只有 `messages` | 需要遍历 + 按 value 里有没有 `messages`/`statuses` 字段分流 |

## Webhook（GET 验证 + POST 接收）

### GET：handshake 验证

跟示例思路一致，校验 `hub.verify_token` 后原样返回 `hub.challenge`（字符串，不要转成数字/JSON）。

### POST：签名校验（示例缺的部分）

Meta 用 App Secret 对整个原始请求体做 HMAC-SHA256，放在 `X-Hub-Signature-256` header 里（格式
`sha256=<hex>`）。校验时必须用**未被反序列化/重新序列化过的原始字节**去算签名——Starlette 里要在
`request.body()` 拿到的原始 bytes 上算，不能先 `request.json()` 再 `json.dumps` 回去比对（key 顺序/空格
不保证一致，会导致签名怎么都对不上）。校验失败直接 403，不进入后续解析。

### POST：payload 解析 + 去重 + 分流

结构大致是 `entry[].changes[].value`，`value` 下可能有：

- `messages[]`：用户发来的消息（文本/图片/文档/音频/视频/位置/交互式回复等，`type` 字段区分）
- `statuses[]`：你之前发出去的消息的状态回调（sent/delivered/read/failed），不是新消息，只用来更新发送状态，
  不触发 agent
- `errors[]`：投递失败原因

解析时要遍历 `entry`/`changes`（不要假设只有一条），按 `messages`/`statuses` 分流处理。

去重用 `messages[].id`（`wamid.xxx`）。MVP 阶段可以用一个进程内、带上限的 LRU/TTL 集合（比如
`cachetools.TTLCache`，按 wamid 存最近几分钟内处理过的即可，Meta 的重复投递一般发生在短时间内）；要不要
落 sqlite/redis 做跨进程重启持久化，看部署方式再定，不需要现在就上重量级方案。

### 立刻 200 + 后台任务

跟现有 `channels/whatsapp/routes.py` 的模式一致：路由函数里只做校验+解析+入队，`return 200` 之后再
`asyncio.create_task` 跑 `run_agent_turn`；`_background_tasks` 持有引用防 GC 这个模式直接照抄。

## 出站消息

### 文本消息：先判断 24 小时窗口

发消息前要知道这条线程"上一次收到用户消息"是什么时候：

- 窗口内：正常发 `type: text` 自由文本，跟示例的 `sendWhatsAppMessage` 逻辑一样（`POST
  /{phone_number_id}/messages`，Bearer token）
- 窗口外：不能发自由文本，得发 `type: template` 引用一个已经在 Meta 后台审批通过的模板（模板内容/变量
  在申请时就定好，不是运行时随便拼字符串）。本次不做模板申请/管理，只在代码里留一个清晰的报错/降级路径
  （比如记日志 + 不强行发送，而不是让 API 报错后被吞掉、用户完全收不到任何回复却不知道为什么）

判断"上次收到消息的时间"可以就用去重那张表/最近一条 inbound 消息的时间戳，不需要额外建表。

### 媒体：上传/下载都是两步

- **收图片/文件**：webhook 拿到 `media id` → `GET /{media-id}`（带 token）换临时 URL → `GET` 那个 URL（同样
  带 `Authorization: Bearer <token>`）下载字节，URL 几分钟内失效，拿到就马上下载，不要缓存 URL
- **发图片/文件**：`POST /{phone_number_id}/media`（multipart）上传换 media id → 消息体里 `{"type": "image",
  "image": {"id": "<media-id>"}}` 引用；或者如果文件本来就有公网可访问的 https 链接，可以直接用
  `{"link": "..."}` 跳过上传这一步

## thread_id 与鉴权归属

新增 `whatsapp_meta_thread_id(phone) -> f"wa_meta:{phone}"`，写进 `shared/thread_ids.py`，跟现有
`whatsapp_thread_id` 并列，不复用同一个前缀——即便同一个手机号，走 Meta 官方渠道和走 simulator 渠道也当
两条独立对话（历史、长期记忆都不共享），避免后续两个渠道并存时数据混在一起。

密钥：

- App Secret（算 webhook 签名用）、System User 永久 Access Token、Phone Number ID：三个都进 `.env`，
  跟现有 `WHATSAPP_SIMULATOR_URL` 平级放，不要复用同名变量
- 永久 token 需要在 Meta Business Manager 里建 System User 并授权对应 WhatsApp 资产后签发，不是开发者后台
  首页那个 24 小时临时 token——这一步是后台操作，不在代码范围内，但部署前必须换掉

## 目录结构（落地时）

```
channels/whatsapp_meta/
  routes.py       # GET handshake + POST 签名校验/解析/去重/分流/入队，风格照抄 channels/whatsapp/routes.py 的"立刻ack+后台任务"部分
  client.py       # 出站：send_text（判断24h窗口）、上传/发媒体、下载入站媒体，全部走 graph.facebook.com
  processor.py    # 调 shared/engine.run_agent_turn，推进度、发最终结果，跟 channels/whatsapp/processor.py 同构但独立实现
  dedup.py        # wamid 的 TTL 去重集合
```

`agent_server/__init__.py` 里跟现有 `_whatsapp_lifespan` 平级挂一个 `_whatsapp_meta_lifespan`（如果有需要
清理的后台任务）。

## 暂不确定 / 留白

- 技术栈：确定复用 `excel_agent` 现有 Python/Starlette + `shared/engine.py` 这套引擎，作为它的新 channel，
  不单独起 Node.js 服务
- 去重存储用进程内 TTL 集合还是 sqlite/redis：看后续实际部署方式（单进程/多副本）再定
- 消息模板的申请、内容、变量设计：不在这份文档范围内，跨 24 小时窗口时的降级行为先按"记日志、不静默失败"
  处理，模板体系需要时再单独设计
