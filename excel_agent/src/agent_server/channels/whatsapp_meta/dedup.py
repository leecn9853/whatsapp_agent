"""进程内状态：wamid 去重、每个手机号最近一次收到消息的时间。

都是模块级 dict，重启即丢——Meta 的重复投递一般发生在短时间内，去重不需要跨进程持久化；
24 小时窗口判断本来就是"大致够用"的软限制，不是需要精确审计的数据，没必要为此建表（见
docs/whatsapp-meta-channel-design.md）。

用 time.monotonic() 而不是 datetime.now()/time.time()，避免系统时钟被手动调整或跟 NTP
同步跳变时把去重 TTL、24 小时窗口判断搞乱。
"""

from __future__ import annotations

import time

_DEDUP_TTL_SECONDS = 10 * 60

_seen_wamids: dict[str, float] = {}
_last_inbound_at: dict[str, float] = {}


def _evict_expired() -> None:
    now = time.monotonic()
    expired = [wamid for wamid, expire_at in _seen_wamids.items() if expire_at <= now]
    for wamid in expired:
        del _seen_wamids[wamid]


def seen_or_record(wamid: str) -> bool:
    """已经处理过（未过期）返回 True；否则记录并返回 False。"""
    _evict_expired()
    if wamid in _seen_wamids:
        return True
    _seen_wamids[wamid] = time.monotonic() + _DEDUP_TTL_SECONDS
    return False


def record_inbound(phone: str) -> None:
    _last_inbound_at[phone] = time.monotonic()


def last_inbound_at(phone: str) -> float | None:
    return _last_inbound_at.get(phone)
