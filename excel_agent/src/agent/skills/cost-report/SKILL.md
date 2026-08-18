---
name: cost-report
description: 用于生成/查看"成本报表"图片的任务：从第三方数据服务拉取最新的月度成本明细、
  供应商采购数据，重算模板里的汇总公式后，把成本分析看板的某个维度渲染成一张表格图或图表图
  片。适用场景包括"生成最新的成本报表""看一下月度成本趋势/部门预算执行情况/采购类别成本对比"
  "把成本数据画个图表给我"这类请求。
---

# 成本报表图片生成技能

## 核心流程

这不是通用 Excel 处理任务——数据来源（固定的两个接口）、模板结构、看板区块的行列坐标都是
写死的，不需要先用 `inspect_excel` 看结构。直接调用 `generate_cost_report_image` 工具即可：
它会自己拉取最新数据、套用模板、用 LibreOffice 重算汇总公式、把结果画成图片，生成的 PNG 会
自动发给用户，不需要额外调用 `save_file`。**不会**生成或发送 Excel 文件。

工具有两个参数：

- `section`：用户想看哪个维度，不确定时用默认值 `monthly_trend`。
  - `monthly_trend`（默认）：按月份看总支出/总预算/预算执行率/超支情况的趋势。
  - `department_budget`：按部门看年度总成本 vs 年度预算的执行对比。
  - `purchase_category`：按采购类别看供应商采购总额 vs 明细记录总额的对比。
- `render_type`：图片形式，默认 `table`（数据表格图，含合计/平均行，信息最全）；用户明确说
  "图表""趋势图""柱状图""折线图"之类要可视化效果时才传 `chart`（不含合计行，画折线图/柱状图）。

不需要自己判断具体行列范围、也不需要多次调用做数据聚合——一次调用就能完成整个任务。

## 常见请求 → 参数映射

- "生成成本报表" / "看下最新成本情况" → `section=monthly_trend, render_type=table`（都用默认值）。
- "月度成本趋势图" / "画个成本趋势图表" → `section=monthly_trend, render_type=chart`。
- "各部门预算执行得怎么样" → `section=department_budget, render_type=table`。
- "部门预算对比图" → `section=department_budget, render_type=chart`。
- "采购类别成本对比" / "供应商采购花了多少" → `section=purchase_category`。

## 关于失败情况

如果第三方数据服务没启动或 LibreOffice（`soffice`）不可用，工具会返回一段中文错误说明——
直接把这段说明转述给用户即可，不要自己猜测原因或编造替代数据。
