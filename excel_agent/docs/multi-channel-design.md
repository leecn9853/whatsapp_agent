# 多渠道接入设计（toC）

## 背景

WhatsApp、toB 两个渠道已经迁移/落地完成（`channels/whatsapp/`、`channels/tob/`），
具体结构和实现以代码为准，不在本文重复。本文只保留 **toC**（给最终用户用的 Web 前端，
账号+密码登录）还未落地的设计。

toC 复用的共享代码在 `agent_server/shared/`（`engine.py`/`runtime.py`/`runs_store.py`/
`thread_ids.py`/`memory.py`/`files.py`/`security.py`），原则不变：
**`channels/toc/` 下只放"解析请求 → 鉴权 → 调用 `shared/engine.py` → 编码响应"这层薄适配代码**，
重试/续跑逻辑永远只在 `shared/engine.py` 里，不允许绕开它自己重跑一遍。

## 目录结构（待落地）

```
agent_server/
  channels/
    toc/
      routes.py         # LangGraph Server 协议（方案A）
      auth.py            # 账号+密码登录、发会话 token
```

## 鉴权

账号 + 密码登录，登录成功后发一个会话 token（cookie 或 header 带的 token，具体存储/校验实现在
`channels/toc/auth.py`），归属维度是登录账号的 `user_id`。toC 的正式路由不用 `local_only` 兜底。

## thread_id 命名空间

统一前缀方案里 toC 对应 `toc:<user_id>`（`user_id` 来自 toC 账号体系，登录后拿到）。落地时记得
在 [main.py](../src/agent/main.py) 的 `/memories/` 隔离逻辑和 `shared/thread_ids.py` 里补上这个前缀
（此前迁移时 toC 部分留白未加）。

## 长期记忆查看（本轮不实现，留在这里记录）

以后如果要给用户加一个"查看我的记忆"的自助入口，`channels/toc/routes.py` 应该复用
`shared/memory.py` 的 `aget_memory`，只允许查登录用户自己的 thread（不像 `tob/admin.py`
那样不做渠道/用户过滤），不要重新拼一遍 namespace escaping 规则。

## toC 协议：方案A（LangGraph Server 协议）

贴近 LangGraph Server 协议，`channels/toc/routes.py` 提供 `POST /threads`、
`POST /threads/{id}/runs/stream`，SSE 走标准事件类型（`values`/`messages/partial`/… ），前端直接用
`@langchain/react` 的 `useStream`，拿到现成的子agent面板、中断处理等组件。

`routes.py` 内部仍然调用 `shared/engine.py` 的 `run_agent_turn`，只是把它 yield 出来的
工具调用事件/`RunResult`/`RunFailed` 转成 LangGraph Server 协议格式，而不是 toB 那套
`tool_call`/`error`/`done` 自定义格式——这层转换属于"编码响应"，按上面的原则放在
`toc/routes.py` 里，不改 `shared/engine.py`。

## 文件上传/下载

WhatsApp 专属的文件校验（只收 `.xlsx`/`.xls`）+ 落盘逻辑不在共享引擎里，toC 需要自己实现：
网页表单/multipart 上传，`channels/toc/routes.py` 自己解析 multipart body、校验扩展名、落盘，
再调用 `shared/engine.py`。

## 待办

1. `channels/toc/auth.py`（账号+密码、发会话 token）
2. `channels/toc/routes.py`（LangGraph Server 协议 + multipart 文件上传）
3. `shared/thread_ids.py` 补 `toc:<user_id>` 前缀，`main.py` 的 `/memories/` 隔离逻辑同步更新
