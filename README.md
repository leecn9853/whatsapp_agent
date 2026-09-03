# WhatsApp Excel Agent

面向内部人员的 Excel / 报表 AI 助手。白名单用户通过 WhatsApp 发消息或 Excel 文件，由 Agent 分析数据、生成图表或产出报表。

> **当前阶段：Demo / 内网验证**  
> 侧重跑通主链路，鉴权、生产加固等待需求确定后再做。详见 [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md#当前阶段demo)。

## 仓库结构

| 目录 | 说明 |
|------|------|
| [`excel_agent/`](excel_agent/) | 核心 Agent 服务（:8200） |
| [`whatsapp_simulator/`](whatsapp_simulator/) | WhatsApp 接入网关（:3000） |
| [`third_app/`](third_app/) | 报表技能 mock 数据源（:8800） |
| [`content_agent/`](content_agent/) | 实验原型，**不参与主链路** |

完整架构见 [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md)。

## 环境要求

| 工具 | 用途 |
|------|------|
| [uv](https://docs.astral.sh/uv/) | `excel_agent`、`third_app`（含语音转写用的 ffmpeg 二进制） |
| **Docker Desktop** | Postgres + LibreOffice 沙箱 |
| Node.js | `whatsapp_simulator` |

### Docker Desktop

1. 安装 [Docker Desktop for Mac (Apple Silicon)](https://docs.docker.com/desktop/setup/install/mac-install/)
2. 打开 **应用程序 → Docker**，等菜单栏鲸鱼图标显示 **Docker Desktop is running**
3. 若终端找不到 `docker` 命令，在 Docker Desktop → **Settings → General** 勾选 CLI 工具，或执行：
   ```bash
   echo 'export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"' >> ~/.zshrc
   source ~/.zshrc
   ```

## 首次初始化（一次性）

```bash
# Python 依赖
cd excel_agent && uv sync && cd ..
cd third_app && uv sync && cd ..

# Node 依赖 + Puppeteer 浏览器（没装 Google Chrome 时必做）
cd whatsapp_simulator && npm install && npm run install-browser && cd ..

# 环境变量（各服务独立 .env，不要用仓库根目录的 .env）
cp excel_agent/.env.example excel_agent/.env
cp whatsapp_simulator/.env.example whatsapp_simulator/.env
# 编辑 excel_agent/.env：填 DEEPSEEK_API_KEY、TAVILY_API_KEY、DATABASE_URL 等
# 编辑 whatsapp_simulator/.env：WEBHOOK_URL=http://localhost:8200/webhook
```

## 日常启动（推荐）

```bash
# 1. 打开 Docker Desktop，等就绪

# 2. 仓库根目录一键启动（含 postgres/sandbox + 三个业务服务）
cd ~/Desktop/work_blue/whatsapp_agent   # 换成你的路径
make dev

# 3. 首次或会话失效时：浏览器打开 http://localhost:3000/login 扫码
#    （也可打开 /qr 看 PNG；已登录时可在该页退出换号）

# 4. 检查
make health
```

`make dev` 会并行拉起所有服务，**终端需保持打开**；`Ctrl+C` 会停止业务服务（Docker 容器继续跑）。

### 分步启动（日志更清晰）

```bash
make infra        # Postgres + sandbox（也可省略，make dev 已包含）
make third-app    # 终端 1
make agent        # 终端 2
make simulator    # 终端 3
```

### 不走 WhatsApp 时

只需 Docker + `make agent`，用 tob 接口测试：

```bash
curl -N -X POST http://127.0.0.1:8200/v1/tob/threads/smoke-test/runs \
  -H "Content-Type: application/json" -d '{"message": "你好"}'
```

## 配置说明

各服务**独立 `.env`**，不要合并成一份：

| 配置文件 | 必填项 |
|----------|--------|
| `excel_agent/.env` | `DEEPSEEK_API_KEY`、`TAVILY_API_KEY`、`DATABASE_URL`、`WHATSAPP_SIMULATOR_URL` |
| `whatsapp_simulator/.env` | `WEBHOOK_URL`；`MESSAGE_WHITELIST` demo 可留空 |

**对接参数**（单机本地都用 localhost）：

| whatsapp_simulator | excel_agent |
|--------------------|-------------|
| `WEBHOOK_URL=http://localhost:8200/webhook` | `WHATSAPP_SIMULATOR_URL=http://localhost:3000` |

`POSTGRES_PASSWORD` 与 `DATABASE_URL` 里的密码须一致；由 `excel_agent/docker-compose.yml` 读取。

## 常见问题

| 现象 | 处理 |
|------|------|
| `role "excel_agent" does not exist` | 本机 Homebrew Postgres 占了 5432：`brew services stop postgresql@18`（及 @16），再 `make infra` |
| `Could not find Chrome` | `cd whatsapp_simulator && npm run install-browser` |
| `docker: command not found` | 打开 Docker Desktop，或配置 PATH（见上文） |
| simulator 一直重试 initialize | 先 `npm run install-browser`；仍失败则看 [whatsapp_simulator/README.md](whatsapp_simulator/README.md) 运维章节 |
| 报表报「拉取数据失败」 | 确认 `third_app` 在跑：`curl http://127.0.0.1:8800/docs` |
| 发回复 `503` / `client not ready` | WhatsApp 未登录或掉线：打开 http://localhost:3000/login 扫码；`curl http://localhost:3000/status` 应为 `READY` |
| `/login` 仍是 `not found` / 改了代码不生效 | 旧 Node 占着 3000：`kill -9 $(lsof -tiTCP:3000 -sTCP:LISTEN)` 后重新 `make dev` |
| 语音报错找不到 `ffmpeg` | 在 `excel_agent` 执行 `uv sync`（依赖含 `imageio-ffmpeg`）后重启 agent |

## 文档索引

| 文档 | 内容 |
|------|------|
| [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) | 架构、deepagents 技术栈、消息链路、Demo 约束 |
| [excel_agent/README.md](excel_agent/README.md) | Agent 环境变量、健康检查、排障 |
| [whatsapp_simulator/README.md](whatsapp_simulator/README.md) | API、扫码、掉线排查 |
| [third_app/README.md](third_app/README.md) | Mock 数据接口 |
