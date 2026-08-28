"""包一个已经在跑的 docker-compose sandbox 容器，实现 deepagents 的
`BaseSandbox` 接口。详细背景/架构决策见 docs/skills-tools-refactor-plan.md。

作为 src/agent/main.py 里 CompositeBackend 的 default backend 接入；
sandbox/smoke_test.py 里也有独立的验证脚本。
"""
from __future__ import annotations

import io
import shlex
import tarfile
from typing import cast

import docker
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

DEFAULT_CONTAINER_NAME = "excel_agent-sandbox-1"  # docker compose 项目名(excel_agent) + service 名(sandbox) 的默认命名


class DockerSandbox(BaseSandbox):
    """包一个已经通过 `docker compose up -d sandbox` 启动好的长驻容器。"""

    def __init__(self, container_name: str = DEFAULT_CONTAINER_NAME) -> None:
        self._client = docker.from_env()
        self._container = self._client.containers.get(container_name)

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
        result = self._container.exec_run(["sh", "-c", shell_cmd], demux=True)
        # docker-py 没有类型标注，exec_run 的 output 字段推断不出 demux=True 时
        # 固定是 (stdout, stderr) 二元组；这里按实际语义显式标注。
        stdout, stderr = cast("tuple[bytes | None, bytes | None]", result.output)
        output = (stdout or b"").decode("utf-8", errors="replace")
        if stderr:
            output += ("\n" if output else "") + stderr.decode("utf-8", errors="replace")
        if timeout is not None and result.exit_code == 124:
            output += ("\n" if output else "") + f"[DockerSandbox] 命令执行超过 {timeout}s，已被终止。"
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
