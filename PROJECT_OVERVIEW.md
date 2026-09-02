# WhatsApp Excel Agent 项目总览

本文档描述本 monorepo 的目录结构、生产环境服务职责与相互关系，供新成员 onboarding、日常运维或 AI 辅助开发时快速建立全局认知。

**阅读指引**：

- 跑通主链路：看「生产服务一览」→「启动顺序」→「端口速查」
- 改 WhatsApp 消息处理：看「关键代码入口」中 `channels/whatsapp/`
- 改 Agent 工具/技能：看 `excel_agent/src/agent/`
- 本地一键启动：`make dev` 或 `make health`

---

## 仓库结构

```
whatsapp_agent/                 # monorepo 根目录
├── PROJECT_OVERVIEW.md         # 本文档（架构总览）
├── README.md                   # 快速入口
├── Makefile                    # make infra / agent / health / dev
├── excel_agent/                # ★ 核心生产服务：Excel/报表 Agent
├── whatsapp_simulator/         # ★ WhatsApp 接入网关
├── third_app/                  # ★ 报表技能 mock 数据源
└── content_agent/              # 实验性内容 Agent（非生产，见下文）
```

| 目录 | 角色 | 是否生产必需 |
|------|------|-------------|
| `excel_agent` | 核心 Agent：模型推理、工具/技能、会话持久化、多渠道 HTTP | **是** |
| `whatsapp_simulator` | WhatsApp 扫码登录、白名单、Webhook 桥接 | **是**（走 WhatsApp 渠道时） |
| `third_app` | 成本/支付宝报表技能的 HTTP 假数据 | **是**（跑报表技能时） |
| `content_agent` | 早期文字内容 Agent 原型（情商沟通 skill） | **否** |

---

## 项目定位

本项目是一个 **面向内部人员的 Excel / 报表 AI 助手**。白名单内的同事通过 WhatsApp 向指定机器人账号发消息（文字或 Excel 文件），即可触发 Agent 分析数据、生成图表，或调用预置技能（成本报表、支付宝流水报表等）产出图片/文件并回传。

**使用范围**：内部工具，非对外商业产品。用户规模小、身份可控，通过 `MESSAGE_WHITELIST` 限制只有指定号码能触发 Agent。

核心 AI 能力集中在 `excel_agent`；`whatsapp_simulator` 承担 **WhatsApp 接入层**；`third_app` 为报表技能提供模拟数据源。

---

## 当前阶段（Demo）

| 维度 | 现状 |
|------|------|
| 目标 | 内网跑通 WhatsApp → Agent → 报表/图表 主链路 |
| 部署 | 单机本地或单机服务器，服务用 `localhost` 互访 |
| 鉴权 | **未做** webhook / simulator API / third_app 鉴权（需求确定后再加） |
| 白名单 | demo 可留空（不限制号码）；上线前必须配置 `MESSAGE_WHITELIST` |
| third_app | 开发用 mock；上线报表技能时换真实业务 API |
| content_agent | 实验目录，忽略 |

生产加固（鉴权、防火墙、密钥轮换、systemd 编排等）**不在当前范围**，待产品需求明确后迭代。

---

## AI 技术栈（deepagents）

`excel_agent` 基于官方 [deepagents](https://github.com/langchain-ai/deepagents) 的 **`create_deep_agent()`** 标准 API 构建，不是 fork 或自研框架。主要组成：

| 能力 | 实现 |
|------|------|
| Agent 入口 | `create_deep_agent(model, tools, middleware, skills, memory, backend, …)` |
| 会话持久化 | LangGraph `AsyncPostgresSaver` + `AsyncPostgresStore` |
| 技能 | `skills=["/workspace/skills/"]`，沙箱内 `execute` 跑 CLI 脚本 |
| 长期记忆 | `memory=["/memories/AGENTS.md"]`，按 WhatsApp 用户隔离 |
| 子代理 | `web-search-agent`（联网调研） |
| 执行环境 | `DockerSandbox`（LibreOffice 报表）+ 宿主机 `input/`/`output/`（Excel 工具） |

业务定制中间件：`topic_gate`（跑题拦截）、`StructuredSummarizationMiddleware`（摘要落库）。代码入口：`excel_agent/src/agent/main.py` → `build_agent()`。

---

## WhatsApp 接入方案

**已确定：统一使用 `whatsapp_simulator`（whatsapp-web.js）作为 WhatsApp 接入方式。**

| 方案 | 状态 | 说明 |
|------|------|------|
| **whatsapp_simulator** | ✅ **采用** | 专用 WhatsApp 号扫码登录，白名单控制访问，支持 Excel 文件收发 |
| Meta Cloud API（`whatsapp_meta`） | 不采用 | 代码中保留但不在当前方案内使用 |
| 阿里云 Chat App 等 BSP | 不采用 | 内部工具无需此复杂度 |

**选型理由（内部场景）**：无 Meta 企业认证成本；白名单满足安全需求；支持 Excel 上传；用户规模小（数十人以内），非官方协议风险可接受。建议使用**专用工作号**，勿绑个人主号。

**运维注意**：simulator 依赖 WhatsApp Web 非官方协议，可能因 WhatsApp 更新掉线需重新扫码；会话持久化在 `.wwebjs_auth/`，需做好备份。

---

## 生产服务一览

| 服务 | 技术栈 | 默认端口 | 是否必需 | 一句话说明 |
|------|--------|----------|----------|------------|
| **excel_agent** | Python (Starlette + LangGraph + deepagents) | `8200` | **必需** | 核心 Agent：接收消息、跑模型、调工具/技能、持久化会话 |
| **whatsapp_simulator** | Node.js (Express + whatsapp-web.js) | `3000` | **必需**（WhatsApp 渠道） | WhatsApp 网关：扫码登录、白名单过滤、Webhook 双向通信 |
| **third_app** | Python (FastAPI) | `8800` | **必需**（报表技能） | 模拟第三方业务数据 API |

**运行时基础设施**（不算独立业务服务，但 `excel_agent` 硬依赖）：

| 组件 | 说明 |
|------|------|
| **Postgres** (`:5432`) | 会话 checkpoint、store、runs 表持久化 |
| **Docker sandbox** | LibreOffice 渲染报表截图；skill 脚本在容器内执行 |

**无 WhatsApp 的调试路径**：使用 `excel_agent` 的 `tob` 渠道（`POST /v1/tob/threads/{id}/runs` SSE 接口），无需启动 `whatsapp_simulator`。

---

## 配置说明（各服务独立 .env）

`whatsapp_simulator` 与 `excel_agent` 是**两个独立服务**，后期也会分开部署。它们**不需要共用一份配置文件**，各自维护 `whatsapp_simulator/.env` 与 `excel_agent/.env` 即可。

只需保证以下**对接参数**互相指向（其余变量各管各的）：

| whatsapp_simulator | excel_agent |
|--------------------|-------------|
| `WEBHOOK_URL` → agent 的 `/webhook` 地址 | `WHATSAPP_SIMULATOR_URL` → simulator 的 base URL |

示例（本地开发）：

```bash
# whatsapp_simulator/.env
WEBHOOK_URL=http://localhost:8200/webhook

# excel_agent/.env
WHATSAPP_SIMULATOR_URL=http://localhost:3000
```

`MESSAGE_WHITELIST` 只在 simulator 侧配置；`DEEPSEEK_API_KEY`、`DATABASE_URL` 等只在 excel_agent 侧配置。不要把两个服务的变量合并到一个 `.env` 里强行共享。

---

## 架构关系图

```
┌─────────────────────────────────────────────────────────────────────────┐
│              内部白名单用户（WhatsApp 客户端）                              │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │ 向专用机器人号发消息
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  whatsapp_simulator (:3000)                                              │
│  · whatsapp-web.js + Puppeteer 驱动 WhatsApp Web 会话（扫码登录）          │
│  · MESSAGE_WHITELIST：仅白名单号码触发 webhook                            │
│  · 入站：Webhook POST → excel_agent /webhook                             │
│  · 出站：excel_agent 调 POST /messages、/messages/media 回发消息/文件       │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │ WEBHOOK_URL=http://localhost:8200/webhook
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  excel_agent — agent-server (:8200)                                      │
│  ┌─────────────────┐  ┌──────────────────┐  ┌─────────────────────────┐ │
│  │ channels/       │  │ shared/engine.py │  │ agent/ (deepagents)     │ │
│  │ whatsapp（主）   │→ │ run_agent_turn   │→ │ LLM + 工具 + Skills     │ │
│  │ tob（调试）      │  │ runs_store       │  │ DockerSandbox.execute   │ │
│  └─────────────────┘  └──────────────────┘  └───────────┬─────────────┘ │
└──────────────────────────────┬───────────────────────────┼───────────────┘
                               │                           │
              ┌────────────────┴────────┐     ┌────────────┴────────────┐
              ▼                         ▼     ▼                         ▼
     ┌─────────────────┐      ┌─────────────────┐          ┌──────────────────┐
     │ Postgres (:5432) │      │ sandbox 容器     │          │ third_app (:8800) │
     │ checkpoint/store │      │ LibreOffice 渲染 │──HTTP──→│ 模拟订单/成本/     │
     │ runs 表          │      │ skills 脚本执行  │          │ 支付宝流水 API    │
     └─────────────────┘      └─────────────────┘          └──────────────────┘
```

---

## 关键代码入口

改代码时优先定位以下路径，避免全库扫描：

| 关注点 | 路径 |
|--------|------|
| HTTP 应用入口 | `excel_agent/src/agent_server/__init__.py`（`app` + lifespan） |
| 路由聚合 | `excel_agent/src/agent_server/channels/` |
| WhatsApp Webhook 入站 | `excel_agent/src/agent_server/channels/whatsapp/routes.py` |
| 消息处理与回发 | `excel_agent/src/agent_server/channels/whatsapp/processor.py`、`client.py` |
| Agent 单轮执行 | `excel_agent/src/agent_server/shared/engine.py` → `run_agent_turn` |
| Agent 本体与工具 | `excel_agent/src/agent/main.py`、`excel_agent/src/agent/tools/` |
| 预置技能 | `excel_agent/src/agent/skills/`（`cost-report`、`alipay-report`、`excel-chart`） |
| Docker 沙箱执行 | `excel_agent/src/agent/backends/docker_sandbox.py` |
| tob 调试 SSE | `excel_agent/src/agent_server/channels/tob/routes.py` |
| simulator 服务端 | `whatsapp_simulator/src/server.js` |
| third_app API | `third_app/main.py` → `third_app/src/third_app/server.py` |

---

## 各服务详细说明

### 1. excel_agent（核心 Agent 服务）

**目录**：`excel_agent/`

**职责**：

- **agent-server**（`src/agent_server`）：对外 HTTP 服务，聚合多渠道路由
- **agent 本体**（`src/agent`）：基于 deepagents + DeepSeek，提供 Excel 读写、图表生成、联网搜索、文件保存等工具
- **Skills**（`src/agent/skills/`）：
  - `cost-report`：从 third_app 拉取成本数据，沙箱内 LibreOffice 渲染报表截图
  - `alipay-report`：从 third_app 拉取支付宝匹配流水，生成报表图片
  - `excel-chart`：通用 Excel 图表生成说明
- **渠道适配**（`src/agent_server/channels/`）：
  - `whatsapp/`：**主渠道**，对接 simulator 的 Webhook + 出站 HTTP
  - `tob/`：内部调试 SSE API + admin 查看页
  - `whatsapp_meta/`：Meta Cloud API 实现，**当前不使用**，代码保留供参考

**关键环境变量**：`DEEPSEEK_API_KEY`、`TAVILY_API_KEY`、`DATABASE_URL`、`THIRD_APP_BASE_URL`、`WHATSAPP_SIMULATOR_URL`

**启动**：

```bash
cd excel_agent
uv sync
cp .env.example .env   # 编辑必填项
docker compose up -d postgres sandbox
make dev               # 监听 0.0.0.0:8200
```

---

### 2. whatsapp_simulator（WhatsApp 接入网关）

**目录**：`whatsapp_simulator/`

**职责**：

- 用 `whatsapp-web.js` 封装 WhatsApp Web 会话（首次需扫码，会话持久化在 `.wwebjs_auth/`）
- 提供 REST API：发文字/媒体、查会话、查状态、登出/重启
- 通过 `WEBHOOK_URL` 将 `message` 等事件 POST 到 Agent 的 `/webhook`
- **`MESSAGE_WHITELIST`**：只有白名单号码的消息才会推送给 Agent

**与 excel_agent 的对接**：

| 方向 | 机制 |
|------|------|
| 入站（用户 → Agent） | simulator Webhook → `excel_agent POST /webhook` |
| 出站（Agent → 用户） | `excel_agent` 调 `WHATSAPP_SIMULATOR_URL/messages` 或 `/messages/media` |

**推荐配置**（`whatsapp_simulator/.env`）：

```bash
PORT=3000
WEBHOOK_URL=http://localhost:8200/webhook
WEBHOOK_EXCLUDE_EVENTS=qr
MESSAGE_WHITELIST=85212345678,8613800138000
```

**启动**：

```bash
cd whatsapp_simulator
npm install
cp .env.example .env
npm start
# 首次扫码；或 GET http://localhost:3000/qr
```

**运维提示**：专用工作号；备份 `.wwebjs_auth/`；`GET /status` 查连接；群聊 `@g.us` 在 excel_agent 侧直接忽略。

---

### 3. third_app（模拟第三方数据服务）

**目录**：`third_app/`

**职责**：提供报表技能所需的假数据 API：

| 接口 | 用途 | 说明 |
|------|------|------|
| `GET /api/electronics/orders` | 成本报表 — 供应商采购 | 约 150 条 |
| `GET /api/food-agri/orders` | 成本报表 — 月度成本明细 | 分页，page_size ≤ 99 |
| `GET /api/alipay/matching-records` | 支付宝报表 — 匹配流水 | 按日缓存，page_size ≤ 500 |

**访问方式**：

- 宿主机脚本：`THIRD_APP_BASE_URL`（默认 `http://127.0.0.1:8800`）
- Docker 沙箱内：`http://host.docker.internal:8800`（`docker-compose.yml` 硬编码）

**启动**：

```bash
cd third_app
uv run python main.py   # 0.0.0.0:8800
```

> 不启动 third_app 不会导致 agent-server 启动失败，但 `cost-report` / `alipay-report` 会报「拉取数据失败」。

---

### 4. content_agent（实验性，非生产）

**目录**：`content_agent/`

**状态**：**不参与主链路**，无其他服务依赖它，可忽略除非在做内容类 Agent 实验。

这是 `excel_agent` 之前的**轻量原型**：同样基于 deepagents + DeepSeek，但架构更简单——单文件 Starlette webhook、SQLite 持久化、无 Docker 沙箱、无 Excel/报表能力。内置 `eq-communication` skill（情商与人际沟通类文字内容生成）。

| 对比项 | excel_agent（生产） | content_agent（实验） |
|--------|---------------------|----------------------|
| 端口 | `8200` | `8100` |
| 持久化 | Postgres | SQLite |
| 主要能力 | Excel 分析、报表技能 | 文字内容生成 |
| WhatsApp 对接 | 完整多渠道架构 | 独立 `src/webhook.py` |

若要将 simulator 临时指向它，需把 `WEBHOOK_URL` 改为 `http://localhost:8100/webhook`，且**不要与 excel_agent 同时占用同一机器人会话**。`README.md` 尚未编写，启动方式：`cd content_agent && make dev`。

---

## 典型消息处理链路

1. 白名单用户在 WhatsApp 向机器人号发送文字或 Excel 文件
2. `whatsapp_simulator` 校验白名单后，POST `{ event: "message", data: {...} }` 到 `excel_agent /webhook`
3. `channels/whatsapp/routes.py` 解析 payload：校验文件格式（仅 `.xlsx`/`.xls`）、落盘、创建 `run_id`
4. 后台 `processor.process_message` 调用 `shared/engine.run_agent_turn`
5. Agent 调用工具（读 Excel、聚合、画图）或 `execute` 跑 skill 脚本
6. 报表 skill 在沙箱内 HTTP 拉取 `third_app`，LibreOffice 渲染截图到 `output/`
7. `processor` 经 `whatsapp/client.py` 调 simulator 回发消息/文件

---

## 服务间依赖与启动顺序

**日常开发（推荐）**：打开 Docker Desktop → 仓库根目录 `make dev`（已包含 `docker compose up` + 三个业务服务）。

手动分步时：

```
1. docker compose     → postgres + sandbox（make infra）
2. third_app          → 报表 mock（跑报表技能时需要）
3. excel_agent        → make dev (:8200)
4. whatsapp_simulator → npm start (:3000)，首次扫码
```

**验证**：

```bash
curl http://127.0.0.1:8800/docs                                    # third_app
curl -N -X POST http://127.0.0.1:8200/v1/tob/threads/smoke-test/runs \
  -H "Content-Type: application/json" -d '{"message": "你好"}'        # excel_agent
curl http://127.0.0.1:3000/status                                    # simulator
make health                                                          # 或根目录一键检查
```

---

## 端口与环境变量速查

| 组件 | 端口 | 关键配置 |
|------|------|----------|
| excel_agent | `8200` | `DATABASE_URL`, `DEEPSEEK_API_KEY`, `TAVILY_API_KEY`, `WHATSAPP_SIMULATOR_URL` |
| whatsapp_simulator | `3000` | `WEBHOOK_URL`, `MESSAGE_WHITELIST` |
| third_app | `8800` | 无（开发 mock，无鉴权） |
| content_agent | `8100` | `DEEPSEEK_API_KEY`（实验，非生产） |
| Postgres | `5432` | `POSTGRES_PASSWORD` |
| Docker sandbox | — | 容器名 `excel_agent-sandbox-1`，无对外端口 |

**simulator 与 excel_agent 的 `.env` 需对齐**：

| whatsapp_simulator | excel_agent |
|--------------------|-------------|
| `WEBHOOK_URL=http://localhost:8200/webhook` | — |
| — | `WHATSAPP_SIMULATOR_URL=http://localhost:3000` |

---

## 已知设计约束

**Demo 阶段已知、可接受：**

- HTTP 接口（`/webhook`、simulator 发消息 API、`third_app`）**暂无鉴权**，仅适合本机/内网 demo
- `MESSAGE_WHITELIST` 留空时不限制号码（方便调试）

**业务规则：**

- **仅白名单用户可触发**（配置后）：非白名单消息被 simulator 静默忽略
- **群聊默认不回复**：`@g.us` 结尾的 JID 直接 ack，不触发 Agent
- **文件类型限制**：WhatsApp 渠道仅接受 `.xlsx`/`.xls`
- **单文件大小限制**：simulator 默认 `MAX_MEDIA_SIZE_MB=20`
- **sandbox 启动超时**：默认 30 秒，超时 agent-server 退出
- **报表技能与通用 Excel 工具分离**：cost/alipay 走固定 skill + third_app，不走 `inspect_excel` 探索流程
- **非官方 WhatsApp 协议**：内部小范围使用可接受，不建议对外大规模商用

**本地开发踩坑：**

- Mac 若装了 Homebrew `postgresql@18` 等，会占用 `5432`，导致连错库、报 `role "excel_agent" does not exist` → `brew services stop postgresql@18`
- simulator 本机无 Chrome 时需 `npm run install-browser`（见 `whatsapp_simulator/README.md`）

---

## 相关文档索引

| 文档 | 路径 | 内容 |
|------|------|------|
| **快速入口（先看这个）** | `README.md` | 首次初始化、日常 `make dev`、常见问题 |
| 架构与 Demo 约束 | `PROJECT_OVERVIEW.md` | 本文档 |
| excel_agent 启动与排障 | `excel_agent/README.md` | 环境变量、健康检查、日志 |
| 可观测性设计参考 | `excel_agent/docs/observability-plan.md` | 日志/健康检查方案（已落地） |
| 多渠道设计（备选） | `excel_agent/docs/multi-channel-design.md` | toC Web 前端接入方案 |
| whatsapp_simulator API | `whatsapp_simulator/README.md` | REST 接口、Webhook、白名单 |
| third_app API | `third_app/README.md` | 数据接口（端口以 `main.py` 的 8800 为准） |

---

## 总结

- **excel_agent** 是系统核心：模型推理、工具调用、会话持久化、消息处理均在此。
- **whatsapp_simulator** 是 WhatsApp 接入层：扫码登录、白名单、Webhook 桥接。
- **third_app** 是报表技能的模拟数据源，仅被沙箱内 skill 脚本 HTTP 调用。
- **content_agent** 是早期实验原型，**不在生产链路中**，可忽略。

主链路：**内部白名单用户 ↔ simulator ↔ excel_agent ↔（工具/sandbox）↔ third_app**。Postgres 与 Docker 沙箱为运行时基础设施。
