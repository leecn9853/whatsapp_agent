from datetime import date

from fastapi import FastAPI, Query

from third_app.alipay_cache import get_matching_records
from third_app.data_generator import generate_monthly_costs, generate_supplier_purchases
from third_app.schemas import (
    AlipayMatchingRecord,
    MonthlyCostRow,
    PagedAlipayMatchingRecords,
    PagedMonthlyCosts,
    SupplierPurchaseRow,
)

app = FastAPI(title="Mock Cost Report Data Service")

_SUPPLIER_PURCHASES = generate_supplier_purchases(150)
_MONTHLY_COSTS = generate_monthly_costs(504)
get_matching_records(date.today())


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
    record_date: date | None = Query(default=None, alias="date"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=200, ge=1, le=500),
):
    """支付宝匹配数据流水明细（整日数据），支持分页，每页最多 500 条。

    可通过 date 参数指定日期，未指定时默认返回当天数据。同一日期的数据会
    缓存为本地 JSON 文件，缓存缺失时才重新生成，保证同一日期分页数据一致。

    金额区间、成功率、序号不在此接口返回，由下游根据 order_amount 分桶、
    根据 status 统计后在 Excel 中生成。
    """
    target_date = record_date or date.today()
    records = get_matching_records(target_date)
    start = (page - 1) * page_size
    end = start + page_size
    return PagedAlipayMatchingRecords(
        date=target_date.isoformat(),
        total=len(records),
        page=page,
        page_size=page_size,
        items=[AlipayMatchingRecord(**r) for r in records[start:end]],
    )

