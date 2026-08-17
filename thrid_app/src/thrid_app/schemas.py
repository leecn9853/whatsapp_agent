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
