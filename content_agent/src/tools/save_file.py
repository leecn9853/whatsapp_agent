# 定义你自己的自定义工具 (Custom Tools)
import random
import re
from datetime import datetime
from pathlib import Path

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from src.context import ContextSchema

# 生成文件统一落到项目根目录下的 output/
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


def _sanitize(text: str, max_len: int = 50) -> str:
    """把标题/用户ID里文件名不允许的字符替换成下划线，并限制长度。"""
    safe = _UNSAFE_CHARS.sub("_", text).strip("_")
    return safe[:max_len] or "untitled"


def _build_stem(title_stem: str, ctx: ContextSchema | None) -> str:
    """按调用来源拼出文件名主体（不含扩展名）。

    - 调试（直接运行 src/main.py）：DEBUG_<标题>_<时间戳>_<随机数>
    - WhatsApp 用户：<标题>_<时间戳>_<用户ID>_<随机数>
    调用来源和用户 ID 通过 ContextSchema 传入
    （见 src/main.py 和 src/webhook.py 里 agent 调用处的 context 参数）。
    """
    caller = ctx.caller if ctx else "debug"
    user_id = ctx.user_id if ctx else None

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    random_suffix = f"{random.randint(0, 999999):06d}"
    safe_title = _sanitize(title_stem)

    if caller == "whatsapp" and user_id:
        safe_user_id = _sanitize(str(user_id).split("@")[0])
        return f"{safe_title}_{timestamp}_{safe_user_id}_{random_suffix}"

    return f"DEBUG_{safe_title}_{timestamp}_{random_suffix}"


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

    stem = _build_stem(title_stem, runtime.context)
    candidate = OUT_DIR / f"{stem}{suffix}"

    # 理论上时间戳+随机数已经足够唯一，这里再加一层保险，避免同一秒内随机数刚好重复时覆盖旧文件
    n = 1
    while candidate.exists():
        candidate = OUT_DIR / f"{stem}({n}){suffix}"
        n += 1

    candidate.write_text(content, encoding="utf-8")
    return str(candidate)
