"""支付宝匹配数据表 skill 的工具：分页拉取模拟第三方数据服务（thrid_app）当天全部支付宝
匹配流水记录，按 数据类别 -> 明细分类 -> 金额区间 三级动态分组聚合，生成一张带报表ID水印
的截图（PNG）交付给用户。

跟 cost_report_tools.py 的关键差异：
- 没有固定模板——每次调用用 openpyxl 从空白 workbook 现场建表，行数随当天数据量变化。
- 类别/明细分类的取值集合与顺序完全由本次拉到的数据决定（首次出现顺序），这里和
  _alipay_report_render.py 都不允许出现任何写死的分类枚举——第三方接口随时可能返回
  开发者预先不知道的新类别。
- 不依赖 LibreOffice 重算公式——这份报表本身不含公式，是纯数值结果，openpyxl 原生
  写入即为最终结果；LibreOffice 在这里只用来把生成的 sheet 转 PDF 截图。

Excel（report.xlsx）、接口原始数据（data.json）都只落进快照目录留存，供事后追溯，不发给
用户；发给用户的是快照目录里同一份图片的备份（对齐 cost_report_tools.py 的做法）。

聚合算法/单元格样式/截图渲染细节见 _alipay_report_render.py（保持"网络拉取 + 编排"与"纯
聚合/渲染逻辑"分离的约定，同 cost_report_tools.py / _cost_report_render.py 的关系）。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from src.context import ContextSchema
from src.agent.tools._alipay_report_render import (
    build_sheet_plan,
    derive_title_text,
    render_screenshot,
    render_workbook,
)
from src.agent.tools._naming import build_stem
from src.agent.tools._report_screenshot import ReportRenderError
from src.agent.tools._report_snapshot import resolve_snapshot_dir
from src.agent.tools.excel_tools import OUTPUT_DIR, _user_dir

THIRD_APP_BASE_URL = os.getenv("THIRD_APP_BASE_URL", "http://127.0.0.1:8800")

# files.py 用这个集合判断哪些工具调用产生的文件需要自动发给用户（返回值是文件路径）。
ALIPAY_REPORT_OUTPUT_TOOL_NAMES = {"generate_alipay_matching_report"}

_PAGE_SIZE = 500  # 接口 page_size 上限（server.py Query(..., le=500)）
_MAX_PAGES = 20  # 约 2000 条 / 每页 500 条 = 4 页；20 是防止 total 异常返回导致死循环的保护上限


def _fetch_all_alipay_records(client: httpx.Client) -> tuple[list[dict], str, int]:
    """返回 (全部记录, 这批数据对应的自然日, 接口 total)。`date`/`total` 都是接口在每一页
    响应里都会带的顶层字段，跟分页无关，取第一页返回的即可。"""
    items: list[dict] = []
    report_date: str | None = None
    total = None
    page = 1
    while (total is None or len(items) < total) and page <= _MAX_PAGES:
        resp = client.get(
            f"{THIRD_APP_BASE_URL}/api/alipay/matching-records",
            params={"page": page, "page_size": _PAGE_SIZE},
        )
        resp.raise_for_status()
        data = resp.json()
        total = data["total"]
        if report_date is None:
            report_date = data["date"]
        items.extend(data["items"])
        if not data["items"]:
            break
        page += 1
    return items, report_date, total


def _resolve_image_save_path(ctx: ContextSchema | None) -> Path:
    user_output_dir = _user_dir(OUTPUT_DIR, ctx)
    stem = build_stem("支付宝匹配数据表", ctx)
    candidate = user_output_dir / f"{stem}.png"
    n = 1
    while candidate.exists():
        candidate = user_output_dir / f"{stem}({n}).png"
        n += 1
    return candidate


@tool(response_format="content_and_artifact")
def generate_alipay_matching_report(runtime: ToolRuntime[ContextSchema]) -> tuple[str, str | None]:
    """生成「支付宝匹配数据表」报表图片：拉取当天全部流水、动态分组聚合、渲染截图并交付。

    不需要任何参数，一次调用完成拉取→聚合→出表→截图→交付全流程。报表结构和字段口径见
    alipay-report 技能的 SKILL.md。

    仅当用户明确要生成/查看"支付宝匹配数据表"（或近似说法：匹配数据、对账表、存款匹配
    报表、支付宝流水汇总等）时才调用。
    """
    try:
        with httpx.Client(timeout=30.0) as client:
            records, report_date, total = _fetch_all_alipay_records(client)
    except httpx.HTTPError as e:
        return (
            f"拉取支付宝匹配数据失败：{e}\n"
            f"请确认模拟数据服务已启动且 THIRD_APP_BASE_URL（当前为 {THIRD_APP_BASE_URL!r}）配置正确。",
            None,
        )

    if not records:
        return "第三方数据服务当前没有返回任何支付宝匹配记录，无法生成报表。", None

    try:
        report_id, snapshot_dir = resolve_snapshot_dir(runtime.context)

        data_json_path = snapshot_dir / "data.json"
        data_json_path.write_text(
            json.dumps({"date": report_date, "total": total, "items": records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        plan = build_sheet_plan(records)
        title_text = derive_title_text(report_date)
        report_xlsx_path = snapshot_dir / "report.xlsx"
        render_workbook(plan, title_text, report_xlsx_path)

        footer_text = f"报表ID: {report_id}"
        save_path = _resolve_image_save_path(runtime.context)
        with tempfile.TemporaryDirectory(prefix="alipay_report_") as tmp_root:
            render_screenshot(report_xlsx_path, save_path, footer_text, Path(tmp_root))

        # 最终发给用户的图片也备份一份进快照文件夹，跟 report.xlsx/data.json 放在一起。
        shutil.copy2(save_path, snapshot_dir / save_path.name)
    except ReportRenderError as e:
        return (str(e), None)

    content = (
        f"已拉取 {len(records)} 条支付宝匹配流水记录，按 {len(plan.data_category_spans)} 个"
        f"数据类别、{len(plan.detail_category_spans)} 个明细分类聚合，生成报表截图：{save_path.name}"
    )
    return content, str(save_path)
