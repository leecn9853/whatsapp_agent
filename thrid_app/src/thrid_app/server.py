from fastapi import FastAPI, Query

from thrid_app.data_generator import generate_monthly_costs, generate_supplier_purchases
from thrid_app.schemas import MonthlyCostRow, PagedMonthlyCosts, SupplierPurchaseRow

app = FastAPI(title="Mock Cost Report Data Service")

_SUPPLIER_PURCHASES = generate_supplier_purchases(150)
_MONTHLY_COSTS = generate_monthly_costs(504)


@app.get("/api/electronics/orders", response_model=list[SupplierPurchaseRow])
def get_supplier_purchases():
    """供应商采购成本数据，一次性返回全部数据，不分页。"""
    return _SUPPLIER_PURCHASES


@app.get("/api/food-agri/orders", response_model=PagedMonthlyCosts)
def get_monthly_costs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=99),
):
    """月度成本明细数据，支持分页，每页最多 99 条。"""
    start = (page - 1) * page_size
    end = start + page_size
    return PagedMonthlyCosts(
        total=len(_MONTHLY_COSTS),
        page=page,
        page_size=page_size,
        items=_MONTHLY_COSTS[start:end],
    )
