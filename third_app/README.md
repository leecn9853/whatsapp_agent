GET /api/electronics/orders — 外贸电子产品行业订单数据，不分页，一次性返回 5000 条

GET /api/food-agri/orders?page=1&page_size=99 — 食品/农产品出口行业订单数据，分页，page_size 上限 99（超过返回 422）

GET /api/alipay/matching-records?page=1&page_size=200 — 支付宝匹配数据流水明细（整日数据），分页，page_size 上限 500（超过返回 422）。每条记录含 data_category（数据类别）、detail_category（明细分类）、order_amount（订单金额）、status（成功/失败/处理中）、occurred_at、remark；金额区间、成功率、序号不在接口中返回，需下游按 order_amount 分桶、按 status 统计后在 Excel 中生成。

启动方式：uv run python main.py（或 uv run uvicorn thrid_app.server:app --reload）

访问 http://127.0.0.1:8000/docs 可查看 Swagger 文档