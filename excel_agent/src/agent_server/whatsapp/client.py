"""WhatsApp 网关的出站 HTTP 封装：发文字消息、发媒体文件。

纯粹是对 whatsapp_simulator 那两个 HTTP 接口的封装，不做任何"要不要发""发什么"的
业务判断——那部分逻辑在 whatsapp/processor.py 和 routes/webhook.py 里。
"""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path

import httpx

WHATSAPP_SIMULATOR_URL = os.getenv("WHATSAPP_SIMULATOR_URL", "http://localhost:3000")


async def send_text(client: httpx.AsyncClient, user_id: str, message: str) -> None:
    resp = await client.post(
        f"{WHATSAPP_SIMULATOR_URL}/messages",
        json={"to": user_id, "message": message},
    )
    resp.raise_for_status()


async def send_file(client: httpx.AsyncClient, user_id: str, path: Path) -> None:
    mimetype, _ = mimetypes.guess_type(path.name)
    media_base64 = base64.b64encode(path.read_bytes()).decode()
    resp = await client.post(
        f"{WHATSAPP_SIMULATOR_URL}/messages/media",
        json={
            "to": user_id,
            "mediaBase64": media_base64,
            "mimetype": mimetype or "application/octet-stream",
            "filename": path.name,
        },
    )
    resp.raise_for_status()
