# 多渠道接入设计（WhatsApp / toC / toB）

## 背景

目前 agent-server 只有 WhatsApp webhook 是正式对外的入口，`routes/runs.py`、`routes/threads.py`、
`routes/memories.py` 是靠 `local_only` 锁在本机的调试用 API（三个文件都是迁移前的旧位置，迁移后已不存在）。
现在要新增两类正式使用者：

- **toC**：给最终用户用的 Web 前端，账号+密码登录
- **toB**：给其它后端系统调用的 API；目前实际使用者是内部开发/维护人员，还没有真正的外部调用方，
  另外需要一个内部查看页面，看 thread 列表/对话历史/run 记录

三类调用方（WhatsApp / toC / toB）分开维护，鉴权、请求响应格式各自独立演进，但底层跑 agent 的引擎只有一份，且
所有共用代码集中放在一个目录下，`agent_server/` 顶层只留入口聚合，不再散落 `_engine.py`/`_runtime.py` 之类的文件。

## 目录结构

```
agent_server/
  __init__.py          # 入口：聚合 channels/*/routes.py，挂到 app 上
  shared/               # 三个渠道共用的一切，渠道目录不允许重新实现这里的逻辑
    engine.py           # 原 _engine.py：run_agent_turn，重试/续跑/工具调用事件提取
    runtime.py          # 原 _runtime.py：agent/store/pool/lock_for 等运行时对象
    runs_store.py       # run 记录存储
    thread_ids.py        # 三个渠道统一的 thread_id 前缀方案（whatsapp:/toc:/tob:）
    memory.py            # 按 thread_id 查长期记忆（/memories/ 写入的 AGENTS.md），tob/admin.py 和未来 toC 复用
    files.py
    security.py         # local_only（保留给内部调试用）+ 新增的密码校验 helper
  channels/
    whatsapp/           # 现 routes/webhook.py + whatsapp/ 整体迁入，行为不变
      routes.py
      client.py
      processor.py
    toc/                # 新增：面向 Web 前端用户
      routes.py         # LangGraph Server 协议（方案A）
      auth.py           # 账号+密码登录、发会话 token
    tob/                # 新增：面向其它后端系统 + 内部查看页面
      routes.py         # 直接沿用现在 threads.py/runs.py 的语义
      admin.py          # 内部查看页面：thread 列表/对话历史/run 记录/长期记忆，只读
```

**原则：`channels/*` 目录下只放"解析请求 → 鉴权 → 调用 `shared/engine.py` → 编码响应"这层薄适配代码。**
重试/续跑逻辑永远只有 `shared/engine.py` 一份；三个渠道各自的 `routes.py`/`auth.py` 不允许绕开它自己重跑一遍。

## 各渠道鉴权

| 渠道 | 鉴权方式 | 归属维度 |
|---|---|---|
| WhatsApp | webhook 签名校验（现有） | 手机号 |
| toC | 账号 + 密码登录，登录成功后发一个会话 token（cookie 或 header 带的 token，具体存储/校验实现在 `channels/toc/auth.py`） | 登录账号的 user_id |
| toB | 暂不做鉴权，沿用现在 `local_only` 的内部信任边界（只能本机/内网访问）——toB 目前的实际使用者就是内部
开发/维护人员，还没有真正的外部调用方，先不为一个不存在的场景设计账号体系 | 调用方自己在请求里传的 `external_id` |

> 备注：常见的外部鉴权方案有 API Key（每个调用方各自一份密码，服务端按密码反查是谁）和 mTLS（双向证书校验，
> 基础设施更重）。等 toB 真的接入外部调用方时再选一种落地到 `channels/tob/`；到时候只改这一个目录，
> 不影响 `shared/`、也不影响本节下面的内部查看页面。

`local_only` 保留给内部调试用途，toC 的正式路由不再用它兜底；toB 目前仍然靠它兜底（见上表）。

## thread_id 命名空间

[main.py](../src/agent/main.py) 里 `/memories/` 按 `user_id`（= thread_id）做隔离，三个渠道各自生成
thread_id 的规则不同，为避免不同渠道的 id 互相碰撞，统一加渠道前缀：

- `whatsapp:<phone>`
- `toc:<user_id>`（`user_id` 来自 toC 账号体系，登录后拿到）
- `tob:<external_id>`（`external_id` 由调用方自己在请求里指定；toB 目前没有身份鉴权，这个前缀本身不提供
  隔离保证，只是和其它渠道的 id 区分开）

`tob/routes.py`、`tob/admin.py` 里的 `list_threads` 等管理接口据此可以直接看出记录属于哪个渠道。

**不做历史数据迁移**：加前缀之后，`checkpoints` 表（对话历史）和 store 里 `/memories/` 的旧 namespace
（都是裸手机号，没有前缀）直接作废清空，不写兼容判断、不做双写迁移。上线新版本前清空这两处旧数据即可。

## 长期记忆查看

原来 `/v1/memories*`（`routes/memories.py`）是一个不属于任何渠道的通用调试接口，谁都能查任意 thread 的
长期记忆，不做区分。这次收拢到渠道目录下：

- 查 namespace 的 escaping 规则（`thread_id.replace(".", "_")`）抽成 `shared/memory.py` 的
  `aget_memory(store, thread_id)`，避免各渠道各自拼一遍容易写错、也方便以后统一改规则。
- **toB**：`channels/tob/admin.py` 新增 `GET /v1/tob/admin/threads/{id}/memory`，内部查看页面详情页第三块
  展示任意 thread 的长期记忆，和对话历史、run 记录并列。走 `local_only`，和页面其它接口同一套信任边界。
- **WhatsApp**：不需要——没有界面消费这份数据，不单独开路由。
- **toC**（本轮不实现，留在这里记录）：以后如果要给用户加一个"查看我的记忆"的自助入口，`channels/toc/routes.py`
  应该复用 `shared/memory.py` 的 `aget_memory`，只允许查登录用户自己的 thread（不像 `tob/admin.py` 那样不做
  渠道/用户过滤），不要重新拼一遍 namespace escaping 规则。

## toC 协议：方案A（LangGraph Server 协议）

贴近 LangGraph Server 协议，`channels/toc/routes.py` 提供 `POST /threads`、
`POST /threads/{id}/runs/stream`，SSE 走标准事件类型（`values`/`messages/partial`/… ），前端直接用
`@langchain/react` 的 `useStream`，拿到现成的子agent面板、中断处理等组件。

`routes.py` 内部仍然调用 `shared/engine.py` 的 `run_agent_turn`，只是把它 yield 出来的
工具调用事件/`RunResult`/`RunFailed` 转成 LangGraph Server 协议格式，而不是现在 `runs.py` 里
`tool_call`/`error`/`done` 这套自定义格式——这层转换属于"编码响应"，按上面的原则放在 `toc/routes.py` 里，
不改 `shared/engine.py`。

## toB 接口范围：不新增接口

`channels/tob/routes.py` 把现在 `routes/threads.py`/`routes/runs.py`（迁移前的旧位置）的路由语义原样搬过去，
挂在 `/v1/tob/*` 下（不复用旧的 `/v1/threads/*` 路径，是为了给 toC 未来要用的 LangGraph Server 协议标准路径
`/threads`、`/threads/{id}/runs/stream` 留出干净的命名空间，避免两个渠道抢同一段 URL）：

- `GET /v1/tob/threads`（只列出 `tob:` 前缀的记录）
- `GET /v1/tob/threads/{external_id}/state`
- `DELETE /v1/tob/threads/{external_id}`
- `POST /v1/tob/threads/{external_id}/runs`（SSE 仍用现在 `tool_call`/`error`/`done` 这套自定义协议）

路径参数用调用方自己起的 `external_id`，不暴露内部 `tob:` 前缀方案。继续用 `local_only` 兜底。不做批量
提交等新接口，等真的有外部调用方提出需求、需要正式鉴权时再一起加。

## toB 查看页面

内部开发/维护人员用，看四类数据：thread 列表、某个 thread 的对话历史、该 thread 下的 run 记录
（状态/耗时/失败原因）、该 thread 的长期记忆（见上面「长期记忆查看」一节）。走 `local_only`，和
`tob/routes.py` 用同一套内部信任边界，不单独设计账号密码。

**交互结构**：一个列表页 + 一个详情页。

- 列表页：`GET /v1/tob/admin/threads` 现成可用，展示 thread_id + 消息数。
- 详情页（点进某个 thread）：同屏分三块——对话历史（`GET /v1/tob/admin/threads/{id}/state`）、
  run 记录列表（`GET /v1/tob/admin/threads/{id}/runs`，时间倒序：状态、attempt 次数、失败原因、
  起止时间）、长期记忆（`GET /v1/tob/admin/threads/{id}/memory`）。

**需要新增的读接口**：`runs_store.py` 目前只有写方法（`acreate_run`/`amark_*`），没有查询方法，
需要加：

- `alist_runs_for_thread(thread_id) -> list[dict]`：按 `user_id`（= thread_id）查 `runs` 表，
  配合已有的 `idx_runs_user_id_created_at` 索引，时间倒序返回
- 对应挂一个新路由 `GET /v1/tob/admin/threads/{id}/runs`，放在 `channels/tob/admin.py` 里（只读，`local_only`）

**页面实现**：不引入前端框架，`channels/tob/admin.py` 的 `GET /v1/tob/admin` 直接返回一个静态 HTML +
`fetch` 调用上面几个 JSON 接口拼页面，够用即可。

> 之后如果 toB 真的开放给外部调用方、或者这个查看页面要给运营/客服用（而不是内部开发/维护人员），
> 需要重新设计账号体系——那时候 `admin.py`（给人看的页面）和 `routes.py`（给外部系统调的 API）应该
> 分别接入各自合适的鉴权，不要合并成一套。

## 文件上传/下载

现在 [routes.py](../src/agent_server/channels/whatsapp/routes.py) 里的文件校验（只收 `.xlsx`/`.xls`）和
落盘逻辑是 WhatsApp 专属的，不在共享引擎里，三个渠道各自处理：

- **WhatsApp**：不变，沿用现在 `channels/whatsapp/routes.py` 的校验+落盘逻辑。
- **toC**：网页表单/multipart 上传，`channels/toc/routes.py` 自己解析 multipart body、校验扩展名、落盘，
  再调用 `shared/engine.py`。
- **toB**：暂不支持上传。只能通过消息文本触发对已存在文件的操作（比如引用 WhatsApp/toC 渠道已经上传过的文件），
  没有文件上传接口。如果后续 toB 调用方需要传文件，再单独设计（大概率是文件 URL/对象存储引用，而不是直传二进制）。

## 迁移步骤草案

1. ~~建 `shared/` 目录~~ **完成**：`_engine.py`→`shared/engine.py`、`_runtime.py`→`shared/runtime.py`、
   `runs_store.py`、原 `utils/` 下的 `files.py`/`security.py` 都直接扁平放进 `shared/`（不留 `utils/`
   子目录）；建 `channels/` 目录，把 `routes/webhook.py` + `whatsapp/` 迁到 `channels/whatsapp/`，行为不变
2. **本轮跳过**：`channels/toc/auth.py`（账号+密码、发会话 token）+ `channels/toc/routes.py`
   （LangGraph Server 协议 + multipart 文件上传）——等真的要做 toC 时再落地
3. ~~落地 `channels/tob/routes.py`~~ **完成**（原样搬 threads.py/runs.py 语义，继续用 `local_only`，
   不含文件上传）；给 `runs_store.py` 加了 `alist_runs_for_thread`，落地 `channels/tob/admin.py`（查看页面，
   含对话历史/run 记录/长期记忆三块）；原顶层 `routes/memories.py` 已删除，其能力收进
   `channels/tob/admin.py` + `shared/memory.py`
4. 给三个渠道的 thread_id 统一加前缀（**完成**，toC 的 `toc_thread_id` 留白未加，等做 toC 时再补）；
   **完成**：清空了旧的 `checkpoints`/`checkpoint_blobs`/`checkpoint_writes` 表数据和 store 里的旧
   `/memories/` 记录（`store` 表；本机没启用语义检索，没有 `store_vectors` 表），没清 `checkpoint_migrations`、
   `store_migrations`、`runs` 表（审计日志）
5. ~~`local_only` 明确保留为内部专用~~ **完成**（`tob/routes.py`、`tob/admin.py` 继续用它）；
   toC 的正式路由不再用它兜底（等做 toC 时生效）
