"""语音消息的编排层：转写 → 立即回一句意图确认 → 接入现有 Excel 任务处理流程。

跟 `channels/whatsapp/processor.py::process_message` 平级，职责边界分开：这里只管
"语音怎么变成文字、怎么先回一句话"，拿到文字之后直接交给 `_process_message_locked`，
进度提示/最终结果/发文件全部复用现有逻辑，不重新实现。

注意：这里不能直接调 `process_message`（那样会二次抢 `lock_for(thread_id)`，而
asyncio.Lock 不可重入）。转写本身耗时不定，必须在转写之前就抢到锁、全程持锁到处理
结束，否则同一用户紧接着发的下一条消息可能提前抢锁、先处理完，导致回复顺序错乱。
"""

from __future__ import annotations

import contextlib
import logging

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from src.agent.main import llm
from src.agent_server.shared import runtime as _runtime
from src.agent_server.shared.voice_transcribe import VoiceTranscribeError, transcribe_voice
from src.agent_server.channels.whatsapp.client import send_text
from src.agent_server.channels.whatsapp.processor import _process_message_locked

logger = logging.getLogger(__name__)

VOICE_TRANSCRIBE_FAILED_MESSAGE = "抱歉，刚才那条语音没能听清楚，麻烦换成文字重新发一次，或者再说清楚点重新发一次语音。"
VOICE_EMPTY_MESSAGE = "没有听到语音里说了什么内容，麻烦再说一次或者换成文字发给我。"

_INTENT_ACK_SYSTEM_PROMPT = (
    "用户刚发来一段话，下面是内容。请用一句简短、口语化的中文回一句话：先用你自己的话"
    "概括一下用户想做什么，再说你现在就开始处理。不要提到\"转写\"\"识别\"\"语音\"\"文字\"这类"
    "字眼，就当作正常收到一条消息在回应；不要用列表、不要超过40个字。"
)


async def _quick_intent_ack(text: str) -> str | None:
    try:
        reply = await llm.ainvoke(
            [SystemMessage(content=_INTENT_ACK_SYSTEM_PROMPT), HumanMessage(content=text)]
        )
    except Exception:
        logger.warning("生成语音意图确认回复失败，跳过这句提示", exc_info=True)
        return None
    return str(reply.content).strip() or None


async def process_voice_message(
    phone: str, thread_id: str, run_id: str, audio_bytes: bytes, mimetype: str
) -> None:
    """转写 + 处理全程持有 lock_for(thread_id)，且在转写之前就去抢锁。

    转写耗时不定（几百毫秒到几秒），如果像 process_message 那样在转写之后才抢锁，
    会导致同一个用户紧接着发的下一条消息（文字或语音）提前抢到锁、先处理完，
    使回复顺序和用户发送顺序不一致（"串"了）。所以这里在最外层就持锁，转写和
    _quick_intent_ack 都在锁内完成，最后调 _process_message_locked（它假定锁已持有，
    不会重复抢锁）。
    """
    async with _runtime.lock_for(thread_id):
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                text = await transcribe_voice(audio_bytes, mimetype)
            except VoiceTranscribeError as e:
                logger.exception("语音转写失败（thread_id=%s）", thread_id)
                await _runtime.runs_store.amark_error(run_id, f"语音转写失败: {e}")
                with contextlib.suppress(Exception):
                    await send_text(client, phone, VOICE_TRANSCRIBE_FAILED_MESSAGE)
                return

            if not text.strip():
                await _runtime.runs_store.amark_error(run_id, "语音转写结果为空")
                with contextlib.suppress(Exception):
                    await send_text(client, phone, VOICE_EMPTY_MESSAGE)
                return

            ack = await _quick_intent_ack(text)
            if ack:
                with contextlib.suppress(Exception):
                    await send_text(client, phone, ack)

        await _process_message_locked(phone, thread_id, run_id, text)
