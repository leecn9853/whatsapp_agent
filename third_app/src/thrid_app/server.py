from datetime import date

from fastapi import FastAPI, Query

from thrid_app.data_generator import (
    generate_alipay_matching_records,
    generate_monthly_costs,
    generate_supplier_purchases,
)
from thrid_app.schemas import (
    MonthlyCostRow,
    PagedAlipayMatchingRecords,
    PagedMonthlyCosts,
    SupplierPurchaseRow,
)

app = FastAPI(title="Mock Cost Report Data Service")

_SUPPLIER_PURCHASES = generate_supplier_purchases(150)
_MONTHLY_COSTS = generate_monthly_costs(504)
_ALIPAY_MATCHING_RECORDS = generate_alipay_matching_records(2000)


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


@app.get("/api/alipay/matching-records", response_model=PagedAlipayMatchingRecords)
def get_alipay_matching_records(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=200, ge=1, le=500),
):
    """支付宝匹配数据流水明细（整日数据），支持分页，每页最多 500 条。

    金额区间、成功率、序号不在此接口返回，由下游根据 order_amount 分桶、
    根据 status 统计后在 Excel 中生成。
    """
    start = (page - 1) * page_size
    end = start + page_size
    return PagedAlipayMatchingRecords(
        date=date.today().isoformat(),
        total=len(_ALIPAY_MATCHING_RECORDS),
        page=page,
        page_size=page_size,
        items=_ALIPAY_MATCHING_RECORDS[start:end],
    )

