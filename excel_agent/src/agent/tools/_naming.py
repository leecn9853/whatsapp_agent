"""工具间共享的文件命名逻辑：把标题/用户ID 转成安全文件名，并按调用来源拼出文件名主体。

被 save_file.py 和 excel_tools.py 共用，保持两者生成的文件名规则一致。
"""
import random
import re
from datetime import datetime

from src.context import ContextSchema

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


def sanitize(text: str, max_len: int = 50) -> str:
    """把标题/用户ID里文件名不允许的字符替换成下划线，并限制长度。"""
    safe = _UNSAFE_CHARS.sub("_", text).strip("_")
    return safe[:max_len] or "untitled"


def sanitize_user_id(user_id: str) -> str:
    """把 WhatsApp user_id（如 "12345@c.us"）转成安全的文件名/目录名片段。"""
    return sanitize(str(user_id).split("@")[0])


def build_stem(title_stem: str, ctx: ContextSchema | None) -> str:
    """按调用来源拼出文件名主体（不含扩展名）。

    - 调试（直接运行 src/agent/main.py）：DEBUG_<标题>_<时间戳>_<随机数>
    - WhatsApp 用户：<标题>_<时间戳>_<用户ID>_<随机数>
    调用来源和用户 ID 通过 ContextSchema 传入
    （见 src/agent/main.py 和 src/webhook/whatsapp.py 里 agent 调用处的 context 参数）。
    """
    caller = ctx.caller if ctx else "debug"
    user_id = ctx.user_id if ctx else None

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    random_suffix = f"{random.randint(0, 999999):06d}"
    safe_title = sanitize(title_stem)

    if caller == "whatsapp" and user_id:
        return f"{safe_title}_{timestamp}_{sanitize_user_id(user_id)}_{random_suffix}"

    return f"DEBUG_{safe_title}_{timestamp}_{random_suffix}"
