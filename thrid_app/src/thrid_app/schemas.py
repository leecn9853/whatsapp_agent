from pydantic import BaseModel


class OrderRow(BaseModel):
    order_no: str
    company_name: str
    product_name: str
    product_category: str
    quantity: int
    unit_price: float
    amount: float
    currency: str
    destination_country: str
    incoterm: str
    order_date: str
    status: str
    batch_no: str | None = None
    shelf_life_days: int | None = None


class PagedOrders(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[OrderRow]
