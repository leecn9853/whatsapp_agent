"""聚合 channels/* 下所有渠道的路由：whatsapp/routes.py、tob/routes.py、tob/admin.py。

渠道内部再拆分成对外 API（routes.py）和内部查看页面（admin.py）之类的多个文件时，
在这里加一行 import 塞进下面的列表；agent_server/__init__.py 只需要
`from src.agent_server.channels import routes as channel_routes` 一次。
"""

from __future__ import annotations

from src.agent_server.channels.tob.admin import routes as _tob_admin_routes
from src.agent_server.channels.tob.routes import routes as _tob_routes
from src.agent_server.channels.whatsapp.routes import routes as _whatsapp_routes

routes = [*_whatsapp_routes, *_tob_routes, *_tob_admin_routes]
