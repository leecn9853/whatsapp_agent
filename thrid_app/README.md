GET /api/electronics/orders — 外贸电子产品行业订单数据，不分页，一次性返回 5000 条

GET /api/food-agri/orders?page=1&page_size=99 — 食品/农产品出口行业订单数据，分页，page_size 上限 99（超过返回 422）

启动方式：uv run python main.py（或 uv run uvicorn thrid_app.server:app --reload）

访问 http://127.0.0.1:8000/docs 可查看 Swagger 文档