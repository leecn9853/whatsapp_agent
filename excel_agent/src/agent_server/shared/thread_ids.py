"""各渠道统一的 thread_id 前缀方案，避免不同渠道的原始 id（手机号/external_id/...）
互相碰撞。checkpointer 的 thread_id、`/memories/` 的 namespace（= ContextSchema.user_id）
都要用这里生成的带前缀值，不能直接传渠道原始 id。

toC 这轮没做，先不加 toc_thread_id，用到时再补。
"""

from __future__ import annotations


def whatsapp_thread_id(phone: str) -> str:
    return f"whatsapp:{phone}"


def tob_thread_id(external_id: str) -> str:
    return f"tob:{external_id}"
