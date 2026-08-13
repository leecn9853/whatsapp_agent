# Agent-Server 拆分 + Postgres 持久化迁移设计

状态：草案，待评审。落地前先备份整个项目，实施时在新文件/新模块里进行，不直接改现有文件。

> 2026-08-13 更新：下面第 1 节"背景"里的文件路径是写这份文档时（单进程 webhook）
> 的现状描述，之后已经先做了一次纯粹的目录重整（把 agent 相关代码挪到
> `src/agent/`，webhook 专属代码留在 `src/webhook/`，行为不变），当前实际路径见
> 第 3 节表格的"现有位置"列。

## 1. 背景

当前 `src/webhook/__init__.py` 的 Starlette `lifespan` 是唯一构造 agent 实例的地方：
它在事件循环启动后用 `AsyncSqliteSaver.from_conn_string(...)` 构造 checkpointer，
传给 `build_agent(checkpointer)`（当时在 `src/main.py`，现在是 `src/agent/main.py`），
结果存进全局单例 `_runtime.py::agent`（当时在 `src/webhook/`，现在是 `src/agent/`）。
`src/webhook/whatsapp.py`、`src/agent/admin.py` 都直接拿这个全局对象做同进程内的
`.astream()` / `.aget_state()` / `.checkpointer.adelete_thread()` 方法调用。

问题：
- agent 的生命周期被硬绑在 webhook 这一个 Starlette 进程的事件循环上，其他终端
  （CLI、其他消息渠道、内部管理后台）没有任何入口可以复用同一个 agent/对话状态。
- 之前尝试过 `langgraph dev`（仓库残留的 `.langgraph_api/` 缓存文件能证明），
  但 `langgraph dev` / LangGraph Platform 会自己接管 checkpointer/store 的构造与注入，
  和现在手写的 `AsyncSqliteSaver` + 自研 `SqliteStore` 命名空间方案冲突，所以
  `main.py` 里注释明确写了"agent 不注册为 LangGraph 平台图"——这条路先放弃，
  不再回头折腾。
- 现在的 checkpointer/store 都是 SQLite 单文件，多进程/多终端并发访问有
  "database is locked" 的隐患（`admin.py` 的模块注释里已经点出过这个风险）。

## 2. 目标架构

拆成两个独立进程：

```
其他终端（CLI / 未来渠道）
         │
         ▼
   agent-server（新增，FastAPI/Starlette）── owns ── Postgres
   ▲                                            （checkpoints / store / runs 三张表）
   │ HTTP/SSE
webhook（现有，改造为瘦客户端）
   │
   ▼
WhatsApp simulator
```

**agent-server**：
- 进程启动时构造一次 `AsyncPostgresSaver`、`AsyncPostgresStore`，调用 `build_agent(checkpointer, store)`，持有全局唯一 agent 实例。
- 对外暴露 HTTP API（见第 4 节），不感知任何 WhatsApp/媒体相关的概念。
- 吸收现在 `admin.py` 里的调试路由（对接的是 agent 内部状态，理应跟 agent 同进程）。
- 吸收现在 webhook 里按 `user_id` 做的**同线程串行锁**（`_user_locks`）——这是 agent 侧的正确性约束（同一 thread_id 不能并发写 checkpoint），不是 WhatsApp 特有逻辑，谁调用都需要这个保护，放在 agent-server 里对所有客户端统一生效。

**webhook**：
- 只保留 WhatsApp 协议相关的东西：接收/校验 webhook payload、媒体上传下载、组装
  progress 文案、把 agent-server 返回的结果/文件推给 WhatsApp simulator。
- 不再 `import src.main`、不再直接摸 checkpointer/store，只通过 HTTP 调 agent-server。
- 现在 `_invoke_with_retry`（按 attempt 超时重试、从 checkpoint 续跑）的逻辑整体挪到 agent-server 内部封装成一个"跑一轮对话"的端点，原因见第 4 节的 `POST /v1/threads/{thread_id}/runs`。

## 3. 现状 → 迁移后 组件对照

| 现有实现 | 现有位置 | 迁移后 |
|---|---|---|
| `build_agent(checkpointer)` | `src/agent/main.py` | 原样保留，新增 `store` 参数改成外部传入 |
| `AsyncSqliteSaver` | `src/webhook/__init__.py` lifespan | 换成 `AsyncPostgresSaver`，构造挪到 agent-server 的 lifespan |
| `SqliteStore`（自研） | `src/agent/stores/sqlite_store.py` | **整个删除**，换官方 `langgraph.store.postgres.aio.AsyncPostgresStore`（官方本来就有 Postgres 版，之前没有的只是 SQLite 版） |
| `RunsStore`（自研 SQLite） | `src/webhook/runs_store.py` | 迁到 Postgres，表结构不变，仍是自研（LangGraph 没有等价的官方"run 生命周期"存储给我们这种场景用） |
| `_runtime.agent` 全局单例 | `src/agent/_runtime.py` | 概念挪到 agent-server 内部，webhook 不再需要这个模块 |
| `_user_locks` | `src/webhook/whatsapp.py` | 挪到 agent-server，按 thread_id 加锁 |
| `admin.py` 全部路由 | `src/agent/admin.py` | 整体挪到 agent-server（本来就已经和 agent 代码放在一起），webhook 里不再引用 |
| `.astream/.aget_state/.checkpointer.adelete_thread` 方法调用 | `whatsapp.py` | 换成对 agent-server 的 HTTP/SSE 调用 |

## 4. agent-server API 设计（v1，草案）

| 方法 & 路径 | 作用 | 对应现有代码 |
|---|---|---|
| `POST /v1/threads/{thread_id}/runs` | 提交一条用户消息，服务端内部完成"按 attempt 超时重试 + 从 checkpoint 续跑"的整套逻辑，以 SSE 流式返回进度事件（工具调用名等）和最终事件（最后一条回复 + 本轮产出文件路径列表） | `_invoke_with_retry` + `_stream_attempt`（`whatsapp.py`） |
| `GET /v1/threads/{thread_id}/state` | 返回该 thread 当前 checkpoint 的完整消息列表 | `_runtime.agent.aget_state(config)` |
| `DELETE /v1/threads/{thread_id}` | 删除该 thread 的对话历史 | `_reset_thread` → `checkpointer.adelete_thread` |
| `GET /v1/threads` | 列出所有 thread_id 及 checkpoint 数（管理用） | `admin.py::admin_threads` |
| `GET /v1/memories/{namespace}` | 读取指定命名空间的长期记忆 | `admin.py::admin_memory` / `store.aget` |
| `GET /v1/memories` | 列出所有记忆命名空间 | `admin.py::admin_users` |

请求体里的 `caller`/`user_id`（对应现在的 `ContextSchema`）作为 `POST /runs` 的字段传入。

鉴权：agent-server 不再靠"只在 loopback 监听"这种网络层假设来保护调试接口
（一旦拆成两个进程，agent-server 可能需要监听非 loopback 地址给 webhook 访问），
需要一个简单的共享密钥（比如请求头 `X-Internal-Token`，双方从同一个环境变量读取）
做最低限度的内部服务间鉴权。**这是一个需要拍板的开放问题，见第 6 节。**

## 5. Postgres 迁移细节

新增依赖（`pyproject.toml`）：
- `langgraph-checkpoint-postgres`（带出 `psycopg[binary]` + `psycopg-pool`）
- 移除 `langgraph-checkpoint-sqlite`（迁移完成后不再需要）

需要的表（由 `AsyncPostgresSaver.setup()` / `AsyncPostgresStore.setup()` 自动建表，
不用手写 migration）：
- checkpoints 相关表（`AsyncPostgresSaver` 自带）
- store 相关表（`AsyncPostgresStore` 自带）
- `runs` 表（自研 `RunsStore`，把现在 SQLite 的 `CREATE TABLE runs (...)` 原样搬到 Postgres 语法，索引不变）

这里的 Docker 只用来起 **Postgres 数据库**，不是给 agent-server 本身用的——
agent-server 是普通的 Python/uvicorn 进程，启动方式和现在 `Makefile` 里的
`uv run uvicorn src.webhook:app ...` 一样（会新增一条 `uv run uvicorn
src.agent_server:app ...`），不需要 Docker。Postgres 装在本机还是用容器跑，
两种都可以，取决于你本机是否已经装了 Docker（见下方回复里的建议）。

本地开发建议用 `docker-compose.yml` 起一个 Postgres 服务（当前仓库没有任何
docker/compose 文件，这也是新增项）：

```yaml
services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_DB: excel_agent
      POSTGRES_USER: excel_agent
      POSTGRES_PASSWORD: <改成真实值，走 .env>
    ports: ["5432:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data"]
volumes:
  pgdata:
```

新增环境变量（`.env.example` 补充）：
- `DATABASE_URL`（形如 `postgresql://excel_agent:xxx@localhost:5432/excel_agent`），agent-server 用它构造 `AsyncPostgresSaver`/`AsyncPostgresStore`/`RunsStore`
- `AGENT_SERVER_URL`（webhook 用它调 agent-server，类比现有的 `WHATSAPP_SIMULATOR_URL`）
- `AGENT_SERVER_INTERNAL_TOKEN`（服务间鉴权共享密钥，双方都读同一个值）

旧数据（`data/checkpoints.sqlite`、`data/memory_store.sqlite`、`data/runs.sqlite`）
**不做自动迁移**，默认视为可丢弃、从 Postgres 空库重新开始——这是低成本假设，
如果需要保留历史对话/记忆，需要单独写一次性迁移脚本（读 SQLite 表，逐行
`INSERT` 进 Postgres 对应表），这属于第 6 节的开放问题，先问清楚要不要做。

## 6. 需要确认的开放问题

1. ~~**部署形态**~~ ✅ 已确认：同机部署，两个进程共享本机文件系统，文件产物走
   第 7 节的方案 A（共享磁盘），不做跨机器文件传输。
2. ~~**旧数据要不要迁移**~~ ✅ 已确认：不迁移，`data/*.sqlite` 直接废弃，
   Postgres 从空库开始。
3. **内部鉴权怎么做**：共享密钥 header 够不够，还是要更严格（mTLS/网络隔离）？
   补充说明这个 header 是给谁用的——不是给最终用户/WhatsApp 的，是 **webhook
   进程调用 agent-server 时带上的服务间凭证**。原因：现在 `admin.py` 靠"只在
   127.0.0.1 监听"这个网络层假设保护调试路由，拆成两个进程后 agent-server
   要能被 webhook 进程访问到（哪怕两者同机，也可能是分别监听不同端口、
   agent-server 监听 0.0.0.0 方便未来加其他终端），不能再假设"能连上就是
   自己人"，所以加一个双方共享的 token，webhook 发请求时带 `X-Internal-Token`
   header，agent-server 收到请求先校验这个值对不对，不对就拒绝。本机开发环境
   下如果 agent-server 明确只监听 127.0.0.1，这一步也可以先跳过，等真的要让
   其他终端从非本机访问时再补上。
4. **admin 调试页面**挪到 agent-server 后，还要不要保留"只允许 loopback 访问"
   这条规则？如果 agent-server 部署在容器里、外部只能通过反向代理访问，
   "loopback"这个判断可能失效，需要换成基于 `AGENT_SERVER_INTERNAL_TOKEN`
   或独立的管理端口。

## 7. 文件产物传输（依赖第 6.1 节的部署形态决定）

现状：`save_file`/`aggregate_excel_sheet`/`create_chart_sheet` 把文件写在项目根目录
`output/`（WhatsApp 场景下是 `output/<sanitize(user_id)>/`），`webhook/whatsapp.py`
直接用本地文件系统 `Path.read_bytes()` 读出来发给 WhatsApp simulator。

拆成两个进程后，如果 agent-server 和 webhook 不共享文件系统，webhook 拿到的
"本轮产出文件路径"就是 agent-server 那台机器上的路径，读不到。两个方案：

- **方案 A（默认，同机部署）**：两个进程挂载同一个 volume/共享同一份磁盘路径，
  webhook 收到的路径原样能读。改动量最小，先落地这个。
- **方案 B（跨机器）**：agent-server 额外暴露 `GET /v1/files/{token}` 按需下发
  文件字节，webhook 改成先调这个接口再转发给 WhatsApp simulator。等真的要跨机器
  部署时再做。

## 8. 建议的迁移步骤（分阶段，每阶段可独立验证）

1. 加 Postgres 依赖、起本地 docker-compose、验证 `AsyncPostgresSaver.setup()` /
   `AsyncPostgresStore.setup()` 能建表成功（不改动任何现有 webhook 代码）。
2. 新建 `src/agent_server/` 模块（新文件，不改现有 `src/webhook/`），把
   `build_agent` 的调用方式改成接收 `checkpointer` + `store` 两个参数
   （目前 `store` 是在 `main.py` 模块顶层直接构造的全局变量，需要一起挪成
   参数传入，和 `checkpointer` 保持同样的"由调用方在有事件循环时构造"模式）。
3. 在 agent-server 里实现第 4 节的 API，本地单独跑起来，用 `curl`/httpx 脚本
   验证一遍（不接 webhook）。
4. 改造 `src/webhook/whatsapp.py`：把直接方法调用换成对 agent-server 的
   HTTP/SSE 调用，删除 `_runtime.py`、`admin.py`、`sqlite_store.py`（改用
   Postgres 后自研 SQLite Store 不再需要）。
5. 两个进程一起跑通整条链路（WhatsApp → webhook → agent-server → Postgres），
   对照旧版本回归测试一轮现有的对话/文件生成场景。
6. 确认没问题后，删除旧的 `data/*.sqlite` 相关代码路径和 `langgraph-checkpoint-sqlite` 依赖。

## 9. 风险 / 回滚

- 每一步都是新增文件或者独立可验证的模块，第 1-3 步完全不触碰现有 webhook 代码，
  随时可以中止而不影响线上现有服务。
- 第 4 步开始改 `whatsapp.py` 才是真正的"切换"，建议保留旧的
  `_runtime.py` + 直接调用逻辑的分支（比如加一个环境变量开关），观察一段时间
  确认 agent-server 链路稳定后再彻底删除旧代码。
