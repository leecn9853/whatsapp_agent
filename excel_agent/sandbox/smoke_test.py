"""阶段一手动 smoke test —— 不是正式测试套件，不接入 CI。

用法：
    uv run python sandbox/smoke_test.py

前置条件：`docker compose up -d sandbox` 已经跑起来（容器名见
`src/agent/backends/docker_sandbox.py` 里的 DEFAULT_CONTAINER_NAME）。

第三步（网络连通性）会尝试连 third_app（真实地址见 THIRD_APP_BASE_URL 默认值
http://127.0.0.1:8800）——如果 third_app 没启动，这一步会打印明确提示，不算脚本失败，
只代表"这条腿这次没验证到"，跟 execute/upload/download 是否通过无关。想验证这条腿，先在
另一个终端起 third_app：
    cd ../third_app && uv run python main.py
"""
from __future__ import annotations

import sys
import time

from src.agent.backends.docker_sandbox import DockerSandbox

THIRD_APP_PORT = 8800


def main() -> int:
    sandbox = DockerSandbox()
    ok = True

    print("1. execute 基本命令")
    result = sandbox.execute("python3 --version")
    print(f"   output={result.output!r} exit_code={result.exit_code}")
    if result.exit_code != 0:
        ok = False
        print("   ✗ 非 0 退出码")
    else:
        print("   ✓")

    print("2. upload_files / download_files 往返")
    upload_result = sandbox.upload_files([("/tmp/hello.txt", b"hello from host")])[0]
    if upload_result.error is not None:
        ok = False
        print(f"   ✗ 上传失败：{upload_result.error}")
    else:
        download_result = sandbox.download_files(["/tmp/hello.txt"])[0]
        if download_result.content == b"hello from host":
            print("   ✓ 往返内容一致")
        else:
            ok = False
            print(f"   ✗ 往返内容不一致：error={download_result.error} content={download_result.content!r}")

    print("3. 网络连通性：容器 -> host.docker.internal -> third_app")
    net_result = sandbox.execute(
        f"curl -sS -m 3 -o /dev/null -w '%{{http_code}}' http://host.docker.internal:{THIRD_APP_PORT}/docs"
    )
    if net_result.output.strip() == "200":
        print("   ✓ 连通，third_app /docs 返回 200")
    else:
        print(
            f"   … 未连通（output={net_result.output!r} exit_code={net_result.exit_code}）——"
            "如果 third_app 没启动，这是预期的，不算脚本失败；启动后重跑本脚本再验证。"
        )

    print("4. execute(timeout=...) 真的会终止超时命令，且不影响正常调用")
    which_result = sandbox.execute("which timeout")
    if which_result.exit_code != 0:
        ok = False
        print("   ✗ 镜像里没有 timeout 命令（coreutils），DockerSandbox.execute 的超时机制失效")
    else:
        print(f"   ✓ 镜像自带 timeout 命令：{which_result.output.strip()!r}")

        start = time.monotonic()
        timeout_result = sandbox.execute("sleep 5", timeout=1)
        elapsed = time.monotonic() - start
        if elapsed >= 3:
            ok = False
            print(f"   ✗ sleep 5 + timeout=1 实际耗时 {elapsed:.1f}s，没有被提前终止")
        elif timeout_result.exit_code != 124:
            ok = False
            print(f"   ✗ 超时命令 exit_code={timeout_result.exit_code}，预期 124")
        elif "已被终止" not in timeout_result.output:
            ok = False
            print(f"   ✗ 超时提示文案缺失，output={timeout_result.output!r}")
        else:
            print(f"   ✓ 耗时 {elapsed:.1f}s 被提前终止，exit_code=124，提示文案正确")

        normal_result = sandbox.execute("echo ok", timeout=5)
        if normal_result.exit_code == 0 and normal_result.output.strip() == "ok":
            print("   ✓ 正常命令未超时时不受影响")
        else:
            ok = False
            print(
                f"   ✗ 包一层 timeout 之后正常命令异常：output={normal_result.output!r} "
                f"exit_code={normal_result.exit_code}"
            )

    print("\n全部核心项（execute / upload+download / timeout）" + ("通过 ✓" if ok else "未通过 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
