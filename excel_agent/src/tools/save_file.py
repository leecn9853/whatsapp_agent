# 定义你自己的自定义工具 (Custom Tools)
from pathlib import Path

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from src.tools._naming import build_stem

# 生成文件统一落到项目根目录下的 output/
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)


@tool
def save_file(filename: str, content: str, runtime: ToolRuntime) -> str:
    """将内容保存为本地文件，统一写入 output 目录。

    仅当用户明确要求生成/保存/导出文件时才调用。文件名会自动加上时间戳、
    随机数（调试时还会加 DEBUG 标记，WhatsApp 用户请求时会加上用户 ID），
    因此不会出现重名覆盖问题，也无需自己处理路径。
    输入 filename 为文件标题（可带扩展名，不需要包含目录），content 为要写入的文本内容。
    返回实际保存的文件路径。
    """
    title_stem = Path(filename).stem
    suffix = Path(filename).suffix

    stem = build_stem(title_stem, runtime.context)
    candidate = OUT_DIR / f"{stem}{suffix}"

    # 理论上时间戳+随机数已经足够唯一，这里再加一层保险，避免同一秒内随机数刚好重复时覆盖旧文件
    n = 1
    while candidate.exists():
        candidate = OUT_DIR / f"{stem}({n}){suffix}"
        n += 1

    candidate.write_text(content, encoding="utf-8")
    return str(candidate)
