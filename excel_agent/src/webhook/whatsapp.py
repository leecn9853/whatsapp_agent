import asyncio
import base64
import contextlib
import logging
import mimetypes
import os
from pathlib import Path

import httpx
from langchain.agents.middleware import InputAgentState
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.context import ContextSchema
from src.main import DATA_DIR
from src.stores.runs_store import RunsStore
from src.tools.excel_tools import OUTPUT_FILE_TOOL_NAMES, save_uploaded_file
from src.webhook import _runtime

logger = logging.getLogger(__name__)

WHATSAPP_SIMULATOR_URL = os.getenv("WHATSAPP_SIMULATOR_URL", "http://localhost:3000")

# 目前只接收 Excel 文件；document 类型消息的后缀不在这个集合里就直接告知用户不支持，
# 不创建 run、不调用 agent。
ALLOWED_EXCEL_EXTENSIONS = {".xlsx", ".xls"}
UNSUPPORTED_FORMAT_MESSAGE = "目前只支持 Excel 文件（.xlsx / .xls），暂不支持这个格式，请重新发送 Excel 文件。"
MEDIA_ERROR_MESSAGE = "刚才那个文件没有接收成功（可能太大或下载失败），请重新发送一次。"

# 单次 attempt 的超时窗口：从本次 attempt 的续跑点开始，跑完这次「剩余的所有
# 步骤」（可能是好几个节点）算作一次 attempt，不是单个节点的超时。
# 超时后 asyncio.wait_for 会取消 agent.astream，已经提交的节点不受影响，下一次 attempt
# 会从下一个未提交的节点续跑（见 _invoke_with_retry）。
# 注意：如果卡住的是某一个节点本身耗时超过这个值（而不是"剩余步骤总数多"），
# 每次 attempt 都会在这同一个节点上超时，重试无法绕开——这种情况要调大这个值，不是加 attempt 次数。
AGENT_ATTEMPT_TIMEOUT_SECONDS = float(os.getenv("AGENT_ATTEMPT_TIMEOUT_SECONDS", "300"))
# 最多尝试几次（含第一次）。
# 续跑机制下重试不会重跑已提交的节点，主要用来兜住网络抖动、偶发超时/5xx 这类瞬时故障，不是为了"给任务更多时间"。
AGENT_MAX_ATTEMPTS = int(os.getenv("AGENT_MAX_ATTEMPTS", "3"))
# 两次尝试之间等多久再重试，按 attempt 次数线性增长（1st retry 等待 3s，2nd retry 等待 6s，3rd retry 等待 9s）。
AGENT_RETRY_BACKOFF_SECONDS = float(os.getenv("AGENT_RETRY_BACKOFF_SECONDS", "3"))
# 处理超过这么久还没回复，先提示用户一句"还在处理"，避免用户以为卡死了
PROCESSING_NOTICE_SECONDS = float(os.getenv("PROCESSING_NOTICE_SECONDS", "20"))
# 处理失败时给用户的通用报错提示，避免把 agent 内部的异常信息直接暴露给用户。
FAILURE_MESSAGE = "抱歉，刚刚处理你的消息时出错了，请稍后再试一次。"

runs_store = RunsStore(DATA_DIR / "runs.sqlite")

# 同一个 user_id 的消息必须串行处理：如果用户连续发两条消息，前一条还没处理完，
# 两个 agent 调用会并发跑在同一个 thread_id 上，而 LangGraph 的 checkpointer
# 对同一 thread 的并发写入行为是未定义的，可能导致本轮生成的文件被错误地
# 归属到上一轮、或者消息重复/漏发。不同 user_id 之间没有这个问题，仍可并发。
_user_locks: dict[str, asyncio.Lock] = {}


def _lock_for(user_id: str) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())


_FILE_OUTPUT_TOOL_NAMES = {"save_file", *OUTPUT_FILE_TOOL_NAMES}

# 每次执行遇到这些工具调用时，往 WhatsApp 推送一句人类可读的进度提示。不在这个
# 表里的工具（比如 write_todos，规划用、跑得快、对用户没有可读性价值）保持静默。
TOOL_PROGRESS_MESSAGES: dict[str, str] = {
    "list_excel_files": "正在查看你的表格文件…",
    "inspect_excel": "正在查看表格内容…",
    "aggregate_excel_sheet": "正在按你的要求汇总数据…",
    "create_chart_sheet": "正在生成图表…",
    "web_search": "正在联网搜索…",
    "save_file": "正在保存文件…",
    "task": "正在委托子任务处理，请稍候…",
}

# webhook 立即 ack 后，agent 执行转入后台任务；这里持有引用防止任务被 GC
# （asyncio 不会保留仅靠 create_task 创建、无人持有的任务的强引用）。
_background_tasks: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _files_saved_this_turn(messages: list) -> list[Path]:
    """从本轮（最后一条 HumanMessage 之后）产出文件的工具调用结果里提取文件路径。

    thread_id 按 user_id 复用，result["messages"] 会带上该会话的完整历史，
    所以只取最后一条 HumanMessage 之后的部分，避免把之前几轮已经发过的文件重新发一遍。
    _FILE_OUTPUT_TOOL_NAMES 里的工具都用 response_format="content_and_artifact"
    声明：真实的绝对路径只放在 ToolMessage.artifact 里，不会进入喂给模型的
    content（否则模型会拿这个真实路径去调内置的文件系统工具，而那些工具跑在
    虚拟路径空间里，根本找不到这个路径——历史上就踩过这个坑）。

    同一个文件路径只保留一份：aggregate_excel_sheet/create_chart_sheet 对已经在
    output/ 里的文件是原地覆盖（见 excel_tools._resolve_save_path），一次任务里
    先聚合再画图时两个工具调用会指向同一个文件，不去重会把同一份文件发两遍。
    """
    human_indices = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    start = human_indices[-1] if human_indices else 0

    paths: list[Path] = []
    seen: set[Path] = set()
    for msg in messages[start:]:
        if not (isinstance(msg, ToolMessage) and msg.name in _FILE_OUTPUT_TOOL_NAMES):
            continue
        if not msg.artifact:
            continue
        path = Path(msg.artifact)
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(path)
    return paths


async def _send_text(client: httpx.AsyncClient, user_id: str, message: str) -> None:
    resp = await client.post(
        f"{WHATSAPP_SIMULATOR_URL}/messages",
        json={"to": user_id, "message": message},
    )
    resp.raise_for_status()


async def _send_file(client: httpx.AsyncClient, user_id: str, path: Path) -> None:
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


async def _notify_if_slow(client: httpx.AsyncClient, user_id: str) -> None:
    """处理超过 PROCESSING_NOTICE_SECONDS 还没结束就提示用户一句，被取消时安静退出。

    和下面按工具调用推送的进度提示是互补关系：这句是"完全没有任何工具调用触发
    推送时"（比如模型长时间纯思考、或第一个工具本身就很慢）的兜底提示。
    """
    await asyncio.sleep(PROCESSING_NOTICE_SECONDS)
    try:
        await _send_text(client, user_id, "正在处理中，请稍候…")
    except Exception:
        logger.warning("发送'处理中'提示给 %s 失败", user_id, exc_info=True)


async def _stream_attempt(
    user_id: str,
    input_: InputAgentState | None,
    config: RunnableConfig,
    context: ContextSchema,
    client: httpx.AsyncClient,
) -> None:
    """跑一次 agent 执行，边跑边按工具调用推送进度。

    input_ 为 None 表示从上次中断的 checkpoint 续跑（见 _invoke_with_retry），
    此时不会重新塞入 HumanMessage。durability="sync" 让每一步的 checkpoint 写入
    在进入下一步之前同步落盘，这样超时/崩溃后续跑时，已经跑完的节点不会重跑。
    """
    async for chunk in _runtime.agent.astream(
        input_,
        config=config,
        context=context,
        stream_mode="updates",
        durability="sync",
    ):
        node_output = chunk.get("model")
        if not node_output:
            continue
        for msg in node_output.get("messages", []):
            seen: set[str] = set()
            for call in getattr(msg, "tool_calls", None) or []:
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
                if not name or name in seen:
                    continue
                seen.add(name)
                text = TOOL_PROGRESS_MESSAGES.get(name)
                if text:
                    with contextlib.suppress(Exception):
                        await _send_text(client, user_id, text)


async def _send_files_saved_this_turn(user_id: str, client: httpx.AsyncClient) -> None:
    """尽力把本轮已经生成、落盘的文件发给用户，即使 agent 本轮整体失败。

    durability="sync" 保证每个节点执行完就同步落盘 checkpoint，所以即使最后一次
    模型调用（比如续跑到 summarization 或最终回复这一步）反复超时导致整轮
    判定为失败，之前已经跑完的 save_file/create_chart_sheet 等工具调用产出的
    文件仍然留在 checkpoint 里——不在失败路径里也尝试发送，就等于把已经生成好
    的结果凭空丢掉，用户只会收到一句报错。
    """
    config: RunnableConfig = {"configurable": {"thread_id": user_id}}
    try:
        snapshot = await _runtime.agent.aget_state(config)
    except Exception:
        logger.exception("读取 %s 的 checkpoint 失败，无法尝试发送本轮已生成的文件", user_id)
        return
    messages = snapshot.values.get("messages") or []
    for path in _files_saved_this_turn(messages):
        try:
            await _send_file(client, user_id, path)
        except Exception:
            logger.exception("发送文件 %s 给 %s 失败", path, user_id)


async def _invoke_with_retry(user_id: str, run_id: str, body: str, client: httpx.AsyncClient):
    """带超时和重试地跑一次完整对话轮次，返回结束后的最终状态。

    重试不再是把同一条用户消息重新灌一遍从头跑，而是从上次中断的 checkpoint
    续跑（agent.astream(None, ...)）：已经成功提交的节点（包括已经执行过的
    工具调用）不会重新执行，避免 save_file 这类有副作用的工具被重复触发。
    只有第 1 次尝试真正带上用户消息；第 2 次起如果发现根本没有可续的
    checkpoint（极端情况：连第一次调用都没能写入任何 checkpoint 就失败了），
    才退化为重新带上原始消息——这种情况下的重复执行风险和过去的行为一致。
    """
    config: RunnableConfig = {"configurable": {"thread_id": user_id}}
    context = ContextSchema(caller="whatsapp", user_id=user_id)
    last_error: Exception | None = None

    for attempt in range(1, AGENT_MAX_ATTEMPTS + 1):
        await runs_store.arecord_attempt(run_id, attempt)

        input_: InputAgentState | None
        if attempt == 1:
            input_ = {"messages": [HumanMessage(content=body)]}
        else:
            input_ = None
            snapshot = await _runtime.agent.aget_state(config)
            if not snapshot.values.get("messages"):
                input_ = {"messages": [HumanMessage(content=body)]}

        try:
            await asyncio.wait_for(
                _stream_attempt(user_id, input_, config, context, client),
                timeout=AGENT_ATTEMPT_TIMEOUT_SECONDS,
            )
            return await _runtime.agent.aget_state(config)
        except Exception as e:
            last_error = e
            logger.warning(
                "第 %d/%d 次调用 agent 失败（user_id=%s）：%s",
                attempt,
                AGENT_MAX_ATTEMPTS,
                user_id,
                e,
                exc_info=True,
            )
            if attempt < AGENT_MAX_ATTEMPTS:
                await asyncio.sleep(AGENT_RETRY_BACKOFF_SECONDS * attempt)
    assert last_error is not None
    raise last_error


async def _process_message(user_id: str, run_id: str, body: str) -> None:
    """后台任务：执行 agent 并把结果/进度推送给用户。

    webhook() 收到消息后立刻 ack，这个函数才是实际耗时的部分——不再阻塞 HTTP
    响应，结果和过程中的进度提示都通过 _send_text/_send_file 主动推送。
    """
    async with _lock_for(user_id), httpx.AsyncClient(timeout=60) as client:
        await runs_store.amark_running(run_id)
        notice_task = asyncio.create_task(_notify_if_slow(client, user_id))
        try:
            snapshot = await _invoke_with_retry(user_id, run_id, body, client)
        except asyncio.CancelledError:
            await runs_store.amark_cancelled(run_id)
            raise
        except Exception as e:
            logger.exception("处理来自 %s 的消息失败", user_id)
            await runs_store.amark_error(run_id, f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
            with contextlib.suppress(Exception):
                await _send_text(client, user_id, FAILURE_MESSAGE)
            await _send_files_saved_this_turn(user_id, client)
            return
        finally:
            notice_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notice_task

        messages = snapshot.values["messages"]
        reply = messages[-1].content
        files = _files_saved_this_turn(messages)
        # agent 本身跑成功就算 success；后面推送给用户失败与否是独立的关注点
        # （和过去的行为一致：发送失败只记日志，不影响 webhook 已经返回的 200）。
        await runs_store.amark_success(run_id)

        try:
            await _send_text(client, user_id, reply)
        except Exception:
            logger.exception("发送回复给 %s 失败", user_id)
            return

        for path in files:
            try:
                await _send_file(client, user_id, path)
            except httpx.HTTPStatusError as e:
                logger.error(
                    "发送文件 %s 给 %s 失败：%s %s",
                    path,
                    user_id,
                    e.response.status_code,
                    e.response.text,
                )
            except Exception:
                logger.exception("发送文件 %s 给 %s 失败", path, user_id)


async def _reset_thread(user_id: str) -> None:
    """删除该用户的对话历史（checkpoint），不影响 /memories/ 长期记忆。

    复用 _lock_for(user_id) 是为了不和该用户正在处理中的 _process_message 并发：
    等它跑完再删，避免删除过程中还有新的 checkpoint 写入进来。
    """
    async with _lock_for(user_id):
        await _runtime.agent.checkpointer.adelete_thread(user_id)


async def webhook(request: Request) -> JSONResponse:
    payload = await request.json()

    if payload.get("event") == "chat_removed":
        data = payload.get("data") or {}
        user_id = data.get("from")
        if user_id and not user_id.endswith("@g.us"):
            task = asyncio.create_task(_reset_thread(user_id))
            _track(task)
        return JSONResponse({"ok": True})

    if payload.get("event") != "message":
        return JSONResponse({"ok": True})

    data = payload.get("data") or {}
    user_id = data.get("from")
    body = data.get("body") or ""
    media = data.get("media")
    media_error = data.get("mediaError")

    if not user_id or not (body or media or media_error):
        return JSONResponse({"ok": True})

    if user_id.endswith("@g.us"):
        # 默认不自动回复群聊，避免机器人在群里刷屏
        return JSONResponse({"ok": True})

    async with httpx.AsyncClient(timeout=60) as client:
        if media_error:
            with contextlib.suppress(Exception):
                await _send_text(client, user_id, MEDIA_ERROR_MESSAGE)
            return JSONResponse({"ok": True})

        if media:
            suffix = Path(media.get("filename") or "").suffix.lower()
            if suffix not in ALLOWED_EXCEL_EXTENSIONS:
                with contextlib.suppress(Exception):
                    await _send_text(client, user_id, UNSUPPORTED_FORMAT_MESSAGE)
                return JSONResponse({"ok": True})

            saved_path = save_uploaded_file(user_id, media["filename"], base64.b64decode(media["data"]))
            notice = f"[用户上传了文件：{saved_path.name}]"
            body = f"{body}\n{notice}" if body else notice

    run_id = await runs_store.acreate_run(user_id)
    task = asyncio.create_task(_process_message(user_id, run_id, body))
    _track(task)

    return JSONResponse({"ok": True})


@contextlib.asynccontextmanager
async def lifespan(app):
    yield
    for task in list(_background_tasks):
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)


routes = [Route("/webhook", webhook, methods=["POST"])]
