"""成本报表 skill 的工具：调用模拟第三方数据服务（thrid_app）拉取最新采购/成本数据，
填进固定模板 Excel（templates/cost_report.xlsx）里的两张原始明细 sheet。

模板结构（sheet 名、表头列顺序、单元格坐标）是写死的，不是通用 Excel 处理场景，所以这里只
暴露一个确定性的原子工具 generate_cost_report，内部一次性完成"拉两个接口 -> 拷贝模板 ->
清空旧数据行 -> 按固定列顺序写入 -> 保存"，不拆成 fetch/fill 两个工具让 LLM 自己链式调用
——避免列名拼错、顺序搞反、分页漏页之类的出错空间。

「分类整理」「成本分析看板」两个公式 sheet 不在这里处理：openpyxl 保存时会清空它没有主动写入
的公式单元格的缓存值，用户用 Excel/WPS 打开文件时会自动重新计算出正确结果，不需要服务器侧
用 LibreOffice 之类的工具强制重算。
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import httpx
import openpyxl
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from src.agent.tools._naming import build_stem
from src.agent.tools.excel_tools import OUTPUT_DIR, _user_dir

THIRD_APP_BASE_URL = os.getenv("THIRD_APP_BASE_URL", "http://127.0.0.1:8800")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
TEMPLATE_PATH = _PROJECT_ROOT / "templates" / "cost_report.xlsx"

# webhook.py 用这个集合判断哪些工具调用产生的文件需要自动发给用户（返回值是文件路径），
# 用法和 excel_tools.OUTPUT_FILE_TOOL_NAMES 一致，分开放是因为这是独立的 skill 模块。
COST_REPORT_OUTPUT_TOOL_NAMES = {"generate_cost_report"}

_MONTHLY_COST_SHEET = "月度成本明细"
_MONTHLY_COST_FIELDS = ["month", "department", "cost_category", "amount", "budget", "over_budget"]

_SUPPLIER_SHEET = "供应商采购成本"
_SUPPLIER_FIELDS = [
    "supplier_name",
    "purchase_category",
    "purchase_date",
    "purchase_amount",
    "payment_status",
]

_MAX_PAGES = 20  # 防止 total 异常返回导致死循环的保护上限


class CostReportError(Exception):
    """工具内部错误，转成给 agent 看的错误文本，而不是抛异常中断整轮对话。"""


def _fetch_supplier_purchases(client: httpx.Client) -> list[dict]:
    resp = client.get(f"{THIRD_APP_BASE_URL}/api/electronics/orders")
    resp.raise_for_status()
    return resp.json()


def _fetch_monthly_costs(client: httpx.Client) -> list[dict]:
    page_size = 99
    items: list[dict] = []
    page = 1
    total = None
    while (total is None or len(items) < total) and page <= _MAX_PAGES:
        resp = client.get(
            f"{THIRD_APP_BASE_URL}/api/food-agri/orders",
            params={"page": page, "page_size": page_size},
        )
        resp.raise_for_status()
        data = resp.json()
        total = data["total"]
        items.extend(data["items"])
        if not data["items"]:
            break
        page += 1
    return items


def _clear_data_rows(ws, n_cols: int) -> None:
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=n_cols):
        for cell in row:
            cell.value = None


def _write_rows(ws, records: list[dict], fields: list[str]) -> None:
    for i, record in enumerate(records):
        row = i + 2
        for j, field in enumerate(fields):
            col = j + 1
            value = record[field]
            if field == "purchase_date":
                value = date.fromisoformat(value)
            ws.cell(row=row, column=col, value=value)


def _resolve_cost_report_save_path(output_filename: str | None, ctx) -> Path:
    user_output_dir = _user_dir(OUTPUT_DIR, ctx)
    if output_filename:
        name = Path(output_filename).name
        stem, suffix = Path(name).stem, (Path(name).suffix or ".xlsx")
    else:
        stem = build_stem("成本报表", ctx)
        suffix = ".xlsx"

    candidate = user_output_dir / f"{stem}{suffix}"
    n = 1
    while candidate.exists():
        candidate = user_output_dir / f"{stem}({n}){suffix}"
        n += 1
    return candidate


@tool(response_format="content_and_artifact")
def generate_cost_report(runtime: ToolRuntime, output_filename: str | None = None) -> tuple[str, str | None]:
    """从模拟第三方数据服务拉取最新的月度成本明细和供应商采购数据，填入固定的成本报表模板。

    模板里「分类整理」「成本分析看板」两个 sheet 是公式联动的，服务器侧不会重新计算——回复里
    不要报告或编造具体的汇总数字（总支出、预算执行率等），只需说明报表已更新，用户用 Excel/WPS
    打开即可看到最新汇总。

    仅当用户明确要生成/更新成本报表时才调用，不需要参数，也不需要先用 inspect_excel 看结构
    （模板结构是固定的）。

    返回：生成的文件名，以及实际文件路径。
    """
    try:
        with httpx.Client(timeout=30.0) as client:
            supplier_rows = _fetch_supplier_purchases(client)
            monthly_rows = _fetch_monthly_costs(client)
    except httpx.HTTPError as e:
        return (
            f"拉取成本数据失败：{e}\n"
            f"请确认模拟数据服务已启动且 THIRD_APP_BASE_URL（当前为 {THIRD_APP_BASE_URL!r}）配置正确。",
            None,
        )

    save_path = _resolve_cost_report_save_path(output_filename, runtime.context)
    save_path.write_bytes(TEMPLATE_PATH.read_bytes())

    wb = openpyxl.load_workbook(save_path)
    monthly_ws = wb[_MONTHLY_COST_SHEET]
    _clear_data_rows(monthly_ws, len(_MONTHLY_COST_FIELDS))
    _write_rows(monthly_ws, monthly_rows, _MONTHLY_COST_FIELDS)

    supplier_ws = wb[_SUPPLIER_SHEET]
    _clear_data_rows(supplier_ws, len(_SUPPLIER_FIELDS))
    _write_rows(supplier_ws, supplier_rows, _SUPPLIER_FIELDS)

    wb.save(save_path)

    content = (
        f"已拉取最新的月度成本明细（{len(monthly_rows)} 条）和供应商采购数据（{len(supplier_rows)} 条），"
        f"生成报表：{save_path.name}"
    )
    return content, str(save_path)
