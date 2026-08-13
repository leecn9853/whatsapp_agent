import random

from faker import Faker

from thrid_app.schemas import OrderRow

_fake = Faker("en_US")

_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CNY"]
_INCOTERMS = ["FOB", "CIF", "EXW", "DDP", "FCA", "CPT"]
_STATUSES = ["待发货", "已发货", "已完成", "已取消"]

_ELECTRONICS_PRODUCTS = {
    "手机配件": ["蓝牙耳机", "手机壳", "无线充电器", "数据线", "手机支架"],
    "智能穿戴": ["智能手表", "智能手环", "运动耳机"],
    "充电设备": ["快充充电头", "移动电源", "车载充电器"],
    "照明电器": ["LED灯带", "太阳能庭院灯", "智能台灯"],
    "小家电": ["便携榨汁机", "迷你风扇", "电动剃须刀"],
}

_FOOD_AGRI_PRODUCTS = {
    "冷冻水产": ["冷冻虾仁", "冷冻鱿鱼", "冷冻鳕鱼片"],
    "坚果炒货": ["原味腰果", "开心果", "巴旦木"],
    "茶叶": ["绿茶", "红茶", "乌龙茶"],
    "咖啡豆": ["阿拉比卡咖啡豆", "罗布斯塔咖啡豆"],
    "罐头食品": ["糖水黄桃罐头", "玉米罐头", "午餐肉罐头"],
}


def _random_amount_fields() -> tuple[int, float, float]:
    quantity = random.randint(50, 5000)
    unit_price = round(random.uniform(1.5, 500.0), 2)
    amount = round(quantity * unit_price, 2)
    return quantity, unit_price, amount


def _generate_orders(product_pool: dict[str, list[str]], with_food_fields: bool, count: int) -> list[OrderRow]:
    categories = list(product_pool.keys())
    orders = []
    for i in range(1, count + 1):
        category = random.choice(categories)
        product_name = random.choice(product_pool[category])
        quantity, unit_price, amount = _random_amount_fields()

        row = OrderRow(
            order_no=f"ORD{i:06d}",
            company_name=_fake.company(),
            product_name=product_name,
            product_category=category,
            quantity=quantity,
            unit_price=unit_price,
            amount=amount,
            currency=random.choice(_CURRENCIES),
            destination_country=_fake.country(),
            incoterm=random.choice(_INCOTERMS),
            order_date=_fake.date_between(start_date="-1y", end_date="today").isoformat(),
            status=random.choice(_STATUSES),
            batch_no=f"BATCH{random.randint(100000, 999999)}" if with_food_fields else None,
            shelf_life_days=random.choice([90, 180, 365, 540]) if with_food_fields else None,
        )
        orders.append(row)
    return orders


def generate_electronics_orders(count: int) -> list[OrderRow]:
    return _generate_orders(_ELECTRONICS_PRODUCTS, with_food_fields=False, count=count)


def generate_food_agri_orders(count: int) -> list[OrderRow]:
    return _generate_orders(_FOOD_AGRI_PRODUCTS, with_food_fields=True, count=count)
