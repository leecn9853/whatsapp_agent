# Tools 瘦身 + 自建 Docker 沙箱

## 现状（2026-08-27，阶段二已完成）

`generate_cost_report_image`/`generate_alipay_matching_report` 两个重工具（原本 docstring 把
`SKILL.md` 已讲过的参数映射、报表结构又复述一遍，且每轮请求原样发送、不受
`SkillsMiddleware` 渐进式加载覆盖，是"工具太大"的主要来源）已迁移成
`skills/*/scripts/generate.py` CLI 脚本，靠 `deepagents` 在 backend 支持 command execution
时自动生成的 `execute` 工具跑（模型读 `SKILL.md` 知道该跑什么命令，不需要自建
dispatcher），产物通过 `save_file(source_path=...)` 登记发给用户。`main.py` 的工具列表从
8 个收窄到 6 个：`web_search`/`save_file`/`list_excel_files`/`inspect_excel`/
`aggregate_excel_sheet`/`create_chart_sheet`。

### 架构要点

- [DockerSandbox](../src/agent/backends/docker_sandbox.py) 是 `CompositeBackend` 的
  `default`；`/memories/` 单独路由到 `StoreBackend`。`execute` 的 `timeout` 参数用
  `timeout -k 5 {t}s sh -c ...` 包一层实现真正生效（原来签名带 `timeout` 但函数体不使用，
  框架检测到"支持"却静默丢弃）。
- `skills/`（只读）、`output/`、`snapshots/` 三个目录 bind mount 进容器。`main.py` 里
  `skills=["/workspace/skills/"]` **必须是绝对路径**——`SkillsMiddleware` 用 `ls()`
  （走 `execute()`，按 `WORKDIR` 解析相对路径没问题）找到技能目录后，会再调
  `download_files()`（底层 `get_archive()`）取 `SKILL.md` 内容，这个归档 API 不按 `WORKDIR`
  解析相对路径，传相对路径会 100% 报 `file_not_found`。
- 两个技能的 `scripts/` 目录各自完整独立复制一份需要的渲染/命名逻辑（不 import 原 `ctx`
  依赖的函数签名，改写成吃字符串参数），不共享库、不打进镜像，符合"每个技能自包含"的原则；
  镜像只装通用运行时（LibreOffice + CJK 字体 + `httpx`/`openpyxl`/`pymupdf`/`Pillow`）。
- CLI 脚本输出契约：成功时最后一行 stdout 打印 `RESULT_PATH:<容器内绝对路径>` + `exit 0`；
  已知失败（`httpx.HTTPError`/`ReportRenderError`）打印 `ERROR:<中文说明>` + `exit 1`；未知
  异常兜底成 `ERROR:脚本内部错误：<e>`，完整 traceback 打到 stderr 不污染 stdout 契约。
  `SKILL.md` 教模型只看 stdout 最后一行判断成功/失败。
- [save_file.py](../src/agent/tools/save_file.py) 加了 `source_path` 参数，优先于
  `filename`/`content`（传了 `source_path` 后两者会被忽略）。校验：`/workspace/output/`
  前缀 + `is_relative_to` 防路径穿越 + `.is_file()` 存在性，三者都满足才登记成功。
- LibreOffice 的 `profile_dir`/`outdir` 每次调用用全新 `tempfile.TemporaryDirectory()`（避免
  并发调用抢同一份用户配置锁）这一不变量在两份 CLI 脚本里逐字保留。
- 6 个死文件已删除：`cost_report_tools.py`/`alipay_report_tools.py`/`_cost_report_render.py`/
  `_alipay_report_render.py`/`_report_screenshot.py`/`_report_snapshot.py`；
  [files.py](../src/agent_server/shared/files.py) 简化为
  `FILE_OUTPUT_TOOL_NAMES = {"save_file", *OUTPUT_FILE_TOOL_NAMES}`。
- 切换到支持 `execute` 的 backend 后，`FilesystemMiddleware` 的 `permissions` 不能再挂全局
  `/** deny` 兜底规则（deepagents 强制要求：backend 支持 `SandboxBackendProtocol` 时，
  `_permissions` 里每条规则的 `paths` 必须全部落在 `CompositeBackend.routes` 前缀内，这里
  只有 `/memories/`）。现状是只保留 `/memories/** allow`；模型内置 `write_file`/`edit_file`
  因此可以自由写容器 `/workspace` 任意位置——镜像里除了三个 bind mount 目录外没有别的文件，
  判定为收窄暴露面而非回归。
- 验证方式（`cost-report`、`alipay-report` 均已跑通）：宿主机直跑 → 容器内 `docker compose
  exec sandbox` 直跑 → 真实对话触发 `execute`+`save_file(source_path=...)` 全链路。

## 遗留问题（未解决）

1. **容器启动和 agent 进程启动之间没有编排**——[docker_sandbox.py](../src/agent/backends/docker_sandbox.py)
   的 `DockerSandbox.__init__` 在构造时就要求容器已经在跑（`docker.from_env().containers.get(...)`），
   而 `main.py` 在模块顶层构造 `backend`。如果 `docker compose up -d sandbox` 没先执行，或容器
   被删/改名，agent 服务会在 import 阶段直接启动失败，没有重试、没有健康检查串联。需要补一个
   显式的启动依赖顺序（entrypoint wait-for-container，或把容器查找改成惰性+带重试）。

2. **LibreOffice 真实负载下的性能基准还没测**——"阶段二不做池化"的依据目前只建立在空跑
   `python3 --version`/容器冷启动的基准（~0.033s/~0.59s）上，跟真实 `soffice` 转换耗时完全
   不是一个量级。应该在真实负载下测一次单容器并发耗时，确认扛得住才能放心维持"不做池化"的
   结论。

3. **`read_file` 预览生成图片可能拖垮摘要中间件（2026-08-27 发现，未修复，用户已确认延后
   单独处理）**——模型在 `execute`+`save_file` 之后有时会自主调用内置 `read_file` 去"预览"
   刚生成的图片，deepagents 对图片文件按 mimetype 返回 image content block
   （[filesystem.py:1929](../.venv/lib/python3.14/site-packages/deepagents/middleware/filesystem.py#L1929)）。
   这条带图片的消息进入历史后，如果该轮累计 token 数刚好越过 `SUMMARY_TRIGGER_TOKENS`
   （4000）阈值，`ConversationSummaryAuditMiddleware` 会拿含图片的完整历史去调同一个不支持
   图片输入的 `deepseek-v4-flash` 做结构化摘要，直接 400 报错，整轮任务 `RunFailed`。

   **生产影响已确认**：[whatsapp_meta/processor.py:87-91](../src/agent_server/channels/whatsapp_meta/processor.py#L87-L91)
   遇到 `RunFailed` 时只回复一句通用失败话术，**不会**发送 `fail.files` 里已经落盘成功的
   文件——用户会收到"处理失败"、什么文件都收不到。debug 用的 toB SSE 接口
   （[tob/routes.py:48-51](../src/agent_server/channels/tob/routes.py#L48-L51)）行为不同，
   失败时仍会把 `fail.files` 塞进 `error` 事件，本地调试容易漏过这个生产环境差异。

   不是这次迁移引入的新问题——老的 `@tool` 版本同样会产出图片文件、模型同样有机会
   `read_file` 预览，这个暴露面在阶段二之前就存在，只是这次给 `alipay-report` 做真实对话
   验证时刚好撞上触发条件。**用户决定单独开一轮处理，不阻塞阶段二收尾**。候选方案（未定案，
   留给以后那一轮决定）：
   ① 两个技能的 `SKILL.md` 加明确指令，教模型 `execute` 产出的图片路径不需要、也不要用
   `read_file` 预览，直接把 `RESULT_PATH` 传给 `save_file`；
   ② `ConversationSummaryAuditMiddleware`/`SummarizationMiddleware` 传消息给
   `deepseek-v4-flash` 之前过滤掉图片 block，从框架层面兜底；
   ③ 两者都做。

## 关键文件

| 文件 | 状态 |
| --- | --- |
| `src/agent/tools/cost_report_tools.py` | 已删除——逻辑迁到 [skills/cost-report/scripts/generate.py](../src/agent/skills/cost-report/scripts/generate.py) |
| `src/agent/tools/alipay_report_tools.py` | 已删除——逻辑迁到 [skills/alipay-report/scripts/generate.py](../src/agent/skills/alipay-report/scripts/generate.py) |
| `src/agent/tools/_cost_report_render.py` / `_alipay_report_render.py` / `_report_screenshot.py` / `_report_snapshot.py` | 已删除——逻辑分别独立复制进两个技能各自的 `scripts/generate.py`，不再共享 |
| [src/agent/skills/*/SKILL.md](../src/agent/skills/) | 已补齐"怎么调用 execute"的具体命令模板（`cost-report`/`alipay-report` 均已完成） |
| [src/agent/skills/cost-report/scripts/](../src/agent/skills/cost-report/scripts/) / [alipay-report/scripts/](../src/agent/skills/alipay-report/scripts/) | 阶段二新增，两个技能各自自包含的 CLI 脚本 + 静态资源（模板/字体） |
| [sandbox/Dockerfile](../sandbox/Dockerfile) | 已加 LibreOffice + CJK 字体 + `httpx`/`openpyxl`/`pymupdf`/`Pillow`，只含通用运行时，不含项目业务代码 |
| [docker-compose.yml](../docker-compose.yml) | `sandbox` service 已挂 `skills:ro`/`output`/`snapshots` 三个 bind mount |
| [src/agent/backends/docker_sandbox.py](../src/agent/backends/docker_sandbox.py) | 已接入 `main.py` 作为默认 backend；`timeout` 参数已修复生效 |
| [sandbox/smoke_test.py](../sandbox/smoke_test.py) / [sandbox/skills_discovery_test.py](../sandbox/skills_discovery_test.py) | 阶段二收尾（删除死代码后）重跑均通过 |
| [src/agent/main.py](../src/agent/main.py) | `backend.default` 为 `DockerSandbox()`、`skills` 为绝对路径、`caller_prompt` 含 `run_id`/`user_id`；工具列表收窄到 6 个 |
| [src/agent/tools/save_file.py](../src/agent/tools/save_file.py) | 已加 `source_path` 参数（优先于 `filename`/`content`），含路径穿越/存在性校验 |
| [src/agent_server/shared/files.py](../src/agent_server/shared/files.py) | 已去掉两个报表常量的引用，`FILE_OUTPUT_TOOL_NAMES = {"save_file", *OUTPUT_FILE_TOOL_NAMES}` |
