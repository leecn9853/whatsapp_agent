# excel-agent

通过 WhatsApp 对话处理 Excel 表格、生成报表的 agent。基于 [deepagents](https://github.com/langchain-ai/deepagents)
构建，用自建的 Docker 沙箱跑 LibreOffice 报表渲染等重型任务，模型只负责读技能说明、拼命令、
把产物登记发给用户。

> **完整启动流程**见仓库根目录 [README.md](../README.md)（`make dev`、Docker Desktop、首次初始化）。  
> 本文档侧重 **excel_agent 子项目** 的环境变量与排障。

## 架构总览

- **agent-server**（`src/agent_server`，Starlette）：对外的 HTTP 服务，接收 WhatsApp
  webhook/调试请求，调用 agent 并把产出文件回传给用户。
- **Postgres**：agent 的会话状态（checkpointer/store）持久化存储，`docker-compose.yml` 里的
  `postgres` service。
- **Docker 沙箱**（`docker-compose.yml` 里的 `sandbox` service）：装了 LibreOffice 的长驻
  容器，`cost-report`/`alipay-report` 两个技能的 CLI 脚本在这里跑，产物通过 bind mount 出的
  `output/`/`snapshots/` 目录回到宿主机。
- **third_app**（同仓库 `third_app/`）：模拟第三方数据服务，提供成本报表/支付宝流水等接口，是
  两个报表技能的数据来源，必须单独启动。
- **WhatsApp 接入**：通过同仓库 `whatsapp_simulator`（`whatsapp` 渠道）接入
  WhatsApp，或者用不需要任何 WhatsApp 配置的 `tob` 调试接口直接测 agent。

更细的设计背景见仓库根目录 [PROJECT_OVERVIEW.md](../PROJECT_OVERVIEW.md)。

## 外部依赖

启动前需要准备好：

- **运行环境**：[uv](https://docs.astral.sh/uv/)（管理 Python 版本和依赖，Python 版本见
  `.python-version`，不需要单独装）、**Docker Desktop**（提供 Docker Engine，跑
  `postgres`/`sandbox` 两个容器）。
- **仓库内的兄弟服务**（均在 monorepo 根目录下）：
  - `third_app`——**必需**（跑报表时），报表功能的数据来源。
  - `whatsapp_simulator`——可选，只有要在本地走 `whatsapp` 渠道调试时才需要。
- **外部账号/密钥**（填进 `.env`）：
  - DeepSeek API Key、Tavily API Key——**必需**，agent 主模型和 `web_search` 工具要用。

## 环境变量

在 **`excel_agent/.env`** 配置（从 `.env.example` 复制，不要用仓库根目录的 `.env`）：

- **模型/工具 Key（必填）**：`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`TAVILY_API_KEY`。
- **third_app（必填）**：`THIRD_APP_BASE_URL`，本地直跑脚本/CLI 时用宿主机视角
  `http://127.0.0.1:8800`；沙箱容器里跑的脚本走的是 `docker-compose.yml` 里硬编码的
  `http://host.docker.internal:8800`，不受这个变量影响。
- **Postgres（必填，本地开发用 docker-compose 起的库）**：`POSTGRES_PASSWORD`、
  `DATABASE_URL`（两者要对得上，`DATABASE_URL` 里的密码就是 `POSTGRES_PASSWORD`）。
- **WhatsApp（按需）**：本地调试用 `whatsapp_simulator` 时填 `WHATSAPP_SIMULATOR_URL`。

## 启动步骤

**推荐**：在仓库根目录 `make dev` 或 `make agent`（会先由根目录脚本 / 你手动 `make infra` 拉起 Docker）。

仅启动本子项目时：

1. `uv sync`
2. `cp .env.example .env` 并填写
3. `docker compose up -d postgres sandbox`（或在根目录 `make infra`）
4. `make dev` → 监听 `0.0.0.0:8200`

`third_app`、`whatsapp_simulator` 需在其它终端单独启动，见根目录 README。

## 验证是否跑起来了

- LibreOffice 装好了：
  ```bash
  docker compose exec sandbox soffice --version
  ```
- 沙箱本身没问题（skills 挂载、`execute` 超时等）：
  ```bash
  PYTHONPATH=. uv run python sandbox/smoke_test.py
  PYTHONPATH=. uv run python sandbox/skills_discovery_test.py
  ```
- `third_app` 在跑：
  ```bash
  curl http://127.0.0.1:8800/docs
  ```
- agent 能正常应答——不需要配置任何 WhatsApp 渠道，用调试用的 `tob` SSE 接口发一句话最快：
  ```bash
  curl -N -X POST http://127.0.0.1:8200/v1/tob/threads/smoke-test/runs \
    -H "Content-Type: application/json" \
    -d '{"message": "你好"}'
  ```
- 健康检查与依赖状态：
  ```bash
  curl http://127.0.0.1:8200/health
  ```
- 日志：由 `LOG_LEVEL` 环境变量控制（默认 `INFO`），输出到 stdout。每条日志自动带上
  `run_id`/`thread_id`，可用 `grep run_id=xxx` 串联一次消息处理的完整链路。
- 最近失败 run 概览（仅本机可访问）：
  ```bash
  curl 'http://127.0.0.1:8200/v1/tob/admin/runs/recent?minutes=60'
  ```

也可在仓库根目录执行 `make health` 一次性检查所有服务。

## 常见问题

- **`role "excel_agent" does not exist`**：本机 Homebrew Postgres 占用了 5432，连错库了。执行
  `brew services stop postgresql@18`（如有 @16 也停），确认 `docker ps` 里有
  `excel_agent-postgres-1`，再重启 agent。
- **agent-server 启动时报 `DockerSandbox`/容器相关错误**：等了 30 秒 sandbox 容器还没进入
  `running` 状态——容器没起来，或者被删过/改过名字。回到"启动步骤"第 3 步重新
  `docker compose up -d postgres sandbox`，确认容器在跑（`docker compose ps`）之后重启
  agent-server。
- **生成报表时报"拉取数据失败"**：`third_app` 没启动，或者没跑在 8800 端口。回到"启动步骤"
  第 4 步检查。
