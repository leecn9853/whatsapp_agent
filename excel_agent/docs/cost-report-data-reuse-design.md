# 成本报表：同一用户多次展示复用数据（设计草案，未实现）

## 背景

`generate_cost_report_image`（[src/agent/tools/cost_report_tools.py](../src/agent/tools/cost_report_tools.py)）
是一个完全自包含的原子工具：每次调用都会重新走一遍"拉两个接口（`thrid_app`）→ 填模板 →
LibreOffice 重算 → 截图"的完整流程，没有任何跨调用的缓存或复用逻辑。

问题场景：同一会话里用户先要了一种展示形式（比如图表），过一会儿又要另一种展示形式（比如
表格）——这两次调用会各自重新拉一遍数据。如果第三方数据服务在这期间有变化，两张图看到的
数字可能不一致；即使数据没变，也白白多拉了两次接口 + 多跑了一次 LibreOffice 重算（相对较慢）。

## 结论：靠 LLM 猜用户意图，不做自动判断

要不要复用数据，本质是"用户这句话指的是不是同一批数据"，这是语义判断，工具自己没法从
`render_type`/`section` 这类结构化参数里推断出来。可行方向是加一个新参数（比如
`reuse_last_data: bool`），在 docstring 里教会调用的 LLM：

- 用户说"换个图表/表格看看""这些数据也画个表格"之类**明确指向"刚才这批数据"**时，传 `True`。
- 用户说"最新的""重新看一下""刷新一下"，或者没有明确指向历史数据时，保持默认 `False`（拉最新）。

这本质是让 LLM 猜意图，猜错的后果：该刷新时用了旧数据，或该沿用时又重新拉了一遍——不是
100% 可靠，但比"永远重新拉"或"永远复用"两个极端更贴近用户实际想要的效果。

## 缓存层具体设计

**缓存的是什么**：`recalculate_with_libreoffice` 算出来的那份 xlsx（`recalculated_path`）。
这一份同时包含了两个耗时步骤的结果（拉接口写进模板、LibreOffice 重算公式），且
`render_dashboard_screenshot`/`render_chart_screenshot` 两个截图函数都只认这一份文件，不管
`render_type` 是哪个——缓存这一份就够两种展示形式复用。

**放哪**：复用现在已有的按用户分文件夹机制（`_snapshot_user_folder`），在下面加一个**固定
文件名**的子目录（不像现在 `snapshot_dir` 是按 `report_id` 分文件夹、每次调用都是新的）：

```
snapshots/<user_folder>/_last_fetch/recalculated.xlsx
snapshots/<user_folder>/_last_fetch/meta.json
# meta.json: {"report_id": ..., "fetch_time": ..., "monthly_count": 1980, "supplier_count": 2050}
```

`meta.json` 存的是返回消息里要用到的那几个数字（"已拉取最新的月度成本明细 X 条…"这句话），
复用时没有重新拉接口，这几个数字得从缓存的元数据里读。

**写入时机**：只要这次调用是真的拉了新数据（`reuse_last_data=False`，或者传了 `True` 但缓存
还不存在），拉完、重算完之后，把 `recalculated_path` 覆盖复制到 `_last_fetch/recalculated.xlsx`，
`meta.json` 也覆盖写一份——永远只保留"这个用户目前为止最后一次成功拉到的数据"，不留历史，
被下一次真实拉取覆盖为止。

**读取时机**：`reuse_last_data=True` 且 `_last_fetch/recalculated.xlsx` 存在时，直接跳过
`_build_filled_workbook`（两次 HTTP 请求）和 `recalculate_with_libreoffice`（一次 LibreOffice
子进程），把 `recalculated_path` 指向这份缓存文件，`report_id`/数据条数从 `meta.json` 读，
直接进截图那一步。缓存不存在时（会话里第一次调用、或用户从没成功拉过数据）自动退回到拉
新数据，不报错。

## 待定问题

- **是否需要过期时间**：当前设计没有 TTL，缓存就是"这个用户目前为止最后一次成功拉取"，
  没有时间上限。要不要加一个上限（比如超过 N 分钟就不算"最近"，强制重新拉），还没定。
- **`reuse_last_data` 参数命名和默认值**：暂定默认 `False`（保持现在"总是拉最新"的安全默认
  行为），只有 LLM 判断出用户明确要"沿用刚才这批数据"时才传 `True`。
- **是否要让复用请求沿用同一个 `report_id`**：如果图表和表格来自同一批数据，footer 上的
  报表ID 是否也应该保持一致，方便用户/排查问题时知道"这两张图是同一批数据"——目前倾向于
  是，但还没最终定。

## 状态

未实现，仅作设计记录，后续再讨论是否要做、怎么做。
