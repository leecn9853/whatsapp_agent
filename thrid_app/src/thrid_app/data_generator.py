import random
from datetime import date, timedelta

from faker import Faker

from thrid_app.schemas import MonthlyCostRow, SupplierPurchaseRow

_fake = Faker("zh_CN")

_MONTHS = [f"2025-{m:02d}" for m in range(1, 13)]
_DEPARTMENTS = ["研发部", "市场部", "销售部", "运营部", "行政部", "人力资源部"]
_COST_CATEGORIES = ["人力成本", "云服务器费用", "市场推广费", "办公租金", "设备采购", "差旅费", "培训费用"]
_PAYMENT_STATUSES = ["已付款", "待付款", "部分付款"]

_MONTH_START = date(2025, 1, 1)
_MONTH_END = date(2025, 12, 31)


def generate_monthly_costs(count: int = 504) -> list[MonthlyCostRow]:
    rows = []
    for _ in range(count):
        amount = round(random.uniform(3.5, 78.0), 2)
        budget = round(random.uniform(5.0, 62.0), 2)
        rows.append(
            MonthlyCostRow(
                month=random.choice(_MONTHS),
                department=random.choice(_DEPARTMENTS),
                cost_category=random.choice(_COST_CATEGORIES),
                amount=amount,
                budget=budget,
                over_budget="是" if amount > budget else "否",
            )
        )
    return rows


def generate_supplier_purchases(count: int = 150) -> list[SupplierPurchaseRow]:
    span_days = (_MONTH_END - _MONTH_START).days
    rows = []
    for _ in range(count):
        purchase_date = _MONTH_START + timedelta(days=random.randint(0, span_days))
        rows.append(
            SupplierPurchaseRow(
                supplier_name=_fake.company(),
                purchase_category=random.choice(_COST_CATEGORIES),
                purchase_date=purchase_date.isoformat(),
                purchase_amount=round(random.uniform(1.0, 30.0), 2),
                payment_status=random.choice(_PAYMENT_STATUSES),
            )
        )
    return rows
