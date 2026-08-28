import random
from datetime import date, datetime, time, timedelta

from faker import Faker

from third_app.schemas import AlipayMatchingRecord, MonthlyCostRow, SupplierPurchaseRow

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


_ALIPAY_CATEGORY_MAP = {
    "总存款数据": ["api存款", "DDB存款"],
    "接量数据": ["提款匹配接量", "DDB用户接量", "DDB商家接量", "渠道接量"],
    "提款派发4.0": ["提款派发4.0"],
}
_ALIPAY_STATUSES = ["成功", "成功", "成功", "成功", "失败", "处理中"]
_ALIPAY_FAIL_REMARKS = ["渠道超时", "余额不足", "风控拦截", "用户取消"]
_ALIPAY_AMOUNT_BANDS = [(0, 500), (501, 2000), (2001, 10000), (10001, 30000), (30001, 80000)]


def generate_alipay_matching_records(
    count: int = 2000, record_date: date | None = None
) -> list[AlipayMatchingRecord]:
    report_date = record_date or date.today()
    rows = []
    for i in range(count):
        data_category = random.choice(list(_ALIPAY_CATEGORY_MAP))
        detail_category = random.choice(_ALIPAY_CATEGORY_MAP[data_category])
        low, high = random.choice(_ALIPAY_AMOUNT_BANDS)
        status = random.choice(_ALIPAY_STATUSES)
        occurred_at = datetime.combine(report_date, time.min) + timedelta(
            seconds=random.randint(0, 86399)
        )
        rows.append(
            AlipayMatchingRecord(
                record_id=f"AP{i + 1:08d}",
                data_category=data_category,
                detail_category=detail_category,
                order_amount=round(random.uniform(low, high), 2),
                status=status,
                occurred_at=occurred_at.isoformat(),
                remark=random.choice(_ALIPAY_FAIL_REMARKS) if status == "失败" else None,
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
