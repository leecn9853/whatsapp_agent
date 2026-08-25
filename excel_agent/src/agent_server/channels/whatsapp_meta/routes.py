"""/webhook/meta：Meta WhatsApp Cloud API 的 webhook。

GET 是 Meta 后台保存 webhook 配置时发的 handshake 验证：带 hub.mode/hub.verify_token/
hub.challenge 三个 query 参数，hub.verify_token 跟 WHATSAPP_META_VERIFY_TOKEN 对得上就原样把
hub.challenge 当纯文本返回（不能转成数字/JSON）。

POST：校验签名 -> 解析 entry[].changes[].value -> 按 messages/statuses 分流 -> wamid 去重 ->
立即 200，真正耗时的 agent 调用转入后台任务（跟 channels/whatsapp/routes.py 的模式一致）。
本次只处理文本消息，图片/文件类消息直接回复"暂不支持"，见
docs/whatsapp-meta-channel-design.md。
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from src.agent_server.shared import runtime as _runtime
from src.agent_server.shared.thread_ids import whatsapp_meta_thread_id
from src.agent_server.channels.whatsapp_meta import dedup
from src.agent_server.channels.whatsapp_meta.client import send_text
from src.agent_server.channels.whatsapp_meta.processor import process_message

logger = logging.getLogger(__name__)

VERIFY_TOKEN = os.environ["WHATSAPP_META_VERIFY_TOKEN"]
APP_SECRET = os.environ["WHATSAPP_META_APP_SECRET"]

UNSUPPORTED_TYPE_MESSAGE = "目前只支持文字消息，暂不支持图片/文件，请稍后再试。"

# webhook 立即 ack 后，agent 执行转入后台任务；这里持有引用防止任务被 GC。
_background_tasks: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def verify_webhook(request: Request) -> PlainTextResponse:
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN and challenge is not None:
        return PlainTextResponse(challenge)
    return PlainTextResponse("Forbidden", status_code=403)


def _signature_valid(raw_body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


async def _reply_unsupported(phone: str) -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        with contextlib.suppress(Exception):
            await send_text(client, phone, UNSUPPORTED_TYPE_MESSAGE)


async def receive_webhook(request: Request) -> JSONResponse:
    raw_body = await request.body()
    if not _signature_valid(raw_body, request.headers.get("X-Hub-Signature-256")):
        logger.warning("Meta webhook 签名校验失败，拒绝请求")
        return JSONResponse({"error": "invalid signature"}, status_code=403)

    payload = json.loads(raw_body)

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}

            if value.get("statuses"):
                logger.debug("收到 Meta 消息状态回调：\n%s", json.dumps(value["statuses"], ensure_ascii=False))

            for msg in value.get("messages") or []:
                wamid = msg.get("id")
                phone = msg.get("from")
                if not wamid or not phone:
                    continue
                if dedup.seen_or_record(wamid):
                    continue
                dedup.record_inbound(phone)

                if msg.get("type") != "text":
                    task = asyncio.create_task(_reply_unsupported(phone))
                    _track(task)
                    continue

                body = (msg.get("text") or {}).get("body") or ""
                thread_id = whatsapp_meta_thread_id(phone)
                run_id = await _runtime.runs_store.acreate_run(thread_id)
                task = asyncio.create_task(process_message(phone, thread_id, run_id, body))
                _track(task)

    return JSONResponse({"ok": True})


@contextlib.asynccontextmanager
async def lifespan(app):
    yield
    for task in list(_background_tasks):
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)


routes = [
    Route("/meta", verify_webhook, methods=["GET"]),
    Route("/meta", receive_webhook, methods=["POST"]),
]
