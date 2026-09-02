"""包一个已经在跑的 docker-compose sandbox 容器，实现 deepagents 的
`BaseSandbox` 接口。详细背景/架构决策见 docs/skills-tools-refactor-plan.md。

作为 src/agent/main.py 里 CompositeBackend 的 default backend 接入；
sandbox/smoke_test.py 里也有独立的验证脚本。
"""
from __future__ import annotations

import io
import logging
import shlex
import tarfile
import time
from typing import cast

import docker
import docker.errors
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

logger = logging.getLogger(__name__)

DEFAULT_CONTAINER_NAME = "excel_agent-sandbox-1"  # docker compose 项目名(excel_agent) + service 名(sandbox) 的默认命名
DEFAULT_READY_TIMEOUT_SECONDS = 30.0
DEFAULT_READY_POLL_INTERVAL_SECONDS = 1.0


class DockerSandbox(BaseSandbox):
    """包一个已经通过 `docker compose up -d sandbox` 启动好的长驻容器。

    容器和 agent 进程谁先起没有严格约束——`__init__` 会等待容器进入 `running` 状态，
    在 `ready_timeout` 内容器还没就绪（不存在/还在 created/starting 等瞬时状态）就轮询
    重试，超时后才报清晰的错误，而不是一次查找失败就直接崩溃。
    """

    def __init__(
        self,
        container_name: str = DEFAULT_CONTAINER_NAME,
        *,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        poll_interval: float = DEFAULT_READY_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._client = docker.from_env()
        self._container = self._wait_for_container(container_name, ready_timeout, poll_interval)

    def _wait_for_container(self, container_name: str, ready_timeout: float, poll_interval: float):
        deadline = time.monotonic() + ready_timeout
        attempt = 0
        while True:
            attempt += 1
            try:
                container = self._client.containers.get(container_name)
                status = container.status
            except docker.errors.NotFound:
                container = None
                status = None

            if container is not None and status == "running":
                return container

            state_desc = "不存在" if container is None else f"状态是 {status!r}"
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"sandbox 容器 {container_name!r} 等待 {ready_timeout:.0f}s 后仍{state_desc}，"
                    "启动失败。请先执行 `docker compose up -d sandbox` 并确认容器状态是 "
                    "running，再启动 agent-server。"
                )
            logger.info(
                "等待 sandbox 容器 %r 就绪（第 %d 次检查，当前%s），%.0fs 后重试...",
                container_name,
                attempt,
                state_desc,
                poll_interval,
            )
            time.sleep(poll_interval)

    @property
    def id(self) -> str:
        # docker-py 没有类型标注，Resource.id 的声明类型是 str | None；
        # 这里的 container 是通过 containers.get() 拿到的既有容器，id 必然存在。
        return cast(str, self._container.id)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if timeout is not None:
            # -k 5：SIGTERM 后再等 5s 无效才 SIGKILL；GNU coreutils 语义下超时统一 exit_code=124
            shell_cmd = f"timeout -k 5 {timeout}s sh -c {shlex.quote(command)}"
        else:
            shell_cmd = command
        preview = command if len(command) <= 200 else f"{command[:200]}…"
        started_at = time.monotonic()
        result = self._container.exec_run(["sh", "-c", shell_cmd], demux=True)
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        # docker-py 没有类型标注，exec_run 的 output 字段推断不出 demux=True 时
        # 固定是 (stdout, stderr) 二元组；这里按实际语义显式标注。
        stdout, stderr = cast("tuple[bytes | None, bytes | None]", result.output)
        output = (stdout or b"").decode("utf-8", errors="replace")
        if stderr:
            output += ("\n" if output else "") + stderr.decode("utf-8", errors="replace")
        if timeout is not None and result.exit_code == 124:
            output += ("\n" if output else "") + f"[DockerSandbox] 命令执行超过 {timeout}s，已被终止。"
        if result.exit_code == 0:
            logger.debug(
                "sandbox 命令完成 exit_code=0 耗时 %dms: %s",
                elapsed_ms,
                preview,
            )
        else:
            logger.warning(
                "sandbox 命令失败 exit_code=%s 耗时 %dms: %s",
                result.exit_code,
                elapsed_ms,
                preview,
            )
        return ExecuteResponse(output=output, exit_code=result.exit_code)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses = []
        for path, content in files:
            try:
                tar_stream = io.BytesIO()
                with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                    info = tarfile.TarInfo(name=path.lstrip("/"))
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
                tar_stream.seek(0)
                self._container.put_archive("/", tar_stream)
                responses.append(FileUploadResponse(path=path, error=None))
            except Exception:  # noqa: BLE001 - 协议要求不能往外抛异常，统一转成 error 字段
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses = []
        for path in paths:
            try:
                stream, _ = self._container.get_archive(path)
                tar_bytes = io.BytesIO(b"".join(stream))
                with tarfile.open(fileobj=tar_bytes) as tar:
                    member = tar.getmembers()[0]
                    extracted = tar.extractfile(member)
                    content = extracted.read() if extracted is not None else b""
                responses.append(FileDownloadResponse(path=path, content=content, error=None))
            except Exception:  # noqa: BLE001
                responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
        return responses
