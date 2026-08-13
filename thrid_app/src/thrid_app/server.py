from fastapi import FastAPI, Query

from thrid_app.data_generator import generate_electronics_orders, generate_food_agri_orders
from thrid_app.schemas import OrderRow, PagedOrders

app = FastAPI(title="Mock Trade Data Service")

_ELECTRONICS_ORDERS = generate_electronics_orders(5000)
_FOOD_AGRI_ORDERS = generate_food_agri_orders(5000)


@app.get("/api/electronics/orders", response_model=list[OrderRow])
def get_electronics_orders():
    """外贸电子产品行业订单数据，一次性返回全部数据，不分页。"""
    return _ELECTRONICS_ORDERS


@app.get("/api/food-agri/orders", response_model=PagedOrders)
def get_food_agri_orders(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=99),
):
    """食品/农产品出口行业订单数据，支持分页，每页最多 99 条。"""
    start = (page - 1) * page_size
    end = start + page_size
    return PagedOrders(
        total=len(_FOOD_AGRI_ORDERS),
        page=page,
        page_size=page_size,
        items=_FOOD_AGRI_ORDERS[start:end],
    )
