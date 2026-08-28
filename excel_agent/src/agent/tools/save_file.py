# 定义你自己的自定义工具 (Custom Tools)
from pathlib import Path

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from src.agent.tools._naming import build_stem
from src.agent.tools._paths import PROJECT_ROOT

# 生成文件统一落到项目根目录下的 output/
OUT_DIR = PROJECT_ROOT / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 沙箱容器里 /workspace/output/ 通过 bind mount 跟宿主机 OUT_DIR 是同一份文件，
# 脚本 stdout 打印的 RESULT_PATH 用的是容器内路径，这里做前缀换算。
SANDBOX_OUTPUT_PREFIX = "/workspace/output/"


@tool(response_format="content_and_artifact")
def save_file(
    runtime: ToolRuntime,
    filename: str | None = None,
    content: str | None = None,
    source_path: str | None = None,
) -> tuple[str, str | None]:
    """把内容保存为本地文件，或者把沙箱 execute 命令产出的文件登记成要发给用户的文件。

    两种用法二选一：
    1. 直接写文本内容：仅当用户明确要求生成/保存/导出文件时才调用，传 filename +
       content。文件名会自动加上时间戳、随机数（调试时还会加 DEBUG 标记，WhatsApp
       用户请求时会加上用户 ID），因此不会出现重名覆盖问题，也无需自己处理路径。
       filename 为文件标题（可带扩展名，不需要包含目录），content 为要写入的文本内容。
    2. 登记沙箱产出的文件：脚本执行完 stdout 打印 RESULT_PATH:<路径> 后，把该路径原样
       传给 source_path（此时 filename/content 不需要传，会被忽略——脚本自己已经按
       约定命名好了）。

    返回实际保存/登记的文件名。
    """
    if source_path is not None:
        if not source_path.startswith(SANDBOX_OUTPUT_PREFIX):
            return f"source_path 必须是 {SANDBOX_OUTPUT_PREFIX} 下的路径，收到：{source_path}", None
        rel = source_path[len(SANDBOX_OUTPUT_PREFIX):]
        candidate = (OUT_DIR / rel).resolve()
        if not candidate.is_relative_to(OUT_DIR.resolve()) or not candidate.is_file():
            return f"文件不存在或路径不合法：{source_path}", None
        return f"已保存文件：{candidate.name}", str(candidate)

    if filename is None or content is None:
        return "filename/content 必须成对传入（或者改用 source_path）", None

    title_stem = Path(filename).stem
    suffix = Path(filename).suffix

    stem = build_stem(title_stem, runtime.context)
    candidate = OUT_DIR / f"{stem}{suffix}"

    # 理论上时间戳+随机数已经足够唯一，这里再加一层保险，避免同一秒内随机数刚好重复时覆盖旧文件
    n = 1
    while candidate.exists():
        candidate = OUT_DIR / f"{stem}({n}){suffix}"
        n += 1

    candidate.write_text(content, encoding="utf-8")
    return f"已保存文件：{candidate.name}", str(candidate)
