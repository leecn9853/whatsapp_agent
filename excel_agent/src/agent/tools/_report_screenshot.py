"""通用的「把 Excel sheet 原样截图成 PNG」渲染 helper：LibreOffice 转 PDF/重算公式 +
pymupdf 渲图 + Pillow 贴脚注。

被 cost_report_tools.py（经 _cost_report_render.py）和 alipay_report_tools.py（经
_alipay_report_render.py）两个 skill 共用——谁需要给某个 sheet（设好 print_area/
page_setup 之后）出一张原样截图，配合这里的 convert_to_pdf + render_pdf_page_with_footer
拼起来即可，不用各自重复实现一遍 LibreOffice subprocess 调用和脚注贴图逻辑。

不对 LLM 暴露任何 @tool。
"""
from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pymupdf  # 把 LibreOffice 转出来的 PDF 页面渲成图片
from PIL import Image, ImageDraw, ImageFont

SOFFICE_BIN = os.getenv("SOFFICE_BIN", "soffice")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_FONT_PATH = _PROJECT_ROOT / "assets" / "fonts" / "NotoSansSC-Regular.ttf"


class ReportRenderError(Exception):
    """渲染/重算阶段的内部错误，转成给 agent 看的错误文本，不抛异常中断整轮对话。"""


def _soffice_convert(input_path: Path, outdir: Path, profile_dir: Path, fmt: str) -> Path:
    """用 LibreOffice headless 把 input_path 转换成 fmt 格式，返回转换后文件的路径。

    outdir/profile_dir 由调用方提供（调用方负责整体临时目录的生命周期与清理），每次调用要用
    各自独立的目录，避免并发调用时抢同一份 LibreOffice 用户配置锁。recalculate_with_libreoffice
    （fmt="xlsx"，重算公式）和 convert_to_pdf（fmt="pdf"，配合 print_area 截图某个区块）复用
    这一段 subprocess 调用逻辑，区别只在 --convert-to 传的格式。
    """
    try:
        result = subprocess.run(
            [
                SOFFICE_BIN,
                "--headless",
                "--calc",
                "--convert-to",
                fmt,
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
        raise ReportRenderError(
            "未找到 soffice 命令，请确认已安装 LibreOffice（brew install libreoffice）。"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise ReportRenderError("LibreOffice 转换超时。") from e

    if result.returncode != 0:
        raise ReportRenderError(f"LibreOffice 转换失败：{result.stderr or result.stdout}")

    converted = outdir / f"{input_path.stem}.{fmt}"
    if not converted.exists():
        raise ReportRenderError("LibreOffice 没有生成转换后的文件。")
    return converted


def recalculate_with_libreoffice(input_path: Path, outdir: Path, profile_dir: Path) -> Path:
    """用 LibreOffice headless 重新计算 input_path 里所有公式，返回重算后文件的路径。

    outdir/profile_dir 由调用方提供（调用方负责整体临时目录的生命周期与清理），每次调用要用
    各自独立的目录，避免并发调用时抢同一份 LibreOffice 用户配置锁。
    """
    return _soffice_convert(input_path, outdir, profile_dir, "xlsx")


def convert_to_pdf(input_path: Path, outdir: Path, profile_dir: Path) -> Path:
    """把 input_path 转成 PDF——调用方通常先给设好 print_area 的 sheet 导出成「只有一页、
    内容正好是打印区域」的 PDF，再交给 render_pdf_page_with_footer 渲成图片。
    """
    return _soffice_convert(input_path, outdir, profile_dir, "pdf")


def render_pdf_page_with_footer(pdf_path: Path, page_index: int, out_path: Path, footer_text: str) -> None:
    """用 pymupdf 把 PDF 的第 page_index 页渲成高分辨率 PNG，再用 Pillow 在底部贴一条脚注。

    Pillow 画字直接用 _FONT_PATH 读字体文件，不依赖 matplotlib 的字体注册，所以这个函数
    不需要额外的字体初始化步骤。
    """
    doc = pymupdf.open(pdf_path)
    pix = doc[page_index].get_pixmap(dpi=200)
    doc.close()
    section_img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")

    footer_height = 28
    canvas = Image.new("RGB", (section_img.width, section_img.height + footer_height), "white")
    canvas.paste(section_img, (0, 0))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(str(_FONT_PATH), 16)
    text_width = draw.textlength(footer_text, font=font)
    draw.text(
        ((section_img.width - text_width) / 2, section_img.height + 4),
        footer_text,
        font=font,
        fill="#666666",
    )
    canvas.save(out_path)
