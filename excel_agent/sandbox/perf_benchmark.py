"""LibreOffice 真实负载性能基准 —— 手动脚本，不接入 CI。

背景：docs/skills-tools-refactor-plan.md 遗留问题 #2 指出"阶段二不做 LibreOffice 进程池"的
依据只建立在空跑 `python3 --version`/容器冷启动的基准上，跟真实 `soffice` 转换耗时不是一个
量级。这个脚本用真实的 `cost-report` CLI 全链路（拉数据 -> 填模板 -> soffice 重算公式 ->
soffice 转 pdf -> pymupdf 渲染 PNG），在不同并发度下测单个 sandbox 容器的耗时/成功率，用
真实数据支撑或推翻"不做池化"的结论。

用法：
    PYTHONPATH=. uv run python sandbox/perf_benchmark.py

前置条件同 smoke_test.py：`docker compose up -d sandbox` 已经跑起来；third_app 也要跑起来
（`cd ../third_app && uv run python main.py`），否则每次调用都会在"拉取数据"这一步就失败，
测不出真实的 LibreOffice 耗时。

每次调用都用不同的 `--report-id`（uuid4），避免命中 generate.py 里"同一个 report_id 复用
已重算数据"的缓存路径——那样测出来的是缓存命中耗时，不是真实 LibreOffice 转换耗时。
"""
from __future__ import annotations

import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from src.agent.backends.docker_sandbox import DockerSandbox

THIRD_APP_PORT = 8800
CONCURRENCY_LEVELS = [1, 2, 4, 8]
GENERATE_CMD = (
    "python3 /workspace/skills/cost-report/scripts/generate.py "
    "--render-type chart --report-id {report_id} --caller debug"
)


@dataclass
class CallResult:
    elapsed: float
    ok: bool
    detail: str


def _check_third_app(sandbox: DockerSandbox) -> bool:
    result = sandbox.execute(
        f"curl -sS -m 3 -o /dev/null -w '%{{http_code}}' http://host.docker.internal:{THIRD_APP_PORT}/docs"
    )
    return result.output.strip() == "200"


def _run_one(sandbox: DockerSandbox) -> CallResult:
    report_id = f"perf-bench-{uuid.uuid4()}"
    start = time.monotonic()
    result = sandbox.execute(GENERATE_CMD.format(report_id=report_id), timeout=120)
    elapsed = time.monotonic() - start
    last_line = result.output.strip().splitlines()[-1] if result.output.strip() else ""
    ok = result.exit_code == 0 and last_line.startswith("RESULT_PATH:")
    return CallResult(elapsed=elapsed, ok=ok, detail=last_line)


def _run_concurrency_level(sandbox: DockerSandbox, n: int) -> list[CallResult]:
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(_run_one, sandbox) for _ in range(n)]
        return [f.result() for f in futures]


def main() -> int:
    sandbox = DockerSandbox()

    print("0. 检查 third_app 是否可达...")
    if not _check_third_app(sandbox):
        print(
            "   ✗ third_app 不可达（http://host.docker.internal:8800/docs 未返回 200）。"
            "请先在另一个终端启动：cd ../third_app && uv run python main.py"
        )
        return 1
    print("   ✓ third_app 可达")

    summary: list[tuple[int, list[CallResult]]] = []
    for n in CONCURRENCY_LEVELS:
        print(f"\n并发度 {n}：跑 {n} 个 cost-report chart 全链路调用...")
        results = _run_concurrency_level(sandbox, n)
        summary.append((n, results))

        ok_count = sum(1 for r in results if r.ok)
        elapsed_list = [r.elapsed for r in results]
        print(f"   成功 {ok_count}/{n}")
        print(
            f"   耗时 min={min(elapsed_list):.2f}s "
            f"max={max(elapsed_list):.2f}s "
            f"avg={statistics.mean(elapsed_list):.2f}s "
            f"每次=[{', '.join(f'{e:.2f}' for e in elapsed_list)}]"
        )
        for r in results:
            if not r.ok:
                print(f"   ✗ 失败详情：elapsed={r.elapsed:.1f}s detail={r.detail!r}")

    print("\n" + "=" * 60)
    print("汇总（并发度 | 成功率 | min/avg/max 耗时）")
    for n, results in summary:
        ok_count = sum(1 for r in results if r.ok)
        elapsed_list = [r.elapsed for r in results]
        print(
            f"  {n:>2} | {ok_count}/{n} | "
            f"{min(elapsed_list):.2f}s / {statistics.mean(elapsed_list):.2f}s / {max(elapsed_list):.2f}s"
        )

    all_ok = all(r.ok for _, results in summary for r in results)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
