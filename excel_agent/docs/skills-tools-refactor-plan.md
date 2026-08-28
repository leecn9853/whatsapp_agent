# Tools 瘦身 + 自建 Docker 沙箱

## 现状（2026-08-28，阶段二已完成，遗留问题已全部解决）

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
- [DockerSandbox.__init__](../src/agent/backends/docker_sandbox.py) 不再一次性查找容器失败即
  崩溃：新增 `_wait_for_container` 轮询容器是否进入 `running` 状态，默认 `ready_timeout=30s`/
  `poll_interval=1s`，超时才抛出带 `docker compose up -d sandbox` 提示的 `RuntimeError`。
  agent 进程和 sandbox 容器谁先启动不再有硬性顺序要求（原遗留问题 #1，已解决）。
- **LibreOffice 真实负载基准已测**（[sandbox/perf_benchmark.py](../sandbox/perf_benchmark.py)，
  跑 `cost-report` chart 全链路：拉数据 → 填模板 → soffice 重算 → soffice 转 pdf → pymupdf
  渲染，每次用不同 `report_id` 避免吃缓存）：并发 1/2/4/8 下单容器耗时分别是
  1.60s/1.55s/1.70s/2.16s（avg），成功率全部 100%，没有出现随并发数指数级恶化。测试机器
  是 Docker Desktop 默认 VM 配置（12 核/7.75GiB，未对 sandbox 设 CPU/内存限制），当前模板
  数据量下"阶段二不做 LibreOffice 进程池"的结论成立（原遗留问题 #2，已解决）。生产环境的
  资源配额如果明显更紧张，或者数据量/并发量级发生数量级变化，应该重跑这个脚本复核。
- **`read_file` 预览生成图片导致 400 已解决（原遗留问题 #3）**——`deepseek-v4-flash`
  不支持图片输入，而模型在 `execute`+`save_file` 之后有时会自主调用内置 `read_file`
  去"预览"刚生成的图片，deepagents 按 mimetype 返回 image content block
  （[filesystem.py:1929](../.venv/lib/python3.14/site-packages/deepagents/middleware/filesystem.py#L1929)）。
  这条带图片的消息进入 `state["messages"]` 后，任何绕开主模型节点、自己直接拿
  `state["messages"]` 发起 `.ainvoke()` 的代码都会把原始图片 block 带给不支持图片的模型，
  直接 400、整轮 `RunFailed`。排查过程中发现这个模式实际命中三处，不是最初以为的一处：
  1. **主模型节点**：deepagents 的 `FilesystemMiddleware.awrap_model_call` 本来就有
     `_scrub_unsupported_multimodal_content`，按 `model.profile` 把不支持的 block
     换成文字占位符——但只在 `model.profile` 非空时才生效。`deepseek-v4-flash` 走自定义
     `base_url` 接入，不在 langchain 内置 model profile 库里，`profile` 一直是 `None`，
     这层清洗形同虚设。修复：给 [main.py](../src/agent/main.py) 里唯一的 `llm` 实例显式
     声明 `profile={"image_inputs": False, "audio_inputs": False, "video_inputs": False}`，
     让已有的清洗逻辑真正生效。
  2. **`topic_gate`**（[main.py](../src/agent/main.py) 的 `before_model` 钩子）：这是个
     独立、不绑工具的 yes/no 判断，直接把 `state["messages"]` 原样拼进自己的
     `llm.ainvoke()` 调用——这个调用在 `FilesystemMiddleware` 的包裹范围之外，`profile`
     清洗完全不覆盖它。一旦历史里出现过 `read_file` 预览图片留下的 `ToolMessage`，
     **同一 thread 后续每一轮**都会在 `topic_gate` 这一步直接 400，且这一步排在中间件
     链最前面，连主模型和摘要中间件都进不去——实测中这是影响面最大的一个点（真实
     `tob` 调试接口连续验证时发现）。
  3. **`ConversationSummaryAuditMiddleware`**（[conversation_summary.py](../src/agent/middleware/conversation_summary.py)，
     本仓库自建）：达到 `SUMMARY_TRIGGER_TOKENS` 阈值时，同样把原始 `state["messages"]`
     整个拼进给 `deepseek-v4-flash` 的结构化摘要 `.ainvoke()` 调用。
  2、3 两处都不是主模型节点，`profile` 清洗天然覆盖不到，统一提取了一个共享 helper
  [strip_multimodal_content](../src/agent/middleware/_multimodal.py)：只处理
  `ToolMessage`/`HumanMessage`，把 `{"image","audio","video","file"}` 类型的 content
  block 换成文字占位符，其余原样保留；两处调用前分别过滤一遍再传给模型，`token` 计数和
  落库用的仍是未过滤的原始 `messages`，不影响审计存档的完整性。另外两个技能的
  `SKILL.md`（`cost-report`/`alipay-report`）输出契约小节各加了一句：`RESULT_PATH`
  已经通过 `save_file` 发给用户，不需要也不要再用 `read_file` 预览，降低触发概率（防御性
  补充，不是必须修复的根因）。已通过真实 `tob` 调试对话验证：连续多轮触发
  `read_file` 预览图片 + 跨越摘要阈值（4 次落库，token 数 4676~8784），`topic_gate`、
  主模型、`ConversationSummaryAuditMiddleware` 均不再 400。

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
| [src/agent/middleware/_multimodal.py](../src/agent/middleware/_multimodal.py) | 新增，`strip_multimodal_content` 共享 helper，供 `main.py`（`topic_gate`）和 `conversation_summary.py` 复用 |
| [src/agent/middleware/conversation_summary.py](../src/agent/middleware/conversation_summary.py) | `abefore_model` 传给结构化摘要模型的消息改用 `strip_multimodal_content` 过滤后的拷贝，`token` 计数/落库仍用原始消息 |
| [src/agent/skills/cost-report/SKILL.md](../src/agent/skills/cost-report/SKILL.md) / [alipay-report/SKILL.md](../src/agent/skills/alipay-report/SKILL.md) | 输出契约小节加了一句：不需要、也不要用 `read_file` 预览 `RESULT_PATH` 图片 |
