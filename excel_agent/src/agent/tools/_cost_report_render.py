"""成本报表图片渲染的私有 helper：LibreOffice 重算公式、读取看板区块、matplotlib 画图。

不对 LLM 暴露任何 @tool，只被 cost_report_tools.py 内部调用。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import openpyxl
import pandas as pd

SOFFICE_BIN = os.getenv("SOFFICE_BIN", "soffice")
_DASHBOARD_SHEET = "成本分析看板"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_FONT_PATH = _PROJECT_ROOT / "assets" / "fonts" / "NotoSansSC-Regular.ttf"

_HEADER_COLOR = "#1F4E78"
_BAND_COLOR = "#F2F2F2"
_TOTAL_ROW_COLOR = "#D9E1F2"

_font_ready = False


def _ensure_cjk_font() -> None:
    global _font_ready
    if _font_ready:
        return
    fm.fontManager.addfont(str(_FONT_PATH))
    font_name = fm.FontProperties(fname=str(_FONT_PATH)).get_name()
    plt.rcParams["font.family"] = font_name
    plt.rcParams["axes.unicode_minus"] = False
    _font_ready = True


class CostReportRenderError(Exception):
    """渲染/重算阶段的内部错误，转成给 agent 看的错误文本，不抛异常中断整轮对话。"""


def recalculate_with_libreoffice(input_path: Path, outdir: Path, profile_dir: Path) -> Path:
    """用 LibreOffice headless 重新计算 input_path 里所有公式，返回重算后文件的路径。

    outdir/profile_dir 由调用方提供（调用方负责整体临时目录的生命周期与清理），每次调用要用
    各自独立的目录，避免并发调用时抢同一份 LibreOffice 用户配置锁。
    """
    try:
        result = subprocess.run(
            [
                SOFFICE_BIN,
                "--headless",
                "--calc",
                "--convert-to",
                "xlsx",
                "--outdir",
                str(outdir),
                str(input_path),
                f"-env:UserInstallation=file://{profile_dir}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as e:
        raise CostReportRenderError(
            "未找到 soffice 命令，请确认已安装 LibreOffice（brew install libreoffice）。"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise CostReportRenderError("LibreOffice 重算公式超时。") from e

    if result.returncode != 0:
        raise CostReportRenderError(f"LibreOffice 重算公式失败：{result.stderr or result.stdout}")

    converted = outdir / input_path.name
    if not converted.exists():
        raise CostReportRenderError("LibreOffice 没有生成重算后的文件。")
    return converted


def read_section(recalculated_path: Path, section_cfg: dict) -> pd.DataFrame:
    """从重算后的 xlsx 的「成本分析看板」sheet 里读出一个区块（含合计/平均行），转成 DataFrame。"""
    wb = openpyxl.load_workbook(recalculated_path, data_only=True)
    ws = wb[_DASHBOARD_SHEET]
    columns = section_cfg["columns"]
    n_cols = len(columns)
    rows = []
    for row in range(section_cfg["data_first_row"], section_cfg["total_row"] + 1):
        rows.append([ws.cell(row=row, column=c).value for c in range(1, n_cols + 1)])
    df = pd.DataFrame(rows, columns=columns)

    sort_by = section_cfg.get("sort_by")
    if sort_by:
        # 模板「分类整理」sheet 用 UNIQUE() 去重月份/类别，没有排序，保留的是原始数据里
        # 第一次出现的顺序——月份是 YYYY-MM 格式，文本排序即为时间顺序，这里只影响画图
        # 展示顺序，不改模板本身。合计/平均行始终是最后一行，排序时单独摘出来后拼回末尾。
        data_df, total_row = df.iloc[:-1], df.iloc[[-1]]
        df = pd.concat([data_df.sort_values(sort_by).reset_index(drop=True), total_row], ignore_index=True)
    return df


def _format_cell(value, col_name: str, percent_cols: set[str]) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if col_name in percent_cols:
        return f"{value * 100:.1f}%"
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def render_table_image(df: pd.DataFrame, section_cfg: dict, out_path: Path, footer_text: str) -> None:
    _ensure_cjk_font()
    percent_cols = set(section_cfg.get("percent_cols", []))
    columns = section_cfg["columns"]
    n_rows, n_cols = df.shape

    cell_text = [
        [_format_cell(row[col], col, percent_cols) for col in columns] for _, row in df.iterrows()
    ]

    fig_width = max(6.0, n_cols * 1.7)
    fig_height = max(2.0, (n_rows + 1) * 0.5)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.axis("off")
    ax.set_title(section_cfg["title"], fontsize=14, fontweight="bold", pad=12)

    table = ax.table(cellText=cell_text, colLabels=columns, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.6)
    table.auto_set_column_width(col=list(range(n_cols)))

    for (r, _c), cell in table.get_celld().items():
        if r == 0:
            cell.set_facecolor(_HEADER_COLOR)
            cell.set_text_props(color="white", fontweight="bold")
        elif r == n_rows:
            cell.set_facecolor(_TOTAL_ROW_COLOR)
            cell.set_text_props(fontweight="bold")
        elif r % 2 == 0:
            cell.set_facecolor(_BAND_COLOR)

    fig.text(0.5, 0.01, footer_text, ha="center", va="bottom", fontsize=7, color="#666666")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def render_chart_image(df: pd.DataFrame, section_cfg: dict, out_path: Path, footer_text: str) -> None:
    _ensure_cjk_font()
    chart_cfg = section_cfg["chart"]
    chart_df = df.iloc[:-1]  # 排除「合计/平均」行，不适合和其余数据点混在一张图里

    x_labels = chart_df[chart_cfg["x"]].tolist()
    series_names = chart_cfg["series"]

    fig, ax = plt.subplots(figsize=(max(6.0, len(x_labels) * 0.8), 5.0))

    if chart_cfg["kind"] == "line":
        for name in series_names:
            ax.plot(x_labels, chart_df[name], marker="o", label=name)
    else:
        width = 0.35
        positions = range(len(x_labels))
        for i, name in enumerate(series_names):
            offset = (i - (len(series_names) - 1) / 2) * width
            ax.bar([p + offset for p in positions], chart_df[name], width=width, label=name)
        ax.set_xticks(list(positions))
        ax.set_xticklabels(x_labels)

    ax.set_title(section_cfg["title"], fontsize=14, fontweight="bold")
    ax.set_ylabel("万元")
    ax.legend()
    ax.grid(alpha=0.3)
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")

    fig.text(0.5, 0.01, footer_text, ha="center", va="bottom", fontsize=7, color="#666666")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
