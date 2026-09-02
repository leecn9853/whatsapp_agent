# WhatsApp Excel Agent

面向内部人员的 Excel / 报表 AI 助手。白名单用户通过 WhatsApp 发消息或 Excel 文件，由 Agent 分析数据、生成图表或产出报表。

## 仓库结构

| 目录 | 说明 |
|------|------|
| [`excel_agent/`](excel_agent/) | 核心 Agent 服务（:8200） |
| [`whatsapp_simulator/`](whatsapp_simulator/) | WhatsApp 接入网关（:3000） |
| [`third_app/`](third_app/) | 报表技能 mock 数据源（:8800） |
| [`content_agent/`](content_agent/) | 实验性内容 Agent，非生产 |

完整架构说明见 [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md)。

## 快速启动

各服务**各自维护独立的 `.env`**，不需要共用一份配置文件。只需保证对接参数一致（见下文）。

```bash
# 1. 基础设施
make infra

# 2. 分别在三个终端启动（或 make dev 一键拉起）
make third-app    # :8800
make agent        # :8200
make simulator    # :3000，首次需扫码

# 3. 检查
make health
```

## 跨服务配置对齐

`whatsapp_simulator` 与 `excel_agent` 是**两个独立部署的服务**，各自读自己的 `.env`。没有「共享配置文件」的要求，只有以下**对接参数**需要互相指向：

| whatsapp_simulator `.env` | excel_agent `.env` |
|---------------------------|-------------------|
| `WEBHOOK_URL=http://localhost:8200/webhook` | `WHATSAPP_SIMULATOR_URL=http://localhost:3000` |

其余变量（API Key、数据库、白名单等）各管各的，互不共享。

## 文档

- [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) — 架构、链路、端口速查
- [excel_agent/README.md](excel_agent/README.md) — Agent 启动与排障
- [whatsapp_simulator/README.md](whatsapp_simulator/README.md) — WhatsApp 网关 API 与运维
- [third_app/README.md](third_app/README.md) — Mock 数据接口
