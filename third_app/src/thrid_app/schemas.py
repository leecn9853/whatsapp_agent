from pydantic import BaseModel


class SupplierPurchaseRow(BaseModel):
    supplier_name: str
    purchase_category: str
    purchase_date: str
    purchase_amount: float
    payment_status: str


class MonthlyCostRow(BaseModel):
    month: str
    department: str
    cost_category: str
    amount: float
    budget: float
    over_budget: str


class PagedMonthlyCosts(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[MonthlyCostRow]


class AlipayMatchingRecord(BaseModel):
    record_id: str
    data_category: str
    detail_category: str
    order_amount: float
    status: str
    occurred_at: str
    remark: str | None = None


class PagedAlipayMatchingRecords(BaseModel):
    date: str
    total: int
    page: int
    page_size: int
    items: list[AlipayMatchingRecord]
