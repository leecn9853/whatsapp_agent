# 多渠道接入设计（WhatsApp / toC / toB）

## 背景

目前 agent-server 只有 WhatsApp webhook 是正式对外的入口，[runs.py](../src/agent_server/routes/runs.py)、
[threads.py](../src/agent_server/routes/threads.py) 是靠 [local_only](../src/agent_server/utils/security.py)
锁在本机的调试用 API。现在要新增两类正式使用者：

- **toC**：给最终用户用的 Web 前端（可能基于 `@langchain/react` 的 `useStream`，也可能自研）
- **toB**：给其它后端系统调用的 API

三类调用方（WhatsApp / toC / toB）分开维护，鉴权、请求响应格式各自独立演进，但底层跑 agent 的引擎只有一份。

## 目录结构

```
agent_server/
  _engine.py       # run_agent_turn：重试/续跑/工具调用事件提取，三个渠道共用，不拆分
  _runtime.py       # 共享的 agent/store/pool 等运行时对象
  runs_store.py     # run 记录存储，共用
  utils/            # 共用工具（files.py 等）
  channels/
    whatsapp/       # 现 routes/webhook.py + whatsapp/ 整体迁入
      routes.py
      client.py
      processor.py
    toc/            # 新增：面向前端用户
      routes.py
      auth.py       # 该渠道专属的鉴权
    tob/            # 新增：面向其它后端系统
      routes.py
      auth.py
  __init__.py       # 聚合 channels/*/routes.py，同现在 routes/__init__.py 的写法
```

**原则：三个 channels 目录下只放"解析请求 → 鉴权 → 调用 `run_agent_turn` → 编码响应"这层薄适配代码。**
不允许在某个渠道目录里重新实现一份重试/续跑逻辑——那部分永远只有 `_engine.py` 一份。

## 各渠道鉴权（待定，见下方待确认问题）

| 渠道 | 鉴权方式 | 归属维度 |
|---|---|---|
| WhatsApp | webhook 签名校验（现有） | 手机号 |
| toC | 登录会话（JWT / cookie） | 登录用户 id |
| toB | API Key / mTLS | 调用方 tenant |

`local_only` 装饰器保留给内部调试用途（如果还需要的话），toC/toB 的正式路由不能再靠它兜底。

## thread_id 命名空间

[main.py](../src/agent/main.py) 里 `/memories/` 按 `user_id`（= thread_id）做隔离，三个渠道各自生成
thread_id 的规则不同（WhatsApp 用手机号），为避免不同渠道的 id 互相碰撞，统一加渠道前缀：

- `whatsapp:<phone>`
- `toc:<user_id>`
- `tob:<tenant>:<external_id>`

`threads.py` 的 `list_threads` 等管理接口据此可以直接看出记录属于哪个渠道。

## toC 协议：两个方案，待选

**方案A：贴近 LangGraph Server 协议**，换取直接用 `@langchain/react` 的 `useStream`
（`POST /threads`、`POST /threads/{id}/runs/stream` 走标准 SSE 事件类型 `values`/`messages/partial`/…）。
工作量大，好处是前端拿现成组件（子agent面板、中断处理）。

**方案B：沿用现在 `runs.py` 的自定义 SSE 协议**（`tool_call`/`error`/`done`），前端自己写 `EventSource`
消费逻辑，不依赖 `useStream`。工作量小，但前端要自己维护流式状态。

> 待确认：toC 前端团队打算自己写 hook，还是要接现成的 `@langchain/react` 组件？决定了才能定 `channels/toc/routes.py`
> 的响应格式，这个应该在动手写 toC 路由之前先敲定。

## toB 接口范围（待确认）

现在 [threads.py](../src/agent_server/routes/threads.py)/[runs.py](../src/agent_server/routes/runs.py)
这组通用 API 具体要不要原样变成 toB 的接口，还是 toB 需要额外的批量接口（比如一次提交多个 thread 的任务），
需要和 toB 的实际调用方对一下需求。

## 迁移步骤草案

1. 建 `channels/` 目录，把现有 `routes/webhook.py` + `whatsapp/` 迁到 `channels/whatsapp/`，行为不变，先验证现有
   WhatsApp 流程没有回归
2. 确认 toC 协议方案（A/B），落地 `channels/toc/routes.py` + 对应鉴权
3. 确认 toB 接口范围，落地 `channels/tob/routes.py` + API Key 鉴权
4. 给三个渠道产生的 thread_id 统一加前缀；历史数据（现有 WhatsApp thread，如果没加前缀）需要一次性迁移或做兼容判断
5. 逐步收紧/下线 `local_only` 调试入口，或明确保留为内部专用、和三个正式渠道分开挂载
