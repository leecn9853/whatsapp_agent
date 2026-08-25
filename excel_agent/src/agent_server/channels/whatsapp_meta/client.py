"""Meta WhatsApp Cloud API 的出站 HTTP 封装：目前只有发文本。

纯粹是对 Graph API `/{phone_number_id}/messages` 接口的封装，不做任何"要不要发"的业务
判断（比如 24 小时会话窗口）——那部分逻辑在 channels/whatsapp_meta/processor.py 里，跟
channels/whatsapp/client.py 的分工原则一致。
"""

from __future__ import annotations

import os

import httpx

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

ACCESS_TOKEN = os.environ["WHATSAPP_META_ACCESS_TOKEN"]
PHONE_NUMBER_ID = os.environ["WHATSAPP_META_PHONE_NUMBER_ID"]


async def send_text(client: httpx.AsyncClient, to: str, text: str) -> None:
    resp = await client.post(
        f"{GRAPH_API_BASE}/{PHONE_NUMBER_ID}/messages",
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        },
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
    )
    resp.raise_for_status()
