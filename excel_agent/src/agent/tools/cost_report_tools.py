"""成本报表 skill 的工具：调用模拟第三方数据服务（thrid_app）拉取最新采购/成本数据，填进固定
模板 Excel（templates/cost_report.xlsx），用 LibreOffice headless 重算公式后，把「成本分析看板」
里的一个区块渲染成表格图或图表图片发给用户。

模板结构（sheet 名、表头列顺序、单元格坐标、看板区块行列范围）是写死的，不是通用 Excel 处理
场景，所以这里只暴露一个确定性的原子工具 generate_cost_report_image，内部一次性完成"拉两个
接口 -> 填模板 -> LibreOffice 重算 -> 读看板区块 -> 画图 -> 返回 PNG"。

不再生成/发送 Excel 文件给用户——Excel 只是内部中间产物，最终产物是 PNG 图片。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import httpx
import openpyxl
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from src.agent.tools._cost_report_render import (
    CostReportRenderError,
    read_section,
    recalculate_with_libreoffice,
    render_chart_image,
    render_table_image,
)
from src.agent.tools._naming import build_stem
from src.agent.tools.excel_tools import OUTPUT_DIR, _user_dir

THIRD_APP_BASE_URL = os.getenv("THIRD_APP_BASE_URL", "http://127.0.0.1:8800")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
TEMPLATE_PATH = _PROJECT_ROOT / "templates" / "cost_report.xlsx"

# LibreOffice 重算后的完整 xlsx 快照（三个看板区块全都在里面，不止当次画的那个 section），
# 按「任务」（agent_server runs_store 的 run_id）分文件夹存放，供追溯某张图片的数字出处；
# 不通过任何 @tool 暴露给 LLM。留存/清理由后续维护人员自行编写脚本处理，这里不做任何
# 自动过期删除。
SNAPSHOT_DIR = _PROJECT_ROOT / "snapshots" / "cost_report"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

# files.py 用这个集合判断哪些工具调用产生的文件需要自动发给用户（返回值是文件路径）。
COST_REPORT_OUTPUT_TOOL_NAMES = {"generate_cost_report_image"}

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

SECTIONS: dict[str, dict] = {
    "monthly_trend": {
        "title": "月度成本趋势",
        "data_first_row": 5,
        "total_row": 17,
        "columns": ["月份", "总支出(万元)", "总预算(万元)", "差异(万元)", "预算执行率", "超支占比", "超支笔数", "总笔数"],
        "percent_cols": ["预算执行率", "超支占比"],
        "sort_by": "月份",
        "chart": {"kind": "line", "x": "月份", "series": ["总支出(万元)", "总预算(万元)"]},
    },
    "department_budget": {
        "title": "部门预算执行对比",
        "data_first_row": 22,
        "total_row": 28,
        "columns": ["部门", "年度总成本(万元)", "年度预算(万元)", "差异(万元)"],
        "percent_cols": [],
        "chart": {"kind": "bar", "x": "部门", "series": ["年度总成本(万元)", "年度预算(万元)"]},
    },
    "purchase_category": {
        "title": "采购类别成本对比",
        "data_first_row": 33,
        "total_row": 39,
        "columns": [
            "成本类别",
            "供应商采购总额(万元)",
            "明细记录总额(万元)",
            "供应商采购占比",
            "待付款笔数占比",
            "待付款笔数",
            "总笔数",
        ],
        "percent_cols": ["供应商采购占比", "待付款笔数占比"],
        "chart": {"kind": "bar", "x": "成本类别", "series": ["供应商采购总额(万元)", "明细记录总额(万元)"]},
    },
}


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


def _build_filled_workbook(client: httpx.Client, dest_path: Path) -> tuple[list[dict], list[dict]]:
    """拉最新数据，写进 dest_path 处的模板拷贝里的两张原始明细 sheet，返回拉到的数据。"""
    supplier_rows = _fetch_supplier_purchases(client)
    monthly_rows = _fetch_monthly_costs(client)

    dest_path.write_bytes(TEMPLATE_PATH.read_bytes())
    wb = openpyxl.load_workbook(dest_path)

    monthly_ws = wb[_MONTHLY_COST_SHEET]
    _clear_data_rows(monthly_ws, len(_MONTHLY_COST_FIELDS))
    _write_rows(monthly_ws, monthly_rows, _MONTHLY_COST_FIELDS)

    supplier_ws = wb[_SUPPLIER_SHEET]
    _clear_data_rows(supplier_ws, len(_SUPPLIER_FIELDS))
    _write_rows(supplier_ws, supplier_rows, _SUPPLIER_FIELDS)

    wb.save(dest_path)
    return monthly_rows, supplier_rows


def _resolve_image_save_path(section: str, ctx) -> Path:
    user_output_dir = _user_dir(OUTPUT_DIR, ctx)
    stem = build_stem(f"成本报表_{section}", ctx)
    candidate = user_output_dir / f"{stem}.png"
    n = 1
    while candidate.exists():
        candidate = user_output_dir / f"{stem}({n}).png"
        n += 1
    return candidate


@tool(response_format="content_and_artifact")
def generate_cost_report_image(
    runtime: ToolRuntime,
    section: Literal["monthly_trend", "department_budget", "purchase_category"] = "monthly_trend",
    render_type: Literal["table", "chart"] = "table",
) -> tuple[str, str | None]:
    """从模拟第三方数据服务拉取最新的月度成本明细和供应商采购数据，生成一张成本报表图片。

    内部会拉最新数据填进成本报表模板，用 LibreOffice 重算模板里的汇总公式，再把「成本分析看板」
    里对应的区块画成图片——不会生成或发送 Excel 文件给用户，只返回一张 PNG 图片。

    参数:
        section: 想看哪个维度的成本汇总。
            - monthly_trend（默认）：按月份看总支出/总预算/预算执行率/超支情况的趋势。
            - department_budget：按部门看年度总成本 vs 年度预算的执行对比。
            - purchase_category：按采购类别看供应商采购总额 vs 明细记录总额的对比。
            不确定用户想看哪个维度时用默认值 monthly_trend。
        render_type: 图片形式，默认 table（数据表格图，含合计/平均行，信息最全）；用户明确要
            "图表""趋势图""柱状图"之类的可视化效果时才传 chart（不含合计行，画折线图/柱状图）。

    仅当用户明确要生成/查看成本报表（表格或图表）时才调用。
    """
    try:
        # 优先用 agent_server runs_store 里这次任务的 run_id，方便直接跟 runs 表对上；
        # 调试（直接跑 src/agent/main.py）时没有 runs_store 记录，本地生成一个兜底。
        report_id = runtime.context.run_id or uuid.uuid4().hex
        fetch_time = datetime.now()
        with httpx.Client(timeout=30.0) as client:
            with tempfile.TemporaryDirectory(prefix="cost_report_") as tmp_root:
                tmp_root_path = Path(tmp_root)
                filled_path = tmp_root_path / "filled.xlsx"
                monthly_rows, supplier_rows = _build_filled_workbook(client, filled_path)

                profile_dir = tmp_root_path / "lo_profile"
                outdir = tmp_root_path / "lo_out"
                profile_dir.mkdir()
                outdir.mkdir()
                recalculated_path = recalculate_with_libreoffice(filled_path, outdir, profile_dir)

                snapshot_dir = SNAPSHOT_DIR / report_id
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                snapshot_name = f"{section}_{render_type}_{fetch_time:%H%M%S%f}.xlsx"
                shutil.copy2(recalculated_path, snapshot_dir / snapshot_name)

                section_cfg = SECTIONS[section]
                df = read_section(recalculated_path, section_cfg)

                footer_text = (
                    f"数据来源: thrid_app · 拉取时间: {fetch_time:%Y-%m-%d %H:%M:%S} · "
                    f"月度成本 {len(monthly_rows)} 条 / 供应商采购 {len(supplier_rows)} 条 · "
                    f"报表ID: {report_id}"
                )

                save_path = _resolve_image_save_path(section, runtime.context)
                if render_type == "table":
                    render_table_image(df, section_cfg, save_path, footer_text)
                else:
                    render_chart_image(df, section_cfg, save_path, footer_text)
    except httpx.HTTPError as e:
        return (
            f"拉取成本数据失败：{e}\n"
            f"请确认模拟数据服务已启动且 THIRD_APP_BASE_URL（当前为 {THIRD_APP_BASE_URL!r}）配置正确。",
            None,
        )
    except CostReportRenderError as e:
        return (str(e), None)

    kind = "表格图" if render_type == "table" else "图表图"
    content = (
        f"已拉取最新的月度成本明细（{len(monthly_rows)} 条）和供应商采购数据（{len(supplier_rows)} 条），"
        f"生成「{section_cfg['title']}」{kind}：{save_path.name}"
    )
    return content, str(save_path)
