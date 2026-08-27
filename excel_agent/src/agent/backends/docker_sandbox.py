"""阶段一：包一个已经在跑的 docker-compose sandbox 容器，实现 deepagents 的
`BaseSandbox` 接口。详细背景/架构决策见 docs/sandbox-selfhosted-plan.md。

阶段一还没有接进 src/agent/main.py 的 backend=；只在 sandbox/smoke_test.py 里独立验证。
"""
from __future__ import annotations

import io
import tarfile

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
        return self._container.id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:  # noqa: ARG002 - exec_run 阶段一暂不支持 timeout
        result = self._container.exec_run(["sh", "-c", command], demux=True)
        stdout, stderr = result.output
        output = (stdout or b"").decode("utf-8", errors="replace")
        if stderr:
            output += ("\n" if output else "") + stderr.decode("utf-8", errors="replace")
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
