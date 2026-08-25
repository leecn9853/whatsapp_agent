# Tools 瘦身：把技能脚本迁入 skills/，用渐进式加载缩小上下文（规划草案，未实现）

## 背景

当前绑定给模型的工具列表（[src/agent/main.py](../src/agent/main.py) 第 54-63 行）有 8 个：

```
web_search, save_file, list_excel_files, inspect_excel,
aggregate_excel_sheet, create_chart_sheet,
generate_cost_report_image, generate_alipay_matching_report
```

每一个绑定工具的函数签名 + docstring，都会被序列化进**每一轮**发给模型的 tool schema
里——不管这一轮任务用不用得上。逐个测了一下 docstring 长度（不含模块级 docstring，只算
真正挂在 `@tool` 函数上的那段）：

| 工具 | docstring 长度（字符） |
| --- | --- |
| `generate_cost_report_image`（[cost_report_tools.py](../src/agent/tools/cost_report_tools.py)） | ~700 |
| `generate_alipay_matching_report`（[alipay_report_tools.py](../src/agent/tools/alipay_report_tools.py)） | ~760 |
| `inspect_excel` / `aggregate_excel_sheet` / `create_chart_sheet`（[excel_tools.py](../src/agent/tools/excel_tools.py)） | 各 ~200-500 |
| `save_file` | ~220 |

`generate_cost_report_image` 和 `generate_alipay_matching_report` 这两个最重——因为它们是
"自包含原子工具"：一次调用里包含"拉接口 → 填模板/建表 → 渲染 → 截图"整条流水线，docstring
里把参数含义、报表结构、失败提示全写了一遍，供模型在**决定要不要调用**和**怎么传参**时看。

同时，项目已经在用 `deepagents` 的 `SkillsMiddleware`（`main.py` 第 252 行
`skills=["./skills/"]`），`src/agent/skills/` 下已有 `cost-report`、`alipay-report`、
`excel-chart` 三个 `SKILL.md`，符合 [agentskills.io 规范](https://agentskills.io/specification)
的渐进式加载：系统提示里只放每个技能的 `name` + `description`（几十字），完整内容要模型自己
`read_file` 按需读取（见 `deepagents/middleware/skills.py` 里的 `SKILLS_SYSTEM_PROMPT`）。

**矛盾点**：SKILL.md 本身已经渐进式加载了，但 `generate_cost_report_image` /
`generate_alipay_matching_report` 这两个工具的 docstring 里又把 SKILL.md 已经讲过的东西
（参数映射、报表结构、失败文案）重复了一遍——这部分是**标准工具 schema**，不受渐进式加载
覆盖，每轮请求原样发送，没有享受到"按需"的好处。这是当前"tools 太大"的主要来源。

`excel_tools.py` 的四个工具（`list_excel_files` / `inspect_excel` / `aggregate_excel_sheet` /
`create_chart_sheet`）不在这次讨论范围内：它们是通用组合式原子操作（读表结构、聚合、画图各自
独立，被上层按需链式调用），不是某个技能专属的"一次性脚本"，硬塞进某个 `skills/*/scripts/`
里并不合适。

## 目标

在不牺牲现有功能/产物契约（[files.py](../src/agent_server/shared/files.py) 靠工具名匹配
`response_format="content_and_artifact"` 自动把生成的图片发给用户这条逻辑）的前提下，减少
`cost-report`、`alipay-report` 这两个技能在**每轮请求的标准 tool schema**里占用的体量。

## 官方依据：这么做符不符合最佳实践

查了 agentskills.io 规范原文和 Anthropic 官方工程博客，三点结论：

1. **规范本身不规定 `scripts/` 怎么被执行**。[agentskills.io/specification](https://agentskills.io/specification)
   原文：`scripts/`: "Contains executable code that agents can run... **Supported languages depend
   on the agent implementation**"——目录结构和 `SKILL.md` 格式是标准化的，但"脚本怎么被调用"
   完全留给具体 agent 实现决定，不存在"官方标准做法必须走 shell/subprocess"这种强制要求。
2. **Anthropic 官方确实把"工具太多、上下文太大"当成一个要解决的问题**，方案就是"用更少的
   通用工具+让细节按需从文件系统里加载"。见
   [Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)：
   把每个能力都注册成独立绑定工具是低效的（文中例子：传统做法 150,000 tokens vs 用代码执行
   方式 2,000 tokens，省 98.7%），做法是不逐个绑工具，而是把工具实现当作代码放在文件系统里，
   给模型**一个**通用执行入口，按需发现、按需加载定义——跟本文方案 B 的 dispatcher 思路是
   同一个形状：**1 个通用入口替换 N 个各自带完整 schema 的工具**。
3. **但官方版本的执行环境是沙箱，不是裸机 shell**。Claude 自家 Skills 功能
   （[claude.com/blog/skills](https://claude.com/blog/skills)）明确写了 Skills 依赖
   "Code Execution Tool beta"——一个**安全沙箱**，不是"直接在宿主机跑命令"。这跟项目里现成能
   拿到的 `deepagents.LocalShellBackend`（官方文档写明"无沙箱、直接在宿主机跑，不建议处理
   不可信输入"）完全不是一个安全级别。

**落到我们的场景**：官方最佳实践的本质是"更少通用工具 + 细节按需加载"，这一点方案 B2 完全
做到（1 个 dispatcher 工具，细节留在 SKILL.md 和脚本文件里，不预先塞进 schema）。跟官方完整
模式相比，B2 少的是"模型现场写新代码"这层自由度——官方需要沙箱兜底就是为了安全地给这层自由度，
而我们的场景是几条固定的报表流水线，压根不需要"模型现场写代码"，白名单式函数调用完全够用，
所以合理地跳过"引入沙箱"这一步，不算打折扣，是按需匹配能力、不多引入不需要的自由度和风险。

**结论：确认采用方案 B2**（受限白名单 dispatcher，不接 shell/无沙箱执行）。下文方案 B 的
描述均以 B2 为准，B1 保留记录仅作对比参考，不作为待实施方案。

### 补充概念澄清：dispatcher 和"脚本"到底是什么

- `run_skill_script` 这个工具本身不"执行"任何文件，就是一个查表转发的普通函数：按
  `(skill, script)` 查一张固定注册表，转发成一次普通的 Python 函数调用，参数校验后传入。
- `skills/*/scripts/*.py` 里的"脚本"，在 B2 语境下就是**普通的可 import 的 Python 模块**（暴露
  一个入口函数，比如 `def generate(render_type, runtime): ...`），不是"可从命令行独立跑的程序"。
  dispatcher 是 `import` 这个模块调用函数，不是 `subprocess.run(["python", "generate.py"])`
  起子进程——跟现在 `cost_report_tools.py` 里 `from src.agent.tools._cost_report_render import
  render_chart_screenshot` 这句 import 本质上是一回事，只是文件挪了目录、多了一层 dispatcher
  转发。脚本内部原有的文件读写、`subprocess.run(["soffice", ...])` 调 LibreOffice 等操作全部
  保留、不受影响——这些参数从头到尾是代码内部生成的临时路径，不经过模型/用户输入，跟"要不要
  给模型 shell 权限"是两个独立问题。
- SKILL.md 里描述"调用 xxx 脚本"这句话，实际指向的是"模型该调用
  `run_skill_script(skill=..., script=..., args=...)` 这个工具、传什么参数"，不是字面意义上的
  "运行这个文件"。

## 方案 A：仅物理搬迁 + 精简 docstring（低风险，未采用，作对比参考）

### 改动内容

1. 把渐进式流水线里"纯逻辑"部分迁到对应技能目录下的 `scripts/`，比如：
   - `src/agent/tools/_cost_report_render.py` → `src/agent/skills/cost-report/scripts/render.py`
   - `src/agent/tools/_report_screenshot.py`、`_report_snapshot.py`、`_naming.py` 中被
     cost-report/alipay-report 共用的部分，可以放进一个共享位置（比如
     `src/agent/skills/_shared/scripts/`），避免两个技能各拷一份。
   - `src/agent/tools/_alipay_report_render.py` → `src/agent/skills/alipay-report/scripts/render.py`
2. `cost_report_tools.py` / `alipay_report_tools.py` 里的 `@tool` 函数**保留原地**（还在
   `src/agent/tools/`，因为 `main.py` 需要 import 它们注册进 `tools` 列表），只是把 import 路径
   改成指向 `skills/*/scripts/`。
3. 把两个 `@tool` 函数的 docstring 从现在的"参数 + 报表结构 + 常见请求映射 + 失败提示"精简到
   只留**模型决定是否调用/怎么传参数所必需的最少信息**（比如 `generate_cost_report_image` 只
   留 `render_type` 两个取值的一句话区分），报表结构、字段口径、失败文案说明这些"人类/模型
   事后核对用"的内容整段移进对应 `SKILL.md`（已经是渐进式加载，不占每轮标准开销）。

### 优点

- 不新增任何执行面（还是普通 Python 函数调用），安全性和现在完全一致。
- 目录结构符合 agentskills.io 规范（技能自带的脚本放在技能目录下），便于以后新增技能时照抄
  这个结构。
- docstring 精简是立即生效的收益：两个最重的工具 schema 直接砍掉大半篇幅。
- 改动范围可控，主要是移动文件 + 改 import + 削减 docstring 文字，`files.py` 的
  `COST_REPORT_OUTPUT_TOOL_NAMES` / `ALIPAY_REPORT_OUTPUT_TOOL_NAMES` 匹配逻辑完全不受影响。

### 缺点 / 局限

- **工具条目数量不变**（还是 8 个绑定工具），只是把其中两个变薄了——如果以后技能继续增多，
  "工具列表越长"这个问题本身没有解决。
- 精简 docstring 需要小心："模型决定是否调用"所需的关键判断信息（比如"仅当用户明确要生成/
  查看报表时才调用"这类防误触发的句子）不能删,否则可能增加误调用概率。

## 方案 B（已确认采用 B2）：引入受限白名单 dispatcher 工具

### 改动内容

1. 把 `generate_cost_report_image` / `generate_alipay_matching_report` 的完整流水线逻辑做成
   `skills/cost-report/scripts/generate.py`、`skills/alipay-report/scripts/generate.py`
   ——每个都是可以独立跑的入口（接收参数、返回结果路径）。
2. 用**一个**通用工具替换掉这两个绑定工具，例如 `run_skill_script(skill: str, args: dict)`。
   这个工具的 docstring 可以很短："运行某个技能自带的脚本，脚本名称/参数从对应 SKILL.md 里
   获取"——具体每个脚本要传什么参数，这部分说明彻底移进 SKILL.md，模型需要用的时候自己先
   `read_file` 看清楚再调用。工具列表从 8 个降到 7 个（以后每加一个"一次性脚本"型技能，工具
   数都不再增加）。
3. `run_skill_script` 内部怎么执行 `scripts/generate.py`，有两种做法，安全性天差地别：
   - **B1：真正 shell/subprocess 执行**——依赖 `deepagents.backends.LocalShellBackend.execute`
     之类的能力。这个 backend 的官方文档明确写了"无沙箱、无隔离，命令直接在宿主机上以当前
     用户权限运行"，并且**不建议**用在"处理不可信用户输入"的场景——而这个 agent 是直接对接
     WhatsApp 真实用户消息的生产服务，用户的自然语言最终会影响到传给脚本的参数，这条边界一旦
     开了口子，风险评估要单独做（比如是否要接 HITL 人工审核中间件、是否要限制脚本目录之外的
     任何路径访问等）。
   - **B2：受限白名单 dispatcher**——`run_skill_script` 内部不真的起子进程，而是维护一个
     `{("cost-report", "generate"): generate_cost_report_image_impl, ...}` 的固定注册表，按
     `skill` + `script` 查表调用，参数用 pydantic/dict 校验。本质还是普通函数调用，没有新增
     任何执行面，只是把多个工具的 schema 合并成一个——收益类似"减少工具条目数"，但不是真正
     意义上的"脚本执行"，某种程度上只是把方案 A 的"合并成一个工具"这一步做到底。
4. 无论 B1/B2，都要同步改 [files.py](../src/agent_server/shared/files.py)：现在
   `FILE_OUTPUT_TOOL_NAMES` 是靠具体工具名（`generate_cost_report_image` 等）匹配
   `ToolMessage.name` 来判断"这次工具调用的返回值要不要自动发文件给用户"。换成单一
   `run_skill_script` 之后，工具名不再能区分技能，需要改成按 `skill` 参数或返回内容里的某个
   标记字段来判断是否需要自动发文件。

### 优点

- 工具列表条目数量和标准 schema 体量都能进一步压缩，且是可扩展的收益——以后新增"一次性脚本"
  型技能不需要再多绑一个工具。
- 更贴近 Anthropic Agent Skills 规范里"code execution with skills"的完整设计意图（skill 提供
  脚本，agent 按需执行，而不是每个脚本都单独包一层工具定义）。

### 缺点 / 风险

- B1 涉及安全边界的实质性变化（无沙箱 shell 执行），对一个直接服务真实 WhatsApp 用户的生产
  agent 而言是需要认真评估的决策，不是纯技术选型问题。
- B2 不新增执行面，但需要重新设计 `runtime.context`（`report_id`/`user_id` 等）怎么传进
  dispatcher、`files.py` 的自动发文件逻辑要跟着改，改动范围比方案 A 大出一圈。
- 无论哪种，`SubAgent`/子代理（如 `web-search-agent`）目前用的是"传入具体工具列表"的方式
  （`main.py` 第 73-79 行），如果以后要给子代理也接入某个技能脚本，dispatcher 模式下要多想一步
  怎么限定子代理只能调它被允许的那个技能/脚本，避免权限一次性放开到"所有技能脚本"。

## 方案对比

| | 方案 A | 方案 B |
| --- | --- | --- |
| 是否新增执行面 | 否 | B1 是 / B2 否 |
| 工具条目数量 | 不变（8个） | 减少（7个，且可扩展） |
| 每轮标准 schema 体量 | 明显减小（两个最重的工具变薄） | 更小 |
| 改动范围 | 移文件 + 改 import + 削减文字 | 新增 dispatcher + 改 files.py + 重新设计参数传递 |
| 风险 | 低 | B1 高（安全边界）/ B2 中（架构改动面） |
| files.py 是否要改 | 不需要 | 需要 |

## 待定问题

- dispatcher 注册表/参数校验的具体形态（`skill`+`script` 两级 key，还是别的组织方式）。
- `runtime.context`（`report_id`/`user_id` 等）怎么传进 dispatcher 再传给具体脚本函数——现在
  两个 `@tool` 函数是靠 `ToolRuntime[ContextSchema]` 参数自动注入的，换成 dispatcher 转发之后
  这条注入路径要重新设计。
- `files.py` 的 `FILE_OUTPUT_TOOL_NAMES` 改成按什么字段判断"要不要自动发文件"（`skill` 参数？
  返回内容里的标记字段？）。
- 子代理（`web-search-agent` 等）以后要不要接入某个技能脚本时，怎么限定它只能调被允许的那个
  技能/脚本，不要一次性放开到"所有技能脚本"。

## 状态

方向已确认：采用方案 B2（受限白名单 dispatcher，不接 shell）。下一步进入详细实施规划
（dispatcher 设计、文件迁移清单、`files.py` 改动、SKILL.md 补充内容）。
