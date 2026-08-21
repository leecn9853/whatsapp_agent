"""报表快照的共享目录约定：按用户再按任务（run_id）分两级文件夹存放渲染中间产物/原始
数据/最终图片，供事后追溯某次交付的数字出处。被 cost_report_tools.py 和
alipay_report_tools.py 共用，不对 LLM 暴露任何 @tool。

留存/清理由后续维护人员自行编写脚本处理，这里不做任何自动过期删除。
"""
from __future__ import annotations

import uuid
from pathlib import Path

from src.agent.tools._naming import sanitize_user_id
from src.context import ContextSchema

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

SNAPSHOT_DIR = _PROJECT_ROOT / "snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def snapshot_user_folder(ctx: ContextSchema | None) -> str:
    """快照按用户分文件夹，比如 whatsapp_85251750935；调试没有 user_id 时就叫 debug。

    user_id 本身已经带渠道前缀（见 thread_ids.py 的 whatsapp_thread_id/tob_thread_id，
    格式是 "whatsapp:<手机号>"/"tob:<external_id>"），sanitize_user_id 把冒号转成下划线后
    就已经是 "whatsapp_85251750935" 这种形式了——这里不能再拼一次 caller，否则会变成
    "whatsapp_whatsapp_85251750935" 这种重复前缀。
    """
    user_id = ctx.user_id if ctx else None
    if user_id:
        return sanitize_user_id(user_id)
    return ctx.caller if ctx else "debug"


def resolve_snapshot_dir(ctx: ContextSchema | None) -> tuple[str, Path]:
    """返回 (report_id, snapshot_dir)。

    report_id 优先用 agent_server runs_store 里这次任务的 run_id，方便直接跟 runs 表对上；
    调试（直接跑 src/agent/main.py）时没有 runs_store 记录，本地生成一个兜底。
    snapshot_dir 是 SNAPSHOT_DIR/<user_folder>/<report_id>，调用前会确保已创建。
    """
    report_id = (ctx.run_id if ctx else None) or uuid.uuid4().hex
    snapshot_dir = SNAPSHOT_DIR / snapshot_user_folder(ctx) / report_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    return report_id, snapshot_dir
