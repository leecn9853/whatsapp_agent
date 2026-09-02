# WhatsApp Excel Agent 项目总览

本文档描述当前仓库内三个服务的职责、相互关系，以及建议的优化与补充项。适用于新成员 onboarding 或规划后续迭代时快速建立全局认知。

## 项目定位

本项目是一个 **面向内部人员的 Excel / 报表 AI 助手**。白名单内的同事通过 WhatsApp 向指定机器人账号发消息（文字或 Excel 文件），即可触发 Agent 分析数据、生成图表，或调用预置技能（成本报表、支付宝流水报表等）产出图片/文件并回传。

**使用范围**：内部工具，非对外商业产品。用户规模小、身份可控，通过 `MESSAGE_WHITELIST` 限制只有指定号码能触发 Agent。

核心 AI 能力集中在 `excel_agent`；`whatsapp_simulator` 承担 **WhatsApp 接入层**（扫码登录、收发消息、Webhook 推送）；`third_app` 为报表技能提供模拟数据源。

---

## WhatsApp 接入方案

**已确定：统一使用 `whatsapp_simulator`（whatsapp-web.js）作为 WhatsApp 接入方式。**

| 方案 | 状态 | 说明 |
|------|------|------|
| **whatsapp_simulator** | ✅ **采用** | 专用 WhatsApp 号扫码登录，白名单控制访问，支持 Excel 文件收发 |
| Meta Cloud API（`whatsapp_meta`） | 不采用 | 代码中保留但不在当前方案内使用；适合对外大规模商业场景，内部工具无需此复杂度 |
| 阿里云 Chat App 等 BSP | 不采用 | 同上，过度设计 |

**选型理由（内部场景）**：

- 无 Meta 企业认证、HTTPS 公网 webhook、专用 API 号码等申请成本
- 白名单机制已满足「仅内部人员可用」的安全需求
- 支持 Excel 文件上传（`whatsapp_meta` 渠道当前尚未实现）
- 用户规模小（数十人以内），非官方协议的风险可接受；建议使用**专用工作号**，勿绑个人主号

**运维注意**：simulator 依赖 WhatsApp Web 非官方协议，可能因 WhatsApp 更新导致掉线需重新扫码；会话持久化在 `.wwebjs_auth/`，需做好备份。

---

## 三个服务一览

| 服务 | 技术栈 | 默认端口 | 是否必需 | 一句话说明 |
|------|--------|----------|----------|------------|
| **excel_agent** | Python (Starlette + LangGraph + deepagents) | `8200` | **必需** | 核心 Agent 服务：接收消息、跑模型、调工具/技能、持久化会话 |
| **whatsapp_simulator** | Node.js (Express + whatsapp-web.js) | `3000` | **必需**（WhatsApp 渠道） | WhatsApp 网关：扫码登录、白名单过滤、Webhook 与 Agent 双向通信 |
| **third_app** | Python (FastAPI) | `8800` | **必需**（报表技能） | 模拟第三方业务数据 API，为成本报表/支付宝报表技能提供数据源 |

除上述三个服务外，`excel_agent` 还依赖 **Docker Compose 起的 Postgres**（会话/checkpoint 持久化）和 **Docker 沙箱容器**（LibreOffice 渲染报表截图），它们不算独立「业务服务」，但是运行时的硬依赖。

**无 WhatsApp 的调试路径**：使用 `excel_agent` 的 `tob` 渠道（`POST /v1/tob/threads/{id}/runs` SSE 接口），无需启动 `whatsapp_simulator`，适合纯后端调试。

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

## 各服务详细说明

### 1. excel_agent（核心 Agent 服务）

**目录**：`excel_agent/`

**职责**：

- **agent-server**（`src/agent_server`）：对外 HTTP 服务，聚合多渠道路由
- **agent 本体**（`src/agent`）：基于 deepagents + DeepSeek 模型，提供 Excel 读写、图表生成、联网搜索、文件保存等工具
- **Skills**（`src/agent/skills/`）：预置业务能力
  - `cost-report`：从 third_app 拉取成本数据，在 Docker 沙箱内用 LibreOffice 渲染报表截图
  - `alipay-report`：从 third_app 拉取支付宝匹配流水，生成报表图片
  - `excel-chart`：通用 Excel 图表生成说明
- **渠道适配**（`src/agent_server/channels/`）：
  - `whatsapp/`：**主渠道**，对接 `whatsapp_simulator` 的 Webhook + 出站 HTTP
  - `tob/`：内部调试 SSE API + admin 查看页（无需 WhatsApp 时可用）
  - `whatsapp_meta/`：Meta Cloud API 实现，**当前方案不使用**，代码保留供未来参考

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
- 通过 `WEBHOOK_URL` 将 `message` 等事件 POST 到 `excel_agent` 的 `/webhook`
- **`MESSAGE_WHITELIST`**：只有白名单内的号码发来的消息才会推送给 Agent，其余消息忽略

**与 excel_agent 的对接**：

| 方向 | 机制 |
|------|------|
| 入站（用户 → Agent） | simulator 收到白名单消息后 Webhook → `excel_agent POST /webhook` |
| 出站（Agent → 用户） | `excel_agent` 调 `WHATSAPP_SIMULATOR_URL/messages` 或 `/messages/media` |

**推荐配置**（`whatsapp_simulator/.env`）：

```bash
PORT=3000
WEBHOOK_URL=http://localhost:8200/webhook
WEBHOOK_EXCLUDE_EVENTS=qr
# 内部同事号码，逗号分隔（支持带/不带国家码、@c.us 后缀）
MESSAGE_WHITELIST=85212345678,8613800138000
```

**启动**：

```bash
cd whatsapp_simulator
npm install
cp .env.example .env
# 编辑 WEBHOOK_URL 和 MESSAGE_WHITELIST
npm start
# 首次启动扫码登录；也可 GET http://localhost:3000/qr 获取二维码
```

**运维提示**：

- 使用**专用工作号**，不要使用个人主号
- 定期备份 `.wwebjs_auth/` 目录，避免重装后需重新扫码
- 通过 `GET /status` 检查连接状态；掉线时执行 `POST /session/restart` 或重新扫码
- 群聊消息默认不触发 Agent（`excel_agent` 侧对 `@g.us` 直接忽略）

---

### 3. third_app（模拟第三方数据服务）

**目录**：`third_app/`

**职责**：

提供报表技能所需的**假数据 API**，模拟真实业务系统的 HTTP 接口：

| 接口 | 用途 | 说明 |
|------|------|------|
| `GET /api/electronics/orders` | 成本报表 — 供应商采购数据 | 一次性返回约 150 条 |
| `GET /api/food-agri/orders` | 成本报表 — 月度成本明细 | 分页，page_size 上限 99 |
| `GET /api/alipay/matching-records` | 支付宝报表 — 匹配流水 | 按日缓存，分页，page_size 上限 500 |

**与 excel_agent 的对接**：

- Agent 宿主机上的脚本通过 `THIRD_APP_BASE_URL`（默认 `http://127.0.0.1:8800`）访问
- Docker 沙箱容器内通过 `http://host.docker.internal:8800` 访问（在 `docker-compose.yml` 中硬编码）

**启动**：

```bash
cd third_app
uv run python main.py   # 监听 0.0.0.0:8800
```

> 不启动 third_app 不会导致 agent-server 启动失败，但触发 `cost-report` / `alipay-report` 技能时会报「拉取数据失败」。

---

## 典型消息处理链路

1. 白名单内的同事在 WhatsApp 向机器人号发送文字或 Excel 文件
2. `whatsapp_simulator` 校验白名单后，POST `{ event: "message", data: {...} }` 到 `excel_agent /webhook`
3. `channels/whatsapp/routes.py` 解析 payload：校验文件格式（仅 `.xlsx`/`.xls`）、落盘上传文件、创建 `run_id`
4. 后台任务 `processor.process_message` 调用 `shared/engine.run_agent_turn`
5. Agent 根据需要调用工具（读 Excel、聚合、画图）或 `execute` 跑 skill 脚本
6. 报表类 skill 在 Docker 沙箱内执行，HTTP 拉取 `third_app` 数据，LibreOffice 渲染后截图到 `output/`
7. `processor` 通过 `whatsapp/client.py` 调 simulator 的 `/messages` 或 `/messages/media` 把结果推回用户

---

## 服务间依赖与启动顺序

建议按以下顺序启动：

```
1. third_app          → 报表技能的数据源（可晚于 agent-server 启动，但跑报表前必须就绪）
2. docker compose     → postgres + sandbox（agent-server 启动时会等待 sandbox 最多 30s）
3. excel_agent        → make dev (:8200)
4. whatsapp_simulator → 配置 WEBHOOK_URL + MESSAGE_WHITELIST 后启动，扫码登录
```

**验证各服务是否正常**：

```bash
# third_app
curl http://127.0.0.1:8800/docs

# excel_agent（无需 WhatsApp）
curl -N -X POST http://127.0.0.1:8200/v1/tob/threads/smoke-test/runs \
  -H "Content-Type: application/json" \
  -d '{"message": "你好"}'

# whatsapp_simulator
curl http://127.0.0.1:3000/status
```

---

## 端口与环境变量速查

| 组件 | 端口 | 关键配置 |
|------|------|----------|
| excel_agent (agent-server) | `8200` | `DATABASE_URL`, `DEEPSEEK_API_KEY`, `TAVILY_API_KEY`, `WHATSAPP_SIMULATOR_URL` |
| whatsapp_simulator | `3000` | `WEBHOOK_URL`, `MESSAGE_WHITELIST` |
| third_app | `8800` | 无（开发 mock，无鉴权） |
| Postgres | `5432` | `POSTGRES_PASSWORD` |
| Docker sandbox | — | 容器名 `excel_agent-sandbox-1`，无对外端口 |

**两处 `.env` 需对齐**：

| whatsapp_simulator | excel_agent |
|--------------------|-------------|
| `WEBHOOK_URL=http://localhost:8200/webhook` | — |
| — | `WHATSAPP_SIMULATOR_URL=http://localhost:3000` |

---

## 优化与补充建议

以下按优先级归类，供后续迭代参考。

### 高优先级（影响可运维性与开发体验）

| 项 | 现状 | 建议 |
|----|------|------|
| **统一日志与健康检查** | `excel_agent` 内大量 `logger.info` 因未配置 `basicConfig` 在生产不输出；无 `/health` 端点 | 按 `excel_agent/docs/observability-plan.md` 落地：统一 logging、run_id/thread_id 上下文注入、`GET /health`、最近失败 run 查询接口 |
| **simulator 可用性监控** | 掉线/扫码失效无主动告警，用户发消息无响应才发现 | 定时探测 `GET /status`；状态非 `READY` 时通知运维；文档化重新扫码流程 |
| **根目录编排脚本** | 三个服务 + Docker 需分别启动，无 monorepo 级一键启动 | 增加根目录 `Makefile` 或 `scripts/dev.sh`：并行拉起 third_app、docker compose、agent-server、simulator，并打印各服务健康检查结果 |
| **文档与代码不一致** | `excel_agent/README.md` 仍将 third_app/simulator 描述为「仓库外兄弟项目」；`third_app/README.md` 写的是 `8000` 端口，实际为 `8800` | 更新各 README，统一端口与接入方案说明 |
| **根目录 README** | 仓库根目录无 README | 增加简短 `README.md`，链到本文档及各子项目 README |

### 中优先级（功能完善）

| 项 | 现状 | 建议 |
|----|------|------|
| **环境变量集中管理** | `whatsapp_simulator` 与 `excel_agent` 两处 `.env` 需手动对齐 | 根目录 `.env.example` 或 dev 脚本中集中声明 `WEBHOOK_URL` / `WHATSAPP_SIMULATOR_URL` |
| **白名单管理** | 改白名单需改 `.env` 并重启 simulator | 短期可接受；人数增多时可考虑配置文件热加载或简单管理页 |
| **third_app 启动校验** | agent-server 启动不检查 third_app 可达性，报表失败才暴露 | 可在 `/health` 中增加对 `THIRD_APP_BASE_URL` 的可选探测 |
| **集成测试** | 有 sandbox smoke_test、pytest 性能测试，但缺少跨服务 E2E | 增加可选 E2E：tob 渠道触发 cost-report + 校验 output 产物 |

### 低优先级 / 长期

| 项 | 现状 | 建议 |
|----|------|------|
| **Web 内部入口** | 仅 WhatsApp + tob 调试接口 | 若团队更习惯网页传 Excel，可做简易内部 Web 页（复用 tob SSE 或落地 `channels/toc/`） |
| **third_app 鉴权** | 开发 mock 无认证 | 若对接真实第三方，在 skill 脚本侧增加 API Key，并在 `.env` 统一管理 |
| **统一 docker-compose** | 仅 postgres + sandbox 在 compose 中 | 可选增加 `docker-compose.dev.yml` 将 third_app 容器化 |
| **CI/CD** | 未见仓库级 GitHub Actions | 增加：lint、pytest、`smoke_test.py`、可选 PR 门禁 |

### 已知设计约束（非缺陷，但需知晓）

- **仅白名单用户可触发**：非白名单号码发消息会被 simulator 静默忽略
- **群聊默认不回复**：`whatsapp/routes.py` 对 `@g.us` 结尾的 JID 直接 ack，不触发 Agent
- **文件类型限制**：WhatsApp 渠道仅接受 `.xlsx`/`.xls`，其它格式会短路回复提示
- **单文件大小限制**：simulator 默认 `MAX_MEDIA_SIZE_MB=20`，超过则返回 `mediaError: too_large`
- **sandbox 启动超时**：agent-server 等待 sandbox 容器就绪默认 30 秒，超时则进程退出
- **报表技能与通用 Excel 工具分离**：cost/alipay 报表走固定 skill 脚本 + third_app，不走 `inspect_excel` 探索流程
- **非官方 WhatsApp 协议**：simulator 基于 whatsapp-web.js，存在因 WhatsApp 更新或风控导致掉线/封号的可能；内部小范围使用可接受，不建议对外大规模商用

---

## 相关文档索引

| 文档 | 路径 | 内容 |
|------|------|------|
| excel_agent 启动与排障 | `excel_agent/README.md` | 环境变量、启动步骤、常见问题 |
| 可观测性方案（待落地） | `excel_agent/docs/observability-plan.md` | 日志、健康检查、run 查询 |
| 多渠道设计（toC 待落地） | `excel_agent/docs/multi-channel-design.md` | toC Web 前端接入方案（备选，非当前主路径） |
| whatsapp_simulator API | `whatsapp_simulator/README.md` | REST 接口、Webhook 事件、白名单配置 |
| third_app API | `third_app/README.md` | 数据接口说明（注意端口以 `main.py` 的 8800 为准） |

---

## 总结

- **excel_agent** 是系统的核心：模型推理、工具调用、会话持久化、消息处理均在此。
- **whatsapp_simulator** 是 WhatsApp 接入层：专用号扫码登录、白名单过滤、Webhook 双向桥接内部用户与 Agent。
- **third_app** 是报表类技能的模拟数据源，仅被 Docker 沙箱内的 skill 脚本 HTTP 调用。

三者关系：**内部白名单用户 ↔ simulator ↔ agent-server ↔（工具/sandbox）↔ third_app**。Postgres 与 Docker 沙箱为 agent 的运行时基础设施。当前最值得优先补齐的是 **可观测性（日志/健康检查）**、**simulator 可用性监控** 以及 **根目录开发编排**。
