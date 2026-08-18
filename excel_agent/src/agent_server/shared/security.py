"""本机专用路由的访问控制。

进程要绑 0.0.0.0 才能让 WhatsApp 网关访问到 /webhook，但 `/v1/tob/*` 这组 JSON API
目前没做鉴权，不能跟着一起对外暴露，所以套一层 request.client.host 校验，只允许
本机调用。
"""

from __future__ import annotations

from functools import wraps

from starlette.requests import Request
from starlette.responses import PlainTextResponse

_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


def local_only(handler):
    @wraps(handler)
    async def wrapper(request: Request):
        host = request.client.host if request.client else None
        if host not in _LOOPBACK_HOSTS:
            return PlainTextResponse("Forbidden: this endpoint is only accessible from localhost", status_code=403)
        return await handler(request)

    return wrapper
