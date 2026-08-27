# Tools 瘦身 + 自建 Docker 沙箱：现状与下一步

## TL;DR

- **问题**：`generate_cost_report_image` / `generate_alipay_matching_report` 两个工具的
  docstring 把对应 `SKILL.md` 已经讲过的内容（参数映射、报表结构）又重复了一遍，而
  docstring 是标准 tool schema，每轮请求原样发送，不受 `SkillsMiddleware` 渐进式加载覆盖——
  这是"工具太大"的主要来源。
- **最终方向**：不自己写 dispatcher 工具。把脚本迁进 `skills/*/scripts/`，让 `DockerSandbox`
  做 `main.py` 的默认 backend——`deepagents` 的 `FilesystemMiddleware` 检测到 backend 实现了
  `SandboxBackendProtocol` 会**自动**加一个通用的 `execute(command: str)` 工具，模型读
  `SKILL.md` 知道要跑什么命令后直接调 `execute` 就行，不需要我们再造一个工具。
- **已完成**：① docstring 精简（2026-08-25）；② 沙箱基础设施本身搭好并验证通过
  （2026-08-26，见下文"沙箱阶段一"）。
- **未完成 / 下一步**：把沙箱接成 `main.py` 的默认 backend、脚本迁移进沙箱、`files.py` 改造、
  网络白名单、并发隔离——这些统称"阶段二"，还没有开始，见下文"阶段二：待完成"。

## 背景

[src/agent/main.py](../src/agent/main.py) 绑定给模型的工具目前有 8 个：
`web_search, save_file, list_excel_files, inspect_excel, aggregate_excel_sheet,
create_chart_sheet, generate_cost_report_image, generate_alipay_matching_report`。

其中 `generate_cost_report_image`（[cost_report_tools.py](../src/agent/tools/cost_report_tools.py)）
和 `generate_alipay_matching_report`（[alipay_report_tools.py](../src/agent/tools/alipay_report_tools.py)）
最重——它们是"自包含原子工具"，一次调用里包含"拉接口 → 填模板/建表 → 渲染 → 截图"整条
流水线，docstring 原本把参数含义、报表结构、失败提示全写了一遍。

项目已经在用 `deepagents` 的 `SkillsMiddleware`（`main.py` 里 `skills=["./skills/"]`），
`src/agent/skills/` 下有 `cost-report`、`alipay-report`、`excel-chart` 三个 `SKILL.md`，
符合 [agentskills.io 规范](https://agentskills.io/specification) 的渐进式加载：系统提示里
只放每个技能的 `name`+`description`，完整内容靠模型按需 `read_file`。矛盾点是：两个重工具的
docstring 把 SKILL.md 已经讲过的东西又复述了一遍，这部分没享受到"按需加载"的好处。

`excel_tools.py` 的四个工具（`list_excel_files`/`inspect_excel`/`aggregate_excel_sheet`/
`create_chart_sheet`）不在讨论范围内：它们是通用组合式原子操作，不是某个技能专属的
"一次性脚本"。

## 已确认的方案：自建沙箱 + 框架自带的 `execute` 工具（原方案 B、C 的合并结论，已放弃自造 dispatcher）

论证过程（完整版见 git 历史）浓缩成三条：

1. Anthropic 官方（[Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)）
   把"工具太多、上下文太大"当作要解决的问题，方案是"更少通用工具 + 细节按需从文件系统加载"。
2. 官方版本的执行环境是**沙箱**，不是裸机 shell（Claude Skills 依赖 "Code Execution Tool
   beta"）。项目现成能拿到的 `deepagents.LocalShellBackend` 官方文档明确写"无沙箱、直接在
   宿主机跑，不建议处理不可信输入"——这个 agent 直接对接 WhatsApp 真实用户消息，不能走这条路
   （对应原方案 B1，已否决）。
3. `deepagents` 官方文档（[deepagents/skills#sandbox-scripts](https://docs.langchain.com/oss/python/deepagents/skills#sandbox-scripts)）
   写明：脚本只有通过**沙箱 backend**才能被"执行"。而且不需要我们自己搭一个转发工具——读
   `deepagents` 源码确认，`FilesystemMiddleware` 会在 backend 实现
   `SandboxBackendProtocol` 时**自动**加一个通用 `execute(command: str)` 工具
   （[filesystem.py:1566-1568](../.venv/lib/python3.14/site-packages/deepagents/middleware/filesystem.py#L1566-L1568)）。
   模型读 `SKILL.md` 知道该跑什么命令，直接调框架自带的 `execute` 就行——原计划里的
   `run_skill_script` dispatcher 是重复造轮子，已放弃。

**结论**：脚本迁进 `skills/*/scripts/`，`DockerSandbox` 作为 `main.py` 的默认 backend，靠
框架自带的 `execute` 工具跑脚本，不新增任何自定义工具。分两个阶段落地：

- **阶段一**：只搭建并验证沙箱基础设施本身，不改动任何现有生产代码路径。**已完成。**
- **阶段二**：把沙箱接成默认 backend + 脚本迁移 + `files.py` 改造。**未开始。**

**接线细节**：`supports_execution()` 对 `CompositeBackend` 只看 `backend.default` 是不是
沙箱（[filesystem.py:1458-1459](../.venv/lib/python3.14/site-packages/deepagents/middleware/filesystem.py#L1458-L1459)）。
现在 `main.py` 是 `CompositeBackend(default=fs_backend, routes={"/memories/": ...})`，要让
`execute` 工具出现，必须让沙箱是 `default`，原来"其余不变、只加 `/skills/` 路由"的设想要反过来
——沙箱做默认、把非沙箱的东西（比如 `/memories/`）路由出去。这个反转具体怎么落地是阶段二要
解决的问题。

## 已完成：方案 A，docstring 精简（2026-08-25）

`generate_cost_report_image` / `generate_alipay_matching_report` 的 docstring 从"参数完整
解释 + 报表结构 + 失败提示"精简到只留"决定是否调用/怎么传参"必需的最少信息（防误触发句
原样保留）。被删掉的内容逐句核对过均已在对应 `SKILL.md` 里覆盖（`alipay-report/SKILL.md`
补了一句表头列名，是唯一发现的缺口）。

**局限**：不解决"工具条目数量"问题——8 个工具还是 8 个，只是两个最重的变薄了。真正收敛
数量要靠阶段二把这两个工具换成框架自带的 `execute`。

## 已完成：沙箱阶段一（2026-08-26）

搭建并验证了沙箱基础设施本身（容器、`BaseSandbox` 子类、网络连通性），**没有**接入任何现有
生产代码路径——`DockerSandbox` 还没被 `main.py` 的 `backend=` 使用，纯粹是独立验证过的新增
文件，随时可以整体删掉回退。

### 架构决策（供阶段二复用，未来若要改再回来改这里）

- **docker-py SDK，不用 shell 出去调 `docker` CLI**：直接对接 daemon API，异常类型
  （`docker.errors.APIError`）比解析 CLI stderr 更容易做结构化错误处理，跟 `BaseSandbox`
  "upload/download 要把错误装进 response 字段而不是抛异常"的契约更好对接。
- **单个长驻容器，不做池化**：`docker compose up -d sandbox` 启动时用
  `CMD ["sleep", "infinity"]` 保持存活，`execute()` 每次对同一容器 `exec_run`。阶段一只验证
  机制能不能跑通，不验证生产并发安全——多用户并发写冲突的隔离设计留给阶段二。
- **网络**：默认桥接网络 + `host.docker.internal`（Docker Desktop 自动注入，Mac 上不需要
  `extra_hosts` 配置）。出口没有做白名单，能访问外网+宿主机，不做限制——这是阶段二接入生产
  流量前需要单独做的加固项。
- **代码落点**：`src/agent/backends/docker_sandbox.py` 新增 `DockerSandbox(BaseSandbox)`，
  不改 `main.py`，保证阶段一纯增量。

### 实现与验证结果

- 新增依赖：`docker>=7.1.0`（实装 `docker==7.2.0`），`pyproject.toml` + `uv sync`。
- [sandbox/Dockerfile](../sandbox/Dockerfile)：`python:3.14-slim` 最小镜像，仅装 curl；
  阶段二迁移脚本时需要加 LibreOffice（`soffice`）+ 项目 Python 依赖。
- [docker-compose.yml](../docker-compose.yml)：新增 `sandbox` service，容器名
  `excel_agent-sandbox-1`（compose 项目名 `excel_agent` + service 名 `sandbox` 的默认命名）。
- [src/agent/backends/docker_sandbox.py](../src/agent/backends/docker_sandbox.py)：
  `DockerSandbox(BaseSandbox)` 实现 `execute`/`upload_files`/`download_files`/`id`。
- [sandbox/smoke_test.py](../sandbox/smoke_test.py)：手动验证脚本（不接入 CI）。运行方式：
  ```
  docker compose up -d sandbox
  uv run python -m sandbox.smoke_test
  ```
  （直接 `python sandbox/smoke_test.py` 会因为脚本目录被加进 `sys.path[0]` 而找不到 `src`
  包，必须用 `-m` 从仓库根目录跑。）
- third_app（成本报表/支付宝报表用的模拟第三方接口）服务端代码已搬到仓库外
  `/Users/CoderYing/Projects/whatsapp_agent/third_app`，`main.py` 绑
  `host="0.0.0.0", port=8800`，沙箱容器通过 `host.docker.internal:8800` 直连，`/docs`
  返回 `200`——网络连通性已用真实地址验证过，不是临时测试端口。
- 三条验证全部通过：`execute` 拿到 `Python 3.14.7`/`exit_code=0`；upload/download 往返内容
  一致；网络连通 third_app 返回 `200`。
- 性能：单次 `exec_run` ~0.033s，容器冷启动（`stop`→`up`→能 `exec`）~0.59s——都很小，阶段二
  沿用单容器不池化没有明显性能问题；真正瓶颈预期在阶段二接入 LibreOffice/`soffice` 调用之后，
  需要单独测。

## 阶段二：待完成（尚未开始）

1. 把两个工具的完整流水线逻辑迁移成沙箱内可执行的脚本
   （`skills/cost-report/scripts/generate.py`、`skills/alipay-report/scripts/generate.py`），
   镜像加装 LibreOffice + 项目 Python 依赖（`openpyxl`/`httpx` 等）。
2. 删掉 `generate_cost_report_image` / `generate_alipay_matching_report` 两个绑定工具；把
   `DockerSandbox` 接成 `main.py` 里 `CompositeBackend` 的 `default`（现在是 `fs_backend`），
   让框架自动加上 `execute` 工具。工具数 8 → 7（`execute` 是框架自带的，不用我们再写）。
3. 原来 `default=fs_backend` 承担的东西（普通文件系统读写）和 `/memories/` 路由怎么摆——
   沙箱做 `default` 之后，非沙箱场景的路由方式需要重新设计（哪些路径继续走 `fs_backend`，
   哪些走沙箱）。
4. `execute` 工具运行在沙箱容器的文件系统里，跟宿主机/`fs_backend` 的路径不是同一套——
   `SKILL.md` 里怎么告诉模型脚本的调用命令、脚本文件本身怎么进沙箱（是打进镜像还是靠
   `upload_files` 同步）要定下来。
5. **文件输出检测机制要重新设计**：原来 `files.py` 的 `FILE_OUTPUT_TOOL_NAMES` 靠工具名
   （`generate_cost_report_image` 等）匹配 `ToolMessage.name` 来判断"要不要自动发文件"。
   换成通用 `execute` 之后工具名永远是 `execute`，这条路径完全失效，而且 `execute` 的
   `ExecuteArtifact`（[protocol.py:800-817](../.venv/lib/python3.14/site-packages/deepagents/backends/protocol.py#L800-L817)）
   只带 `exit_code`，不带产物路径信息——需要新想一个机制（比如脚本按约定把输出路径打印到
   stdout，或者模型跑完 `execute` 后自己再调 `download_files`/`save_file` 把产物取出来）。
   这是从"自造 dispatcher"换成"框架自带 execute"之后新增的一个待解决问题，之前 dispatcher
   方案下可以用结构化返回值绕开，现在绕不开了。
6. `runtime.context`（`report_id`/`user_id` 等）怎么传给沙箱里的脚本——现在两个 `@tool`
   函数靠 `ToolRuntime[ContextSchema]` 自动注入，`execute` 是纯 shell 命令，参数只能靠命令行
   参数/环境变量传，注入路径要重新设计。
7. 出口网络白名单：阶段一确认了默认桥接网络能直连 third_app，阶段二决定要不要收紧到"只放行
   third_app，阻断其余出网"。
8. 并发场景下的容器隔离/池化设计：同一时间多个 WhatsApp 用户各自生成报表，互不干扰（阶段一
   只有一个长驻容器，没考虑这个）。
9. 子代理权限限定：`web-search-agent` 等子代理（`main.py` 里用"传入具体工具列表"的方式）以后
   如果要接入沙箱，`execute` 是完全通用的 shell 工具，没有天然的"只能跑某个技能脚本"限制，
   要单独设计怎么限定子代理不会被开放到任意命令执行。

## 关键文件

| 文件 | 状态 |
| --- | --- |
| [src/agent/tools/cost_report_tools.py](../src/agent/tools/cost_report_tools.py) | docstring 已精简；流水线逻辑待阶段二迁移 |
| [src/agent/tools/alipay_report_tools.py](../src/agent/tools/alipay_report_tools.py) | 同上 |
| [src/agent/skills/*/SKILL.md](../src/agent/skills/) | 已覆盖被精简掉的 docstring 内容 |
| [sandbox/Dockerfile](../sandbox/Dockerfile) | 阶段一最小镜像，阶段二需加 LibreOffice + 依赖 |
| [docker-compose.yml](../docker-compose.yml) | `sandbox` service 已加 |
| [src/agent/backends/docker_sandbox.py](../src/agent/backends/docker_sandbox.py) | `DockerSandbox` 已实现，未接入 `main.py` |
| [sandbox/smoke_test.py](../sandbox/smoke_test.py) | 手动验证脚本，已通过 |
| [src/agent/main.py](../src/agent/main.py) | 阶段二要把 `DockerSandbox` 接成 `CompositeBackend` 的 `default`，删掉两个绑定工具 |
| [src/agent_server/shared/files.py](../src/agent_server/shared/files.py) | 阶段二要改自动发文件判断逻辑 |
