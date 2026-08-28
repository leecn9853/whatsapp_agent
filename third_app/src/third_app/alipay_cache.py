import json
from datetime import date
from pathlib import Path

from third_app.data_generator import generate_alipay_matching_records

CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "alipay_matching_records"


def _cache_file(record_date: date) -> Path:
    return CACHE_DIR / f"{record_date.isoformat()}.json"


def get_matching_records(record_date: date, count: int = 2000) -> list[dict]:
    """按日期返回支付宝匹配流水，优先读取本地 JSON 缓存，缺失时生成并写入缓存。"""
    cache_file = _cache_file(record_date)
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    records = [
        r.model_dump()
        for r in generate_alipay_matching_records(count, record_date)
    ]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return records
