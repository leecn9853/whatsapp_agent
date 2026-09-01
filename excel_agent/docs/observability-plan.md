# 生产可观测性方案：日志 + 健康检查 + 运维查询

这是一份**待落地的方案**，先记录下来，还没有开始实现。目标是给 `excel_agent` 上生产前补上
运维/排障需要的最小可用能力：出问题后能第一时间查清楚是什么、发生在哪个用户/哪次 run 上，
不追求主动告警或接入外部监控系统。

## 背景：现状有哪些坑

- 代码里到处是 `logger.info/debug/warning`（`shared/engine.py`、`channels/whatsapp*/processor.py`、
  `channels/whatsapp_meta/routes.py`、`agent/backends/docker_sandbox.py`），但**从未调用过
  `logging.basicConfig`/`dictConfig`**。root logger 默认级别是 `WARNING`，所以现在
  `.info()`/`.debug()` 在生产环境里**根本不会被打印**；能打印出来的 warning/error 也只是
  Python 的兜底 handler 输出到 stderr，没有时间戳、没有任何结构化字段，出问题基本没法查。
- 已经有一张 Postgres `runs` 表（`shared/runs_store.py`）记录每次 agent 执行的
  status/attempt/error，是现成的排障资产，但只能按单个 `thread_id` 查
  （`tob/admin.py` 的 `get_runs`），没有"最近有哪些失败"的全局视角。
- 没有 `/health` 之类的健康检查接口，运维没法用一条 curl 确认服务/依赖是否正常。
- `DockerSandbox.execute()`（`agent/backends/docker_sandbox.py`）跑的每条命令完全没有日志，
  sandbox 出问题只能手动 `docker exec` 进容器排查。
- 一次消息处理链路（webhook 收到 → agent 重试 → 工具调用 → 回复用户/失败）分散在
  `engine.py`/`processor.py`/`docker_sandbox.py` 里，日志之间没有统一的关联字段
  （`run_id`/`thread_id`），排查某个用户的问题时串不起整条链路。

## 约束（已跟产品/运维方向确认，方案据此定范围）

- agent-server 生产先按**单机进程 / systemd** 跑，日志走 stdout（systemd 收进 journal），
  不引入外部日志系统。
- **不接**任何现成的日志/监控基础设施（无 ELK/Loki/Prometheus/Datadog），从零搭最简单可用的。
- **不需要主动告警**（Slack/企业微信/Sentry 都不需要），目标是"出问题后能查清楚"，不是
  "出问题主动通知"。

因此整套方案**不引入任何新依赖**，全部用标准库 `logging`/`contextvars` + 已有的
`docker`/`psycopg`。

## 改动内容

### 1. 统一日志配置（新文件 `src/agent_server/shared/logging_config.py`）

- `configure_logging()`：用 `logging.config.dictConfig` 一次性配置 root logger。
- 输出到 stdout（`StreamHandler`），不写文件——交给 systemd/journald 收集、轮转。
- 级别由环境变量 `LOG_LEVEL` 控制（默认 `INFO`），方便临时调成 `DEBUG` 排查。
- formatter 包含时间戳、级别、logger 名、以及第 2 步注入的 `run_id`/`thread_id`：
  `%(asctime)s %(levelname)-8s %(name)s [run_id=%(run_id)s thread_id=%(thread_id)s] %(message)s`。
- 把 `httpx`、`docker`、`asyncio`、`watchfiles`（uvicorn `--reload` 用）这几个第三方 logger
  显式设成 `WARNING`，避免 `LOG_LEVEL=DEBUG` 时被噪音淹没。
- 在 `src/agent_server/__init__.py` 最顶部调用 `configure_logging()`（在其余 import 之前）。

### 2. run_id/thread_id 自动注入日志（新文件 `src/agent_server/shared/log_context.py`）

- 用 `contextvars.ContextVar` 存当前 `run_id`/`thread_id`（默认值 `"-"`）。
- 提供 `bind_run_context(run_id, thread_id)`（同步 contextmanager，进入时 set，退出时 reset）。
- 提供一个 `logging.Filter` 子类，在 `emit` 前把这两个 contextvar 的值写进 `LogRecord`，配到
  `logging_config.py` 的 handler 上。
- asyncio 的 `create_task`/`to_thread` 默认会拷贝当前 context，所以只要在
  `run_agent_turn`（`shared/engine.py`）入口 `with bind_run_context(run_id, thread_id):`
  包一层，后续这次 run 触发的所有日志（包括同步跑在线程池里的 `DockerSandbox.execute`）都会
  自动带上这两个字段，不用在每个日志调用点手动传参数。
- 效果：运维可以直接 `journalctl -u excel-agent | grep 'run_id=<xxx>'` 把一次消息处理的完整
  链路（收到 webhook → 第几次 attempt → 调了哪些工具 → sandbox 跑了什么命令 → 成功/失败原因）
  串出来。

### 3. 补关键日志点

- `channels/whatsapp_meta/routes.py` `receive_webhook` 和 `channels/whatsapp/routes.py`
  `webhook`：在创建 run、`asyncio.create_task(process_message(...))` 前加一条 INFO
  日志（phone/wamid 或 phone、thread_id）——现在"收到消息并开始处理"完全没有留痕，只有失败
  路径才有日志。
- `shared/engine.py` `run_agent_turn`：
  - 用 `bind_run_context` 包住整个函数体。
  - attempt 1 开始时 INFO 一条"开始处理"。
  - 成功 `yield RunResult` 前，用 `time.monotonic()` 算总耗时，INFO 一条"处理完成，耗时
    Xms，尝试 N 次"。
  - 已有的重试 warning / 最终失败（在 `RunFailed` 抛出前，调用方 `processor.py` 里已有
    `logger.exception`）保持不变，天然会带上 run_id/thread_id。
- `agent/backends/docker_sandbox.py` `execute()`：记录命令（截断到合理长度，避免整段 base64
  之类的超长内容刷屏）、耗时、`exit_code`；`exit_code == 0` 记 DEBUG，非 0 记 WARNING。这是目前
  唯一完全没有日志覆盖、只能靠手动进容器排查的执行路径。

### 4. `/health` 健康检查接口（新文件 `src/agent_server/health.py`）

- `GET /health`：检查
  - Postgres：用 `_runtime.pool` 拿一个连接执行 `SELECT 1`（带短超时）。
  - Docker sandbox：复用 `docker_sandbox.py` 里的 `DEFAULT_CONTAINER_NAME`，检查容器
    `status == "running"`。
- 全部通过返回 `200 {"status": "ok", "checks": {"postgres": true, "sandbox": true}}`；
  任一失败返回 `503`，`checks` 里标出具体哪项失败。
- 不加 `local_only`（这个接口不暴露任何敏感信息，运维/未来的存活检测都可能要直接 curl）。
- 路由加进 `src/agent_server/channels/__init__.py` 聚合的 `routes` 列表（跟现有
  `_whatsapp_routes`/`_tob_routes` 同级），不需要改 `agent_server/__init__.py` 里
  `Starlette(routes=routes, ...)` 的构造方式。

### 5. 全局"最近 run 概览"接口（复用现有 `runs` 表 + `tob/admin.py` 的鉴权模式）

- `shared/runs_store.py` 新增 `RunsStore.alist_recent_runs(minutes: int, limit: int = 200)`：
  按 `created_at` 倒序查最近 N 分钟内**所有** thread 的 run（不像现有 `alist_runs_for_thread`
  按单个 thread 过滤），用于快速看"最近有没有大面积失败"。
- `channels/tob/admin.py` 新增 `get_recent_runs` handler（复用现成的 `@local_only` 装饰器），
  路由 `GET /v1/tob/admin/runs/recent?minutes=60`，返回 JSON 列表（跟 `get_runs` 的返回结构一致，
  多一个 `thread_id` 字段方便定位是哪个用户）。只加 JSON 接口，不改现有 admin 页面的 HTML/前端
  （避免超出这次范围）。

### 6. README 补一小节"日志与健康检查"

- 在现有"验证是否跑起来了"章节后加几行：`LOG_LEVEL` 环境变量说明、`curl /health` 示例、
  systemd 下怎么看日志（`journalctl`）、以及 `run_id`/`thread_id` 怎么用来串联一次请求的全部
  日志、`/v1/tob/admin/runs/recent` 的用途。

## 不做的事（明确排除，避免范围蔓延）

- 不引入 Prometheus/Grafana/Sentry 等外部系统，不加 `/metrics` 端点。
- 不做主动告警（邮件/Slack/企业微信）。
- 不改造成 JSON 结构化日志格式（journald 场景下人类可读文本 + 关键字段 grep 已经够用，真要接
  日志系统时再切 JSON formatter，改动只在 `logging_config.py` 一个文件）。
- 不把 agent-server 容器化进 `docker-compose.yml`（维持现有单机进程/systemd 部署方式）。
- 不改 `tob/admin.py` 现有 HTML 前端（只加 JSON 接口）。

## 涉及文件

新增：
- `src/agent_server/shared/logging_config.py`
- `src/agent_server/shared/log_context.py`
- `src/agent_server/health.py`

修改：
- `src/agent_server/__init__.py`（顶部调用 `configure_logging()`）
- `src/agent_server/channels/__init__.py`（挂 `/health` 路由）
- `src/agent_server/shared/engine.py`（`bind_run_context` + 开始/完成耗时日志）
- `src/agent_server/channels/whatsapp_meta/routes.py`、`channels/whatsapp/routes.py`
  （收到消息时加 INFO 日志）
- `src/agent/backends/docker_sandbox.py`（`execute()` 加日志）
- `src/agent_server/shared/runs_store.py`（新增 `alist_recent_runs`）
- `src/agent_server/channels/tob/admin.py`（新增 `get_recent_runs` 路由）
- `README.md`（补文档）

## 验证方式（落地时按这个顺序过一遍）

1. `make dev` 起服务，观察终端输出：应该能看到带时间戳/级别/`run_id=-`/`thread_id=-` 的启动
   日志（此时还没有 run，字段是默认值 `-`）。
2. 用 `tob` 调试接口（不需要 WhatsApp 配置）跑一次对话，观察：
   - webhook 收到消息的 INFO 日志。
   - 同一个 `run_id` 贯穿"开始处理"→ 工具调用触发的 sandbox 命令日志 →"处理完成，耗时…"。
   - `docker compose stop sandbox` 后再跑一次触发报表技能，确认 `DockerSandbox.execute` 或
     启动等待逻辑的日志能定位到"sandbox 没起来"。
3. `curl localhost:8200/health`：
   - 正常情况下返回 `200 {"status":"ok",...}`。
   - `docker compose stop sandbox` 后重新请求，确认返回 `503` 且 `checks.sandbox=false`。
   - `docker compose stop postgres` 后重新请求（预期 Postgres 检查超时/失败，返回 503）。
4. `curl localhost:8200/v1/tob/admin/runs/recent?minutes=60`（本机执行，`local_only` 限制）：
   确认能看到刚跑的几次 run，字段包含 `thread_id`。
5. `LOG_LEVEL=DEBUG uv run uvicorn src.agent_server:app --port 8200` 跑一次，确认 sandbox
   `execute()` 的 DEBUG 日志（含命令内容）能看到，且 `httpx`/`docker` 库自身的 DEBUG 噪音没有
   被打出来。
6. `uv run pytest` 跑一遍现有测试，确认新增的日志配置/健康检查代码没有破坏现有测试（尤其是
   `runs_store`/`engine` 相关测试，如果存在）。
