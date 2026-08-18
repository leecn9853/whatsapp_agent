# 多渠道接入设计（WhatsApp / toC / toB）

## 背景

目前 agent-server 只有 WhatsApp webhook 是正式对外的入口，[runs.py](../src/agent_server/routes/runs.py)、
[threads.py](../src/agent_server/routes/threads.py) 是靠 [local_only](../src/agent_server/utils/security.py)
锁在本机的调试用 API。现在要新增两类正式使用者：

- **toC**：给最终用户用的 Web 前端，账号+密码登录
- **toB**：给其它后端系统调用的 API，共享密码鉴权

三类调用方（WhatsApp / toC / toB）分开维护，鉴权、请求响应格式各自独立演进，但底层跑 agent 的引擎只有一份，且
所有共用代码集中放在一个目录下，`agent_server/` 顶层只留入口聚合，不再散落 `_engine.py`/`_runtime.py` 之类的文件。

## 目录结构

```
agent_server/
  __init__.py          # 入口：聚合 channels/*/routes.py，挂到 app 上
  shared/               # 三个渠道共用的一切，渠道目录不允许重新实现这里的逻辑
    engine.py           # 原 _engine.py：run_agent_turn，重试/续跑/工具调用事件提取
    runtime.py          # 原 _runtime.py：agent/store/pool/lock_for 等运行时对象
    runs_store.py       # run 记录存储
    utils/
      files.py
      security.py       # local_only（保留给内部调试用）+ 新增的密码校验 helper
  channels/
    whatsapp/           # 现 routes/webhook.py + whatsapp/ 整体迁入，行为不变
      routes.py
      client.py
      processor.py
    toc/                # 新增：面向 Web 前端用户
      routes.py         # LangGraph Server 协议（方案A）
      auth.py           # 账号+密码登录、发会话 token
    tob/                # 新增：面向其它后端系统
      routes.py         # 直接沿用现在 threads.py/runs.py 的语义，只换鉴权
      auth.py           # 校验请求头里的共享密码
```

**原则：`channels/*` 目录下只放"解析请求 → 鉴权 → 调用 `shared/engine.py` → 编码响应"这层薄适配代码。**
重试/续跑逻辑永远只有 `shared/engine.py` 一份；三个渠道各自的 `routes.py`/`auth.py` 不允许绕开它自己重跑一遍。

## 各渠道鉴权

| 渠道 | 鉴权方式 | 归属维度 |
|---|---|---|
| WhatsApp | webhook 签名校验（现有） | 手机号 |
| toC | 账号 + 密码登录，登录成功后发一个会话 token（cookie 或 header 带的 token，具体存储/校验实现在 `channels/toc/auth.py`） | 登录账号的 user_id |
| toB | 全局共享密码：请求头带一个约定好的密码字符串，服务端只校验密码是否对，不做调用方身份核实 | 调用方自己在请求里传的 `external_id`（服务端不做租户级隔离，见下方风险说明） |

> 备注：常见的企业级方案还有 API Key（本质是"每个调用方各自一份密码"，服务端按密码反查是谁）和
> mTLS（双向证书校验，基础设施更重，需要证书分发）。这里 toB 选的是最简单的"全局一个密码"，
> 好处是实现和使用都简单，代价是**任何拿到密码的人都可以在请求里填任意 `external_id` 冒充别的调用方**，
> 密码只起"挡住外部无关人员"的作用，起不到调用方之间的隔离作用。如果后续出现"必须能区分/限流到具体调用方"
> 的需求，需要升级成每调用方一份密码（即 API Key 模式），到时候只改 `channels/tob/auth.py`，不影响其他部分。

`local_only` 保留给内部调试用途（如果还需要的话），toC/toB 的正式路由不再用它兜底。

## thread_id 命名空间

[main.py](../src/agent/main.py) 里 `/memories/` 按 `user_id`（= thread_id）做隔离，三个渠道各自生成
thread_id 的规则不同，为避免不同渠道的 id 互相碰撞，统一加渠道前缀：

- `whatsapp:<phone>`
- `toc:<user_id>`（`user_id` 来自 toC 账号体系，登录后拿到）
- `tob:<external_id>`（`external_id` 由调用方自己在请求里指定；因为 toB 是全局密码、不核实调用方身份，这里
  不再区分 tenant，一个 `external_id` 空间由所有 toB 调用方共享）

`threads.py` 的 `list_threads` 等管理接口据此可以直接看出记录属于哪个渠道。

**不做历史数据迁移**：加前缀之后，`checkpoints` 表（对话历史）和 store 里 `/memories/` 的旧 namespace
（都是裸手机号，没有前缀）直接作废清空，不写兼容判断、不做双写迁移。上线新版本前清空这两处旧数据即可。

## toC 协议：方案A（LangGraph Server 协议）

贴近 LangGraph Server 协议，`channels/toc/routes.py` 提供 `POST /threads`、
`POST /threads/{id}/runs/stream`，SSE 走标准事件类型（`values`/`messages/partial`/… ），前端直接用
`@langchain/react` 的 `useStream`，拿到现成的子agent面板、中断处理等组件。

`routes.py` 内部仍然调用 `shared/engine.py` 的 `run_agent_turn`，只是把它 yield 出来的
工具调用事件/`RunResult`/`RunFailed` 转成 LangGraph Server 协议格式，而不是现在 `runs.py` 里
`tool_call`/`error`/`done` 这套自定义格式——这层转换属于"编码响应"，按上面的原则放在 `toc/routes.py` 里，
不改 `shared/engine.py`。

## toB 接口范围：不新增接口

`channels/tob/routes.py` 直接把现在 [threads.py](../src/agent_server/routes/threads.py)/
[runs.py](../src/agent_server/routes/runs.py) 的路由原样搬过去（`GET /threads`、
`GET /threads/{id}/state`、`DELETE /threads/{id}`、`POST /threads/{id}/runs`，SSE 仍用现在
`tool_call`/`error`/`done` 这套自定义协议），只是把 `@local_only` 换成 `channels/tob/auth.py`
的密码校验。不做批量提交等新接口，等真的有调用方提出需求再加。

## 文件上传/下载

现在 [webhook.py](../src/agent_server/routes/webhook.py) 里的文件校验（只收 `.xlsx`/`.xls`）和落盘逻辑是
WhatsApp 专属的，不在共享引擎里，三个渠道各自处理：

- **WhatsApp**：不变，沿用现在 `webhook.py` 的校验+落盘逻辑。
- **toC**：网页表单/multipart 上传，`channels/toc/routes.py` 自己解析 multipart body、校验扩展名、落盘，
  再调用 `shared/engine.py`。
- **toB**：暂不支持上传。只能通过消息文本触发对已存在文件的操作（比如引用 WhatsApp/toC 渠道已经上传过的文件），
  没有文件上传接口。如果后续 toB 调用方需要传文件，再单独设计（大概率是文件 URL/对象存储引用，而不是直传二进制）。

## 迁移步骤草案

1. 建 `shared/` 目录，把 `_engine.py`→`shared/engine.py`、`_runtime.py`→`shared/runtime.py`、
   `runs_store.py`、`utils/` 挪进去，同步改所有 import；建 `channels/` 目录，把
   `routes/webhook.py` + `whatsapp/` 迁到 `channels/whatsapp/`，行为不变，先验证现有 WhatsApp 流程没有回归
2. 落地 `channels/toc/auth.py`（账号+密码、发会话 token）+ `channels/toc/routes.py`（LangGraph Server 协议 +
   multipart 文件上传）
3. 落地 `channels/tob/auth.py`（共享密码校验）+ `channels/tob/routes.py`（原样搬 threads.py/runs.py 语义，
   不含文件上传）
4. 给三个渠道的 thread_id 统一加前缀；上线前清空旧的 `checkpoints` 表数据和 store 里的旧 `/memories/` 记录
5. 逐步收紧/下线 `local_only` 调试入口，或明确保留为内部专用、和三个正式渠道分开挂载
